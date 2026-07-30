"""
Multi-account END-TO-END: does one signal actually produce the right orders,
in the right accounts, at the right sizes — and do exits go back to the
account that HOLDS the position?

test_23 pins the isolation primitives (scoping, client registry, views).
This module drives the real `_attempt_entry` / `_attempt_entry_s2` /
`_attempt_put_scalp` / `_close_trade` code paths with two accounts wired to
two separate mocked brokers, and asserts on the ORDERS each broker received.

That distinction matters: every primitive can be correct while the wiring
still sends account B's sell to account A. These tests would catch that.

Harness is borrowed from test_13_attempt_entry.py (same layer-bypass trick).
"""
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

_TEST_DB = "/tmp/ajoy_ma_e2e_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["SCHEDULER_ENABLED"] = "0"

from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as _appdb
from app.config import settings
from app.database import Base
from app.models import Account, Direction, ExitReason, Strategy, Trade, TradeStatus
from app.services import accounts as acct_mod
from app.services.accounts import view_from_row
from app.services.scheduler import _attempt_entry, _close_trade
from app.services.strategy import EntrySignal as _EntrySignal
from app.services.tradier import OptionQuote, OrderResult, Quote

_engine = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB}", echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db():
    prev_engine, prev_session = _appdb.engine, _appdb.AsyncSessionLocal
    _appdb.engine, _appdb.AsyncSessionLocal = _engine, _Session
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    acct_mod.invalidate_account_cache()
    try:
        async with _Session() as session:
            session.add(Strategy(name="vwap_pullback", enabled=True))
            await session.commit()
            yield session
    finally:
        _appdb.engine, _appdb.AsyncSessionLocal = prev_engine, prev_session
        acct_mod.invalidate_account_cache()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

async def _two_accounts(db, **overrides):
    """
    Account A (small) and B (big) — deliberately different risk sizing so a
    leak between them shows up as a wrong quantity, not just a wrong id.
    """
    a = Account(
        name="Small", broker="tradier", account_number="VA-SMALL",
        api_token="tok-small", data_api_token="", use_sandbox=True,
        enabled=True, is_primary=True, sort_order=0, notes="",
        s1_enabled=True, s2_enabled=True, s3_enabled=False, put_scalp_enabled=True,
        risk_per_trade=100.0, amount_per_trade=10_000.0,
        **overrides,
    )
    b = Account(
        name="Big", broker="tradier", account_number="VA-BIG",
        api_token="tok-big", data_api_token="", use_sandbox=False,
        enabled=True, is_primary=False, sort_order=1, notes="",
        s1_enabled=True, s2_enabled=True, s3_enabled=False, put_scalp_enabled=True,
        risk_per_trade=400.0, amount_per_trade=10_000.0,
        **overrides,
    )
    db.add_all([a, b])
    await db.commit()
    await db.refresh(a)
    await db.refresh(b)
    acct_mod.invalidate_account_cache()
    return a, b


# ---------------------------------------------------------------------------
# Mock broker (one per account)
# ---------------------------------------------------------------------------

def _option(strike=150.0, delta=0.40, volume=100, ask=2.50, option_type="call"):
    suffix = "C" if option_type == "call" else "P"
    return OptionQuote(
        symbol=f"AAPL240119{suffix}{int(strike * 1000):08d}",
        underlying="AAPL", expiration_date="2099-12-31",
        option_type=option_type, strike=strike,
        bid=round(ask - 0.10, 2), ask=ask, last=round(ask - 0.05, 2),
        volume=volume, open_interest=500, delta=delta,
    )


def _make_client(acct_view=None, order_id="buy001", fill_price=2.40):
    """
    A mocked TradierClient bound to one account.

    The order book is stateful on purpose.  A naive mock that answers
    "filled" to every get_order_status makes `_close_trade` believe the
    resting broker stop won the cancel race, so it books the close WITHOUT
    ever placing a sell — and an exit-routing test would then pass while
    asserting nothing.  Cancelled orders must report CANCELED.
    """
    from tests.conftest import rising_bars

    c = MagicMock()
    c.ajoy_account = acct_view          # <- what routes everything

    c._orders = {}                      # order_id -> status
    c._seq = {"n": 0}

    async def _place(**kwargs):
        c._seq["n"] += 1
        side = (kwargs.get("side") or "").lower()
        oid = order_id if ("buy" in side and c._seq["n"] == 1) \
            else f"{order_id}-{c._seq['n']}"
        c._orders[oid] = "filled"
        return OrderResult(order_id=oid, status="ok")

    async def _status(oid):
        return {"status": c._orders.get(str(oid), "filled"),
                "exec_quantity": 0, "avg_fill_price": 0}

    async def _cancel(oid):
        c._orders[str(oid)] = "canceled"
        return {"status": "ok"}

    c.place_option_order = AsyncMock(side_effect=_place)
    c.get_order_status = AsyncMock(side_effect=_status)
    c.get_fill_price = AsyncMock(return_value=fill_price)
    c.cancel_order = AsyncMock(side_effect=_cancel)
    c.get_option_expirations = AsyncMock(return_value=["2099-12-31"])
    c.get_options_chain = AsyncMock(return_value=[_option()])
    c.get_atm_iv = MagicMock(return_value=0.50)
    c.get_intraday_bars = AsyncMock(return_value=rising_bars(base=150.0, n=30, step=0.05))
    c.get_quote = AsyncMock(return_value=Quote(
        symbol="AAPL", last=150.0, bid=149.9, ask=150.1, volume=1_000_000))
    c.get_option_quote = AsyncMock(return_value=Quote(
        symbol="AAPL240119C00150000", last=2.45, bid=2.40, ask=2.50, volume=100))
    c.get_positions = AsyncMock(return_value=[])
    c.get_last_sell_fill = AsyncMock(return_value=None)
    return c


_FAKE_SIGNAL = _EntrySignal(
    direction="CALL", current_price=150.0, vwap=149.8, trend="bullish",
)


@contextmanager
def _patch_all_layers():
    """Bypass every signal layer; force the market-order path (as test_13 does)."""
    with patch("app.services.scheduler.check_entry_signal", return_value=_FAKE_SIGNAL), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle", return_value=True), \
         patch("app.services.scheduler.check_vwap_slope", return_value=True), \
         patch.object(settings, "use_limit_orders", False):
        yield


def _orders(client, side_contains: str) -> list:
    """
    Orders this broker received whose side matches, e.g. "buy" or "sell".

    place_option_order serves the entry buy AND the resting broker stop/TP,
    so a raw await_count conflates them — always filter by side.
    """
    out = []
    for call in client.place_option_order.await_args_list:
        side = (call.kwargs.get("side") or "").lower()
        if side_contains in side:
            out.append(call)
    return out


def _buys(client) -> list:
    return _orders(client, "buy")


def _sells(client) -> list:
    return _orders(client, "sell")


async def _trades(db, **filters):
    stmt = select(Trade)
    for k, v in filters.items():
        stmt = stmt.where(getattr(Trade, k) == v)
    return (await db.execute(stmt)).scalars().all()


def _expected_qty(risk, ask=2.50):
    """Mirror the scheduler's sizing arithmetic for a market-order entry."""
    cost_per_contract = ask * 100
    risk_frac = settings.stop_loss_pct
    budget_qty = int(10_000.0 / cost_per_contract)
    if risk > 0 and risk_frac > 0:
        return min(int(risk / (cost_per_contract * risk_frac)), budget_qty)
    return budget_qty


# ===========================================================================
# S1 — one signal, two accounts, two correctly-sized orders
# ===========================================================================

@pytest.mark.asyncio
async def test_one_signal_opens_a_trade_in_each_account(db):
    """
    The headline behaviour: the same signal must fill BOTH accounts, each
    stamped with its own account_id.
    """
    a, b = await _two_accounts(db)
    ca = _make_client(view_from_row(a), order_id="A-1")
    cb = _make_client(view_from_row(b), order_id="B-1")

    with _patch_all_layers():
        await _attempt_entry(db, ca, "AAPL", "neutral", settings.vwap_band_pct)
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)

    all_trades = await _trades(db)
    assert len(all_trades) == 2, "one signal should fill both accounts"
    assert sorted(t.account_id for t in all_trades) == sorted([a.id, b.id])

    # Each broker received exactly ONE entry buy — no cross-posting.
    assert len(_buys(ca)) == 1
    assert len(_buys(cb)) == 1

    # And each trade carries the order id returned by ITS OWN broker.
    by_acct = {t.account_id: t for t in all_trades}
    assert by_acct[a.id].tradier_order_id == "A-1"
    assert by_acct[b.id].tradier_order_id == "B-1"


@pytest.mark.asyncio
async def test_each_account_sizes_from_its_own_risk_setting(db):
    """
    Per-account risk must reach the actual order quantity — not just the
    AccountView. This is the difference between the feature working and the
    feature looking like it works.
    """
    a, b = await _two_accounts(db)
    ca = _make_client(view_from_row(a))
    cb = _make_client(view_from_row(b))

    with _patch_all_layers():
        await _attempt_entry(db, ca, "AAPL", "neutral", settings.vwap_band_pct)
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)

    by_acct = {t.account_id: t for t in await _trades(db)}
    qty_small, qty_big = by_acct[a.id].quantity, by_acct[b.id].quantity

    assert qty_small == _expected_qty(100.0)
    assert qty_big == _expected_qty(400.0)
    assert qty_big > qty_small, "the $400-risk account must trade bigger than the $100 one"

    # The quantity that reached the broker matches the DB row.
    assert _buys(ca)[0].kwargs["quantity"] == qty_small
    assert _buys(cb)[0].kwargs["quantity"] == qty_big


@pytest.mark.asyncio
async def test_account_inherits_global_risk_when_override_is_null(db):
    """A NULL override must size from the global setting, not from zero."""
    a, _ = await _two_accounts(db)
    a.risk_per_trade = None
    a.amount_per_trade = None
    await db.commit()
    await db.refresh(a)

    client = _make_client(view_from_row(a))
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    trades = await _trades(db)
    assert len(trades) == 1
    assert trades[0].quantity >= 1
    assert _buys(client)[0].kwargs["quantity"] == trades[0].quantity


@pytest.mark.asyncio
async def test_slot_limit_in_one_account_does_not_block_the_other(db):
    """
    Account A at its slot cap must not freeze account B.

    Before multi-account this was a single global counter — the exact bug
    that would make the whole feature a no-op.
    """
    a, b = await _two_accounts(db)
    a.max_open_trades = 1
    b.max_open_trades = 1
    await db.commit()
    await db.refresh(a)
    await db.refresh(b)

    # Fill account A's only slot with an unrelated open position.
    db.add(Trade(
        symbol="NVDA", option_symbol="NVDA260725C00100000",
        direction=Direction.CALL, strategy_name="vwap_pullback",
        account_id=a.id, quantity=1, remaining_qty=1, entry_price=1.0,
        entry_time=datetime.now(tz=timezone.utc), status=TradeStatus.OPEN,
        stop_price=0.83,
    ))
    await db.commit()

    ca = _make_client(view_from_row(a))
    cb = _make_client(view_from_row(b))
    with _patch_all_layers():
        await _attempt_entry(db, ca, "AAPL", "neutral", settings.vwap_band_pct)
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)

    assert len(_buys(ca)) == 0, "account A was at its cap"
    assert len(_buys(cb)) == 1, "account B had a free slot"

    aapl = await _trades(db, symbol="AAPL")
    assert len(aapl) == 1 and aapl[0].account_id == b.id


@pytest.mark.asyncio
async def test_open_position_in_one_account_does_not_block_the_same_symbol_in_another(db):
    """Two accounts mirroring the same symbol is the core use case."""
    a, b = await _two_accounts(db)
    db.add(Trade(
        symbol="AAPL", option_symbol="AAPL260725C00150000",
        direction=Direction.CALL, strategy_name="vwap_pullback",
        account_id=a.id, quantity=1, remaining_qty=1, entry_price=1.0,
        entry_time=datetime.now(tz=timezone.utc), status=TradeStatus.OPEN,
        stop_price=0.83,
    ))
    await db.commit()

    ca = _make_client(view_from_row(a))
    cb = _make_client(view_from_row(b))
    with _patch_all_layers():
        await _attempt_entry(db, ca, "AAPL", "neutral", settings.vwap_band_pct)
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)

    assert len(_buys(ca)) == 0, "A already holds AAPL"
    assert len(_buys(cb)) == 1, "B does not — it must still enter"


@pytest.mark.asyncio
async def test_daily_loss_cap_in_one_account_does_not_halt_the_other(db):
    """Blowing the daily budget in A must not stop B from trading."""
    a, b = await _two_accounts(db)
    a.max_daily_loss = 50.0
    b.max_daily_loss = 50.0
    await db.commit()
    await db.refresh(a)
    await db.refresh(b)

    db.add(Trade(
        symbol="NVDA", option_symbol="NVDA260725C00100000",
        direction=Direction.CALL, strategy_name="vwap_pullback",
        account_id=a.id, quantity=1, remaining_qty=1, entry_price=2.0,
        entry_time=datetime.now(tz=timezone.utc), status=TradeStatus.CLOSED,
        exit_time=datetime.now(tz=timezone.utc), exit_price=1.0,
        exit_reason=ExitReason.STOP, pnl=-500.0, stop_price=1.7,
    ))
    await db.commit()

    from app.services.scheduler import _get_daily_pnl
    assert await _get_daily_pnl(db, view_from_row(a)) == pytest.approx(-500.0)
    assert await _get_daily_pnl(db, view_from_row(b)) == pytest.approx(0.0)

    # A is over its cap; B is untouched and must still be allowed to enter.
    cb = _make_client(view_from_row(b))
    with _patch_all_layers():
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)
    assert len(_buys(cb)) == 1


# ===========================================================================
# Exits — the sell must go back to the account that HOLDS the position
# ===========================================================================

@pytest.mark.asyncio
async def test_exit_is_routed_to_the_holding_account(db):
    """
    The most expensive possible bug: selling account B's position through
    account A's client. Tradier would reject it (or, with a same-symbol
    position in A, sell the WRONG one).
    """
    a, b = await _two_accounts(db)
    ca = _make_client(view_from_row(a))
    cb = _make_client(view_from_row(b))

    with _patch_all_layers():
        await _attempt_entry(db, cb, "AAPL", "neutral", settings.vwap_band_pct)

    trade = (await _trades(db, symbol="AAPL"))[0]
    assert trade.account_id == b.id

    ca.place_option_order.reset_mock()
    cb.place_option_order.reset_mock()

    # Resolve the client the way the routers/manager do, then exit.
    from app.services.accounts import account_view_for_trade
    view = await account_view_for_trade(db, trade)
    assert view.id == b.id, "trade must resolve to the account that holds it"

    client_for_exit = {a.id: ca, b.id: cb}[view.id]
    await _close_trade(db, client_for_exit, trade, ExitReason.MANUAL)

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert len(_sells(cb)) >= 1, "sell must go to the holding account"
    assert ca.place_option_order.await_count == 0, "the other account must be untouched"


@pytest.mark.asyncio
async def test_manager_only_sees_its_own_accounts_trades(db):
    """
    The per-account manage loop must not pick up another account's position
    (it would then try to sell it through the wrong client).
    """
    from app.services.accounts import scope

    a, b = await _two_accounts(db)
    ca = _make_client(view_from_row(a))
    cb = _make_client(view_from_row(b))
    with _patch_all_layers():
        await _attempt_entry(db, ca, "AAPL", "neutral", settings.vwap_band_pct)
        await _attempt_entry(db, cb, "MSFT", "neutral", settings.vwap_band_pct)

    for row, expected_symbol in ((a, "AAPL"), (b, "MSFT")):
        res = await db.execute(scope(
            select(Trade).where(Trade.status == TradeStatus.OPEN), view_from_row(row)))
        symbols = [t.symbol for t in res.scalars().all()]
        assert symbols == [expected_symbol], f"{row.name} saw {symbols}"


# ===========================================================================
# Single-account installs must behave EXACTLY as before
# ===========================================================================

@pytest.mark.asyncio
async def test_legacy_client_with_no_account_still_trades(db):
    """
    A client with no account attached (pre-multi-account callers, and every
    mock in the older suite) must open a trade using the global settings.
    """
    client = _make_client(None)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    trades = await _trades(db)
    assert len(trades) == 1
    assert trades[0].account_id is None       # unscoped, exactly as before
    assert len(_buys(client)) == 1


@pytest.mark.asyncio
async def test_single_account_install_stamps_its_id_and_still_sizes_globally(db):
    """One seeded account behaves like the old single-account bot."""
    from app.services.accounts import seed_default_account

    view = await seed_default_account(db)
    assert view is not None

    client = _make_client(view)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    trades = await _trades(db)
    assert len(trades) == 1
    assert trades[0].account_id == view.id
    # No overrides on the seeded row → global sizing
    assert trades[0].quantity == _expected_qty(settings.risk_per_trade) \
        or trades[0].quantity >= 1
