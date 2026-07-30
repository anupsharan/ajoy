"""
Multi-account regression suite (Jul 25 2026).

The whole point of these tests is the failure mode that would cost real
money: an order, an exit or a slot count leaking from one brokerage account
into another.  Every test below pins one specific isolation guarantee.

Isolated DB per module, like test_09/test_15.
"""
import os
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

_TEST_DB = "/tmp/ajoy_multi_account_test.db"
os.environ["SCHEDULER_ENABLED"] = "0"

for _f in [_TEST_DB, _TEST_DB + "-shm", _TEST_DB + "-wal"]:
    pathlib.Path(_f).unlink(missing_ok=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as _appdb
from app.config import settings
from app.models import Account, Base, Direction, ExitReason, Trade, TradeStatus
from app.services import accounts as acct_mod
from app.services.accounts import (
    AccountView,
    account_clause,
    account_of,
    legacy_view,
    scope,
    seed_default_account,
    view_from_row,
)

_engine = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB}", echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db():
    """
    Fresh schema in this module's own DB, with app.database re-pointed at it
    ONLY for the duration of each test.

    The re-point is scoped rather than done at import time on purpose: the
    account helpers resolve `AsyncSessionLocal` at call time, and a permanent
    module-level hijack leaks into whichever test module happens to run next
    (the suite already has documented order-sensitivity — don't add more).
    """
    prev_engine, prev_session = _appdb.engine, _appdb.AsyncSessionLocal
    _appdb.engine, _appdb.AsyncSessionLocal = _engine, _Session
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    acct_mod.invalidate_account_cache()
    try:
        async with _Session() as session:
            yield session
    finally:
        _appdb.engine, _appdb.AsyncSessionLocal = prev_engine, prev_session
        acct_mod.invalidate_account_cache()


def _mk_account(name, **kw) -> Account:
    defaults = dict(
        broker="tradier",
        account_number=f"VA{abs(hash(name)) % 10_000_000}",
        api_token=f"token-{name}",
        data_api_token="",
        use_sandbox=True,
        enabled=True,
        is_primary=False,
        sort_order=0,
        notes="",
        s1_enabled=True,
        s2_enabled=True,
        s3_enabled=False,
        put_scalp_enabled=True,
    )
    defaults.update(kw)
    return Account(name=name, **defaults)


def _mk_trade(symbol="AMZN", account_id=None, status=TradeStatus.OPEN, **kw):
    defaults = dict(
        option_symbol=f"{symbol}260725C00100000",
        direction=Direction.CALL,
        strategy_name="vwap_pullback",
        quantity=1,
        remaining_qty=1,
        entry_price=1.00,
        entry_time=datetime.now(tz=timezone.utc),
        stop_price=0.83,
    )
    defaults.update(kw)
    return Trade(symbol=symbol, account_id=account_id, status=status, **defaults)


# ===========================================================================
# AccountView — settings resolution
# ===========================================================================

def test_override_wins_over_global_setting():
    """A per-account override replaces the global value for that account."""
    view = AccountView(
        id=2, name="Roth", account_number="VA1", api_token="t",
        overrides={"risk_per_trade": 25.0},
    )
    assert view.setting("risk_per_trade") == 25.0
    # Unset key falls through to the global settings singleton
    assert view.setting("max_open_trades") == settings.max_open_trades


def test_blank_override_inherits_global():
    """NULL in the DB column must inherit, not zero out, the global value."""
    view = AccountView(id=3, name="B", account_number="VA2", api_token="t", overrides={})
    assert view.setting("risk_per_trade") == settings.risk_per_trade
    assert view.setting("s2_max_open_trades") == settings.s2_max_open_trades


def test_legacy_view_disables_account_filtering():
    """
    The .env fallback must not filter trades by account.

    This is the back-compat contract the whole pre-existing suite rests on:
    mock clients resolve to the legacy view, whose id is None.
    """
    legacy = legacy_view()
    assert legacy.id is None
    assert account_clause(legacy) is None


def test_account_of_mock_client_is_legacy():
    """A MagicMock client (every old test) must resolve to the legacy view."""
    assert account_of(MagicMock()).id is None
    assert account_of(None).id is None


def test_account_of_reads_the_attached_view():
    view = AccountView(id=7, name="Roth", account_number="VA9", api_token="t")
    client = MagicMock()
    client.ajoy_account = view
    assert account_of(client) is view


def test_strategy_enrolment_flags():
    view = AccountView(
        id=1, name="A", account_number="VA1", api_token="t",
        s1_enabled=True, s2_enabled=False, s3_enabled=False, put_scalp_enabled=True,
    )
    assert view.strategy_enabled("s1_enabled") is True
    assert view.strategy_enabled("s2_enabled") is False
    assert view.strategy_enabled("put_scalp_enabled") is True


# ===========================================================================
# Query scoping — the money-critical isolation
# ===========================================================================

@pytest.mark.asyncio
async def test_open_trades_are_scoped_per_account(db):
    """
    Account A's open position must not appear in account B's slot count.

    Without this, one account holding MAX_OPEN_TRADES would freeze every
    other account — the multi-account feature would do nothing.
    """
    a = _mk_account("A", is_primary=False)
    b = _mk_account("B")
    db.add_all([a, b])
    await db.commit()

    db.add_all([
        _mk_trade("AMZN", account_id=a.id),
        _mk_trade("NVDA", account_id=a.id),
        _mk_trade("INTC", account_id=b.id),
    ])
    await db.commit()

    va, vb = view_from_row(a), view_from_row(b)
    res_a = await db.execute(scope(select(Trade).where(Trade.status == TradeStatus.OPEN), va))
    res_b = await db.execute(scope(select(Trade).where(Trade.status == TradeStatus.OPEN), vb))
    assert len(res_a.scalars().all()) == 2
    assert len(res_b.scalars().all()) == 1


@pytest.mark.asyncio
async def test_same_symbol_open_in_two_accounts(db):
    """
    The per-symbol "already open" gate is per account.

    Two accounts mirroring the same signal is the core use case; a shared
    gate would silently let only the first account trade.
    """
    a, b = _mk_account("A"), _mk_account("B")
    db.add_all([a, b])
    await db.commit()
    db.add_all([_mk_trade("AMZN", account_id=a.id), _mk_trade("AMZN", account_id=b.id)])
    await db.commit()

    for row, expected in ((a, 1), (b, 1)):
        res = await db.execute(scope(
            select(Trade).where(Trade.symbol == "AMZN",
                                Trade.status == TradeStatus.OPEN),
            view_from_row(row)))
        assert len(res.scalars().all()) == expected


@pytest.mark.asyncio
async def test_primary_account_owns_legacy_null_rows(db):
    """Trades written before the migration belong to the primary account."""
    primary = _mk_account("Primary", is_primary=True)
    other = _mk_account("Other")
    db.add_all([primary, other])
    await db.commit()

    db.add_all([
        _mk_trade("AMZN", account_id=None),        # legacy row
        _mk_trade("NVDA", account_id=primary.id),
        _mk_trade("INTC", account_id=other.id),
    ])
    await db.commit()

    res = await db.execute(scope(
        select(Trade).where(Trade.status == TradeStatus.OPEN), view_from_row(primary)))
    assert len(res.scalars().all()) == 2       # legacy + its own

    res = await db.execute(scope(
        select(Trade).where(Trade.status == TradeStatus.OPEN), view_from_row(other)))
    symbols = [t.symbol for t in res.scalars().all()]
    assert symbols == ["INTC"]                 # never sees the legacy row


@pytest.mark.asyncio
async def test_daily_pnl_guard_is_per_account(db):
    """
    A blown daily loss cap in one account must not halt the others.

    Each account carries its own risk budget — that is the whole reason for
    per-account sizing.
    """
    from app.services.scheduler import _get_daily_pnl

    a, b = _mk_account("A"), _mk_account("B")
    db.add_all([a, b])
    await db.commit()

    now = datetime.now(tz=timezone.utc)
    db.add_all([
        _mk_trade("AMZN", account_id=a.id, status=TradeStatus.CLOSED,
                  exit_time=now, exit_price=0.5, pnl=-400.0,
                  exit_reason=ExitReason.STOP),
        _mk_trade("NVDA", account_id=b.id, status=TradeStatus.CLOSED,
                  exit_time=now, exit_price=1.5, pnl=120.0,
                  exit_reason=ExitReason.TP2),
    ])
    await db.commit()

    assert await _get_daily_pnl(db, view_from_row(a)) == pytest.approx(-400.0)
    assert await _get_daily_pnl(db, view_from_row(b)) == pytest.approx(120.0)
    # No account → combined book, i.e. the old single-account behaviour
    assert await _get_daily_pnl(db, None) == pytest.approx(-280.0)


@pytest.mark.asyncio
async def test_cooldown_is_per_account(db):
    """A stop-out in account A must not cool down the same symbol in B."""
    from app.services.scheduler import _get_recent_bad_exit

    a, b = _mk_account("A"), _mk_account("B")
    db.add_all([a, b])
    await db.commit()

    db.add(_mk_trade(
        "AMZN", account_id=a.id, status=TradeStatus.CLOSED,
        exit_time=datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        exit_price=0.5, pnl=-50.0, exit_reason=ExitReason.STOP,
    ))
    await db.commit()

    assert await _get_recent_bad_exit(db, "AMZN", view_from_row(a)) is not None
    assert await _get_recent_bad_exit(db, "AMZN", view_from_row(b)) is None


@pytest.mark.asyncio
async def test_symbol_trade_counts_are_per_account(db):
    from app.services.scheduler import _get_symbol_losses_today, _get_symbol_trades_today

    a, b = _mk_account("A"), _mk_account("B")
    db.add_all([a, b])
    await db.commit()

    now = datetime.now(tz=timezone.utc)
    db.add_all([
        _mk_trade("AMZN", account_id=a.id, status=TradeStatus.CLOSED,
                  exit_time=now, exit_price=0.5, pnl=-30.0, exit_reason=ExitReason.STOP),
        _mk_trade("AMZN", account_id=a.id, status=TradeStatus.CLOSED,
                  exit_time=now, exit_price=0.6, pnl=-20.0, exit_reason=ExitReason.STOP),
        _mk_trade("AMZN", account_id=b.id),
    ])
    await db.commit()

    assert await _get_symbol_losses_today(db, "AMZN", view_from_row(a)) == 2
    assert await _get_symbol_losses_today(db, "AMZN", view_from_row(b)) == 0
    assert await _get_symbol_trades_today(db, "AMZN", view_from_row(a)) == 2
    assert await _get_symbol_trades_today(db, "AMZN", view_from_row(b)) == 1


# ===========================================================================
# Client routing — a sell must go to the account that HOLDS the position
# ===========================================================================

def test_client_is_built_from_account_credentials():
    from app.services.tradier import TradierClient

    view = AccountView(
        id=5, name="Roth", account_number="VA555", api_token="secret-roth",
        use_sandbox=True,
    )
    client = TradierClient(view)
    assert client._account_id == "VA555"
    assert client._order_headers["Authorization"] == "Bearer secret-roth"
    assert client.ajoy_account is view


def test_client_with_no_account_matches_legacy_env_behaviour():
    """TradierClient() must still read .env exactly as it did before."""
    from app.services.tradier import TradierClient

    client = TradierClient()
    expected_id = (settings.tradier_account_id_sandbox if settings.use_sandbox
                   else settings.tradier_account_id)
    assert client._account_id == expected_id
    assert client.ajoy_account is None


def test_live_and_sandbox_accounts_get_different_order_endpoints():
    from app.services.tradier import TradierClient

    live = TradierClient(AccountView(id=1, name="L", account_number="1",
                                     api_token="t", use_sandbox=False))
    sand = TradierClient(AccountView(id=2, name="S", account_number="2",
                                     api_token="t", use_sandbox=True))
    assert live._order_base == settings.tradier_base_url.rstrip("/")
    assert sand._order_base == settings.tradier_base_url_sandbox.rstrip("/")


def test_each_account_gets_its_own_cached_client():
    from app.services.tradier import get_tradier_client

    a = AccountView(id=11, name="A", account_number="VA11", api_token="tok-a")
    b = AccountView(id=12, name="B", account_number="VA12", api_token="tok-b")
    ca1, cb, ca2 = (get_tradier_client(a), get_tradier_client(b),
                    get_tradier_client(a))
    assert ca1 is ca2            # cached per account
    assert ca1 is not cb         # never shared across accounts
    assert ca1._account_id == "VA11"
    assert cb._account_id == "VA12"


def test_changing_a_token_produces_a_new_client():
    """
    Editing credentials must not keep authenticating as the old account.

    A stale client would place orders with a revoked/rotated token — or worse,
    into the previous account number.
    """
    from app.services.tradier import get_tradier_client

    before = get_tradier_client(
        AccountView(id=21, name="R", account_number="VA21", api_token="old"))
    after = get_tradier_client(
        AccountView(id=21, name="R", account_number="VA21", api_token="new"))
    assert before is not after
    assert after._order_headers["Authorization"] == "Bearer new"


@pytest.mark.asyncio
async def test_trade_resolves_to_its_own_account(db):
    """account_view_for_trade must return the account that holds the trade."""
    from app.services.accounts import account_view_for_trade

    a = _mk_account("A", is_primary=True)
    b = _mk_account("B")
    db.add_all([a, b])
    await db.commit()

    t = _mk_trade("AMZN", account_id=b.id)
    db.add(t)
    await db.commit()

    view = await account_view_for_trade(db, t)
    assert view.id == b.id
    assert view.name == "B"


@pytest.mark.asyncio
async def test_legacy_trade_resolves_to_primary(db):
    """A trade with no account_id falls back to the primary account."""
    from app.services.accounts import account_view_for_trade

    primary = _mk_account("Primary", is_primary=True)
    db.add_all([primary, _mk_account("Other")])
    await db.commit()

    t = _mk_trade("AMZN", account_id=None)
    db.add(t)
    await db.commit()

    assert (await account_view_for_trade(db, t)).name == "Primary"


# ===========================================================================
# Account roster / seeding
# ===========================================================================

@pytest.mark.asyncio
async def test_seed_creates_primary_and_backfills_trades(db):
    """
    Upgrading an existing install must adopt its history AND any position
    still open at upgrade time — an unowned open trade would be managed by
    nobody.
    """
    db.add_all([
        _mk_trade("AMZN", account_id=None),
        _mk_trade("NVDA", account_id=None, status=TradeStatus.CLOSED,
                  exit_time=datetime.now(tz=timezone.utc), exit_price=1.2, pnl=20.0),
    ])
    await db.commit()

    view = await seed_default_account(db)
    assert view is not None
    assert view.is_primary

    res = await db.execute(select(Trade).where(Trade.account_id.is_(None)))
    assert res.scalars().all() == []          # everything backfilled


@pytest.mark.asyncio
async def test_seed_is_idempotent(db):
    """Startup runs the seeder every time; it must only ever create one row."""
    assert await seed_default_account(db) is not None
    assert await seed_default_account(db) is None

    res = await db.execute(select(Account))
    assert len(res.scalars().all()) == 1


@pytest.mark.asyncio
async def test_disabled_account_is_skipped_by_scanners_but_not_the_manager(db):
    """
    Disabling stops NEW entries only.

    Abandoning an open position because a checkbox was unticked would be the
    worst failure in this system, so the manager still sees every account.
    """
    from app.services.accounts import active_account_views, all_account_views

    db.add_all([_mk_account("On", enabled=True), _mk_account("Off", enabled=False)])
    await db.commit()
    acct_mod.invalidate_account_cache()

    assert [v.name for v in await active_account_views()] == ["On"]
    assert sorted(v.name for v in await all_account_views()) == ["Off", "On"]


@pytest.mark.asyncio
async def test_all_accounts_disabled_stops_trading(db):
    """
    "Every account disabled" is a deliberate stop and must NOT silently fall
    back to trading the .env account.
    """
    from app.services.accounts import active_account_views

    db.add_all([_mk_account("A", enabled=False), _mk_account("B", enabled=False)])
    await db.commit()
    acct_mod.invalidate_account_cache()

    assert await active_account_views() == []


@pytest.mark.asyncio
async def test_empty_accounts_table_falls_back_to_env(db):
    """A database that predates the feature keeps trading exactly as before."""
    from app.services.accounts import active_account_views

    acct_mod.invalidate_account_cache()
    views = await active_account_views()
    assert len(views) == 1
    assert views[0].id is None       # legacy view → no account filtering


@pytest.mark.asyncio
async def test_strategy_flag_gates_the_scan_loop(db):
    """
    _for_each_account must skip accounts not enrolled in the strategy —
    and must keep going when one account raises.
    """
    from app.services.scheduler import _for_each_account

    db.add_all([
        _mk_account("S1only", s1_enabled=True,  s2_enabled=False),
        _mk_account("S2only", s1_enabled=False, s2_enabled=True),
        _mk_account("Both",   s1_enabled=True,  s2_enabled=True),
    ])
    await db.commit()
    acct_mod.invalidate_account_cache()

    seen = []

    async def _record(acct):
        seen.append(acct.name)

    await _for_each_account("test", "s1_enabled", _record)
    assert sorted(seen) == ["Both", "S1only"]

    seen.clear()
    await _for_each_account("test", "s2_enabled", _record)
    assert sorted(seen) == ["Both", "S2only"]


@pytest.mark.asyncio
async def test_one_failing_account_does_not_stop_the_others(db):
    """A bad token in one account must not silence every other account."""
    from app.services.scheduler import _for_each_account

    db.add_all([_mk_account("Good1"), _mk_account("Bad"), _mk_account("Good2")])
    await db.commit()
    acct_mod.invalidate_account_cache()

    seen = []

    async def _flaky(acct):
        if acct.name == "Bad":
            raise RuntimeError("401 Unauthorized")
        seen.append(acct.name)

    await _for_each_account("test", None, _flaky)
    assert sorted(seen) == ["Good1", "Good2"]


# ===========================================================================
# Sizing — the per-account risk actually reaches the order
# ===========================================================================

def test_per_account_risk_changes_position_size():
    """
    Two accounts on the same signal must size independently.

    Mirrors the scheduler's sizing arithmetic: qty = risk / (premium × stop%).
    """
    small = AccountView(id=1, name="Small", account_number="1", api_token="t",
                        overrides={"risk_per_trade": 30.0, "amount_per_trade": 5000.0})
    big = AccountView(id=2, name="Big", account_number="2", api_token="t",
                      overrides={"risk_per_trade": 300.0, "amount_per_trade": 5000.0})

    premium, stop_frac = 1.00, 0.17
    cost_per_contract = premium * 100          # $100
    risk_per_contract = cost_per_contract * stop_frac   # $17

    def qty(view):
        budget_qty = int(view.setting("amount_per_trade") / cost_per_contract)
        return min(int(view.setting("risk_per_trade") / risk_per_contract), budget_qty)

    assert qty(small) == 1       # 30 / 17
    assert qty(big) == 17        # 300 / 17
    assert qty(big) > qty(small)


def test_premium_budget_still_caps_an_oversized_risk_account():
    """The premium cap must survive per-account overrides."""
    view = AccountView(id=3, name="Rich", account_number="3", api_token="t",
                       overrides={"risk_per_trade": 10_000.0, "amount_per_trade": 500.0})
    cost_per_contract = 1.00 * 100
    budget_qty = int(view.setting("amount_per_trade") / cost_per_contract)
    risk_qty = int(view.setting("risk_per_trade") / (cost_per_contract * 0.17))
    assert min(risk_qty, budget_qty) == 5      # budget wins, not the risk figure


# ===========================================================================
# API surface
# ===========================================================================

def test_api_never_returns_a_raw_token():
    """
    Tokens are write-only.  A leaked token in a JSON response is a
    credential disclosure, not a cosmetic issue.
    """
    from app.routers.accounts import _mask, _to_out

    row = _mk_account("Roth", api_token="super-secret-token-abcd")
    row.id = 1
    row.created_at = None
    out = _to_out(row)
    dumped = out.model_dump()
    assert "super-secret-token-abcd" not in str(dumped)
    assert out.api_token_masked.endswith("abcd")
    assert _mask("") == ""


def test_masked_token_is_recognised_and_never_stored():
    """PATCHing back a masked token must not overwrite the real credential."""
    from app.routers.accounts import _is_mask, _mask

    assert _is_mask(_mask("abcdefgh")) is True
    assert _is_mask("a-real-token") is False
