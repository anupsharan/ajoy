"""
Tests for scheduler._attempt_entry() — order execution and contract selection.

Verifies:
  • Buy order rejected/canceled → no DB Trade created
  • Delta filter (primary / fallback-1 / fallback-2 / all-fail)
  • Expiry preference: non-0DTE chosen over 0DTE; fallback to 0DTE when only option
  • Entry price: ask (market orders) or mid (limit orders)
  • Quantity sizing: max(1, int(budget / cost_per_contract))
  • IV filter: blocks when ATM IV exceeds threshold; passes when below; None IV allows
  • Limit orders: mid-price used, fill polling, cancel on timeout
  • Signals: all layers (1–4) and regime gate short-circuit gates are bypassed via
    mocked bar data that produces a deterministic signal.

Note: _patch_all_layers() disables limit orders (use_limit_orders=False) so the bulk of
the existing tests exercise the market-order code path unchanged.  See the
"Limit order entry" section at the bottom for limit-order-specific tests.
"""
import os, pytest, pytest_asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_entry_test.db"

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Trade, TradeStatus, Strategy, Direction
from app.services.scheduler import _attempt_entry
from app.services.tradier import OrderResult, OptionQuote, Quote


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_entry_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        session.add(Strategy(name="vwap_pullback", enabled=True))
        await session.commit()
        yield session
    await engine.dispose()


async def _count_trades(db_session) -> int:
    result = await db_session.execute(select(Trade))
    return len(result.scalars().all())


async def _get_trade(db_session) -> Trade | None:
    result = await db_session.execute(select(Trade))
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _option(strike=150.0, delta=0.40, volume=100, ask=2.50,
            option_type="call") -> OptionQuote:
    suffix = "C" if option_type == "call" else "P"
    return OptionQuote(
        symbol=f"AAPL240119{suffix}{int(strike * 1000):08d}",
        underlying="AAPL",
        expiration_date="2099-12-31",
        option_type=option_type,
        strike=strike,
        bid=round(ask - 0.10, 2),
        ask=ask,
        last=round(ask - 0.05, 2),
        volume=volume,
        open_interest=500,
        delta=delta,
    )


def _make_client(
    order_status="filled",
    fill_price=2.40,
    expirations=None,
    chain=None,
    atm_iv=0.50,
    place_raises=False,
):
    """Return a fully-mocked TradierClient ready for _attempt_entry."""
    from tests.conftest import rising_bars

    today = date.today().isoformat()
    default_exp = ["2099-12-31"]   # always non-0DTE by default

    c = MagicMock()

    if place_raises:
        c.place_option_order = AsyncMock(side_effect=Exception("API down"))
    else:
        c.place_option_order = AsyncMock(
            return_value=OrderResult(order_id="buy001", status="ok")
        )
    c.get_order_status = AsyncMock(return_value={"status": order_status})
    c.get_fill_price   = AsyncMock(return_value=fill_price)
    c.cancel_order     = AsyncMock(return_value={"status": "ok"})

    c.get_option_expirations = AsyncMock(return_value=expirations or default_exp)
    c.get_options_chain = AsyncMock(
        return_value=chain or [_option(strike=150.0, delta=0.40, volume=100, ask=2.50)]
    )
    c.get_atm_iv = MagicMock(return_value=atm_iv)   # sync

    # Bars: 30 rising 1-min + 30 rising 15-min → deterministic CALL signal
    bars = rising_bars(base=150.0, n=30, step=0.05)
    c.get_intraday_bars = AsyncMock(return_value=bars)
    c.get_quote = AsyncMock(
        return_value=Quote(
            symbol="AAPL", last=150.0, bid=149.9, ask=150.1, volume=1_000_000
        )
    )
    return c


# All layer checks pass when bars are rising and price is just above VWAP.
# We patch the names as they appear in the *scheduler* module (which imported
# them with `from app.services.strategy import ...`), so patches must target
# app.services.scheduler.<name>, not app.services.strategy.<name>.

from contextlib import contextmanager
from app.services.strategy import EntrySignal as _EntrySignal

_FAKE_SIGNAL = _EntrySignal(
    direction="CALL",
    current_price=150.0,
    vwap=149.8,
    trend="bullish",
)

@contextmanager
def _patch_all_layers():
    """
    Single context manager that:
    - Bypasses every signal layer (returns deterministic CALL signal).
    - Disables limit orders (use_limit_orders=False) so all existing tests
      exercise the market-order path unchanged.  Limit-order behaviour is
      covered by the dedicated tests at the bottom of this file.
    """
    from app.config import settings
    with patch("app.services.scheduler.check_entry_signal",       return_value=_FAKE_SIGNAL), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle",    return_value=True), \
         patch("app.services.scheduler.check_vwap_slope",         return_value=True), \
         patch.object(settings, "use_limit_orders", False):
        yield


# ===========================================================================
# Order rejection / cancellation — no DB record must be created
# ===========================================================================

@pytest.mark.asyncio
async def test_buy_rejected_no_db_record(db):
    client = _make_client(order_status="rejected", fill_price=None)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


@pytest.mark.asyncio
async def test_buy_canceled_no_db_record(db):
    client = _make_client(order_status="canceled", fill_price=None)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


@pytest.mark.asyncio
async def test_buy_cancelled_two_l_no_db_record(db):
    client = _make_client(order_status="cancelled", fill_price=None)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


# ===========================================================================
# Delta filter — primary / fallback-1 / fallback-2 / all-fail
# ===========================================================================

@pytest.mark.asyncio
async def test_delta_filter_primary_passes(db):
    """Delta=0.40, volume=100 — both within range. Trade created from primary filter."""
    client = _make_client(chain=[_option(delta=0.40, volume=100)])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


@pytest.mark.asyncio
async def test_delta_filter_fallback1_relaxes_delta(db):
    """Delta outside range (0.10) but has volume → fallback 1. Trade created."""
    out_of_range = _option(delta=0.10, volume=50, ask=1.50)
    client = _make_client(chain=[out_of_range])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


@pytest.mark.asyncio
async def test_delta_filter_fallback2_ask_only(db):
    """Delta out of range AND volume=0 → fallback 2 (ask>0 only). Trade still created."""
    illiquid = _option(delta=0.10, volume=0, ask=1.50)
    client = _make_client(chain=[illiquid])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


@pytest.mark.asyncio
async def test_all_contracts_zero_ask_no_trade(db):
    """Every contract has ask=0 → all three filters empty → no trade."""
    no_ask = _option(delta=0.40, volume=100, ask=0.0)
    client = _make_client(chain=[no_ask])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


# ===========================================================================
# Expiry selection
# ===========================================================================

@pytest.mark.asyncio
async def test_expiry_prefers_non_0dte(db):
    """When both 0DTE and a future expiry exist, the future expiry is chosen."""
    today     = date.today().isoformat()
    next_week = "2099-12-31"

    chosen = []

    async def record_expiry(ticker, expiration):
        chosen.append(expiration)
        return [_option(strike=150.0, delta=0.40, volume=100)]

    client = _make_client(expirations=[today, next_week])
    client.get_options_chain = record_expiry

    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")

    assert len(chosen) >= 1
    assert chosen[0] == next_week


@pytest.mark.asyncio
async def test_expiry_fallback_to_0dte_when_only_option(db):
    """Only today's expiry available → falls back to 0DTE, trade is still created."""
    today = date.today().isoformat()
    client = _make_client(expirations=[today])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


# ===========================================================================
# Entry price
# ===========================================================================

@pytest.mark.asyncio
async def test_entry_price_always_uses_production_ask(db):
    """
    entry_price must always be the real production ask, even when a sandbox
    fill_price is available.  Sandbox fills are synthetic and diverge from real
    market prices; using them would set stop/TP levels at phantom prices and
    cause immediate stop-outs on the first management tick.
    """
    client = _make_client(fill_price=2.35, chain=[_option(ask=2.50)])
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    trade = await _get_trade(db)
    assert trade is not None
    # Must be ask (2.50), NOT the sandbox fill (2.35)
    assert trade.entry_price == pytest.approx(2.50, abs=0.001)


@pytest.mark.asyncio
async def test_entry_price_uses_ask_when_no_fill(db):
    """No fill_price → entry_price = ask (unchanged behaviour)."""
    client = _make_client(fill_price=None, chain=[_option(ask=2.50)])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    trade = await _get_trade(db)
    assert trade is not None
    assert trade.entry_price == pytest.approx(2.50, abs=0.001)


# ===========================================================================
# Quantity sizing
# ===========================================================================

@pytest.mark.asyncio
async def test_skips_when_one_contract_exceeds_budget(db):
    """
    ask=10.00 → cost_per_contract=1000 > budget=500 → SKIP the trade.
    (Old behaviour was max(1, 0) = 1, which bought a $1000 contract on a
    $500 budget — the sizing-cap fix removes that.)
    """
    from app.config import settings
    expensive = _option(ask=10.00, delta=0.40, volume=100)
    client = _make_client(fill_price=None, chain=[expensive])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})

    with patch.object(settings, "amount_per_trade", 500.0):
        with _patch_all_layers():
            await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_fixed_dollar_risk_caps_qty(db):
    """
    Fixed-dollar risk sizing: qty = risk_per_trade / (premium × stop_loss_pct).
    ask=2.00, stop 25% → risk/contract = $50; risk_per_trade=100 → risk_qty=2,
    even though the $1000 budget would allow 5 contracts.
    """
    from app.config import settings
    client = _make_client(fill_price=None, chain=[_option(ask=2.00, delta=0.40, volume=100)])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})

    with patch.object(settings, "amount_per_trade", 1000.0), \
         patch.object(settings, "risk_per_trade", 100.0), \
         patch.object(settings, "stop_loss_pct", 0.25), \
         patch.object(settings, "use_limit_orders", False):
        with _patch_all_layers():
            await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is not None
    assert trade.quantity == 2   # min(risk_qty=2, budget_qty=5)


@pytest.mark.asyncio
async def test_qty_computed_correctly(db):
    """
    ask=2.00 → cost_per_contract=200
    budget=500 → int(500/200)=2
    """
    from app.config import settings
    cheap = _option(ask=2.00, delta=0.40, volume=100)
    client = _make_client(fill_price=None, chain=[cheap])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})

    with patch.object(settings, "amount_per_trade", 500.0):
        with _patch_all_layers():
            await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is not None
    assert trade.quantity == 2


# ===========================================================================
# IV filter (Layer 6)
# ===========================================================================

@pytest.mark.asyncio
async def test_iv_below_threshold_allows_entry(db):
    """ATM IV 80% < 150% threshold → entry proceeds."""
    client = _make_client(atm_iv=0.80)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


@pytest.mark.asyncio
async def test_iv_exactly_at_threshold_allows_entry(db):
    """ATM IV == threshold (1.50) → code uses strict > so this must NOT block."""
    from app.config import settings
    client = _make_client(atm_iv=settings.iv_max_threshold)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


@pytest.mark.asyncio
async def test_iv_above_threshold_blocks_entry(db):
    """ATM IV 200% > 150% threshold → entry blocked, no DB record."""
    client = _make_client(atm_iv=2.00)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


@pytest.mark.asyncio
async def test_iv_none_allows_entry(db):
    """get_atm_iv returns None (no IV data) → entry must NOT be blocked."""
    client = _make_client(atm_iv=None)
    client.get_atm_iv = MagicMock(return_value=None)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


# ===========================================================================
# Trade record fields
# ===========================================================================

@pytest.mark.asyncio
async def test_trade_record_fields_populated(db):
    """Verify the created Trade has all required fields set."""
    client = _make_client(fill_price=2.35)
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is not None
    assert trade.symbol == "AAPL"
    assert trade.direction == Direction.CALL
    assert trade.status == TradeStatus.OPEN
    assert trade.entry_price is not None and trade.entry_price > 0
    assert trade.stop_price  is not None and trade.stop_price  < trade.entry_price
    assert trade.tp2_price   is not None and trade.tp2_price   > trade.entry_price
    assert trade.entry_time  is not None
    assert trade.quantity    >= 1


# ===========================================================================
# Layer 5 — regime gate (new single-SPY-confirmation logic)
# ===========================================================================
# The old gate required BOTH SPY AND the stock to be bearish/bullish.
# The new gate: SPY alone is enough to block.  Also: when trading the regime
# proxy itself (SPY), L5 is skipped (circular self-check).

@pytest.mark.asyncio
async def test_regime_bearish_blocks_call_on_non_spy(db):
    """SPY bearish → CALL on AAPL must be blocked (single-confirmation rule)."""
    client = _make_client()
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "bearish")
    assert await _count_trades(db) == 0, (
        "Regime gate should block a CALL when SPY is bearish"
    )


@pytest.mark.asyncio
async def test_regime_bullish_blocks_put_on_non_spy(db):
    """SPY bullish → PUT on MSFT must be blocked."""
    # We need a PUT signal — patch check_entry_signal to return PUT
    from app.services.strategy import EntrySignal
    put_signal = EntrySignal(direction="PUT", current_price=300.0, vwap=300.5, trend="bearish")
    client = _make_client()
    with patch("app.services.scheduler.check_entry_signal",      return_value=put_signal), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle",    return_value=True), \
         patch("app.services.scheduler.check_vwap_slope",         return_value=True):
        await _attempt_entry(db, client, "MSFT", "bullish")
    assert await _count_trades(db) == 0, (
        "Regime gate should block a PUT when SPY is bullish"
    )


@pytest.mark.asyncio
async def test_regime_bearish_allows_put_on_non_spy(db):
    """SPY bearish → PUT on MSFT is fine (regime agrees with direction)."""
    from app.services.strategy import EntrySignal
    put_signal = EntrySignal(direction="PUT", current_price=300.0, vwap=300.5, trend="bearish")
    # Chain must contain a PUT contract so side_chain is non-empty
    put_contract = _option(strike=300.0, delta=-0.40, volume=100, ask=2.50, option_type="put")
    client = _make_client(chain=[put_contract])
    with patch("app.services.scheduler.check_entry_signal",      return_value=put_signal), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle",    return_value=True), \
         patch("app.services.scheduler.check_vwap_slope",         return_value=True):
        await _attempt_entry(db, client, "MSFT", "bearish")
    assert await _count_trades(db) == 1, (
        "SPY bearish should NOT block a PUT entry"
    )


@pytest.mark.asyncio
async def test_regime_gate_skipped_for_spy_itself(db):
    """
    When trading SPY (the regime proxy), L5 is skipped entirely.
    Even if SPY regime is bearish, a CALL on SPY must still reach contract
    selection (if other layers pass) — L1 handles the directional block,
    not L5.
    """
    from app.config import settings
    client = _make_client()
    # regime = "bearish" would normally block a CALL, but ticker == regime symbol
    with _patch_all_layers(), \
         patch.object(settings, "regime_gate_symbol", "SPY"), \
         patch.object(settings, "regime_gate_enabled", True):
        await _attempt_entry(db, client, "SPY", "bearish")
    # L1 produces a bullish signal (rising bars mock), so the trade proceeds
    assert await _count_trades(db) == 1, (
        "Regime gate must not block SPY trading on itself (circular check)"
    )


@pytest.mark.asyncio
async def test_regime_neutral_allows_all_directions(db):
    """regime=neutral → neither CALL nor PUT is blocked."""
    client = _make_client()
    with _patch_all_layers():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 1


# ===========================================================================
# Limit order entry (use_limit_orders=True)
#
# These tests enable limit orders and patch asyncio.sleep to be instant so
# the poll loop runs without real delays.
# ===========================================================================

@contextmanager
def _patch_layers_limit():
    """
    Like _patch_all_layers but with use_limit_orders=True and asyncio.sleep
    mocked to return immediately (avoids 2-second waits in the poll loop).
    """
    from app.config import settings
    with patch("app.services.scheduler.check_entry_signal",       return_value=_FAKE_SIGNAL), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle",    return_value=True), \
         patch("app.services.scheduler.check_vwap_slope",         return_value=True), \
         patch.object(settings, "use_limit_orders", True), \
         patch.object(settings, "limit_order_timeout_seconds", 10), \
         patch("app.services.scheduler.asyncio.sleep", AsyncMock(return_value=None)):
        yield


@pytest.mark.asyncio
async def test_limit_order_entry_uses_fill_price(db):
    """
    With use_limit_orders=True, the limit is placed at mid (bid+ask)/2.
    After fill confirmation, entry_price is updated to the *actual* fill price
    from Tradier (fill reconciliation).
    _option(): ask=2.50, bid=2.40, mid=2.45.
    _make_client() default fill_price=2.40 (typical bid execution).
    Entry must record 2.40 (actual fill), not 2.45 (limit mid-quote).
    """
    client = _make_client(order_status="filled",
                          fill_price=2.40,          # actual fill at bid
                          chain=[_option(ask=2.50)])  # mid=2.45
    with _patch_layers_limit():
        await _attempt_entry(db, client, "AAPL", "neutral")
    trade = await _get_trade(db)
    assert trade is not None
    assert trade.entry_price == pytest.approx(2.40, abs=0.001), (
        f"Entry should use actual fill price $2.40, got {trade.entry_price}"
    )


@pytest.mark.asyncio
async def test_limit_order_entry_fill_matches_mid(db):
    """
    When the actual fill price matches the limit mid-quote, entry_price = mid.
    (Tests the common case where the limit fills at exactly the posted price.)
    """
    client = _make_client(order_status="filled",
                          fill_price=2.45,          # fill exactly at mid
                          chain=[_option(ask=2.50)])  # mid=2.45
    with _patch_layers_limit():
        await _attempt_entry(db, client, "AAPL", "neutral")
    trade = await _get_trade(db)
    assert trade is not None
    assert trade.entry_price == pytest.approx(2.45, abs=0.001), (
        f"Entry should use actual fill price $2.45, got {trade.entry_price}"
    )


@pytest.mark.asyncio
async def test_limit_order_places_limit_not_market(db):
    """
    With use_limit_orders=True, place_option_order must be called with
    order_type='limit' and limit_price = mid_price.
    """
    client = _make_client(order_status="filled",
                          chain=[_option(ask=2.50)])  # mid=2.45
    with _patch_layers_limit():
        await _attempt_entry(db, client, "AAPL", "neutral")
    # First call is the buy; a broker-side stop order may follow it.
    call_kwargs = client.place_option_order.call_args_list[0].kwargs
    assert call_kwargs.get("order_type") == "limit"
    assert call_kwargs.get("limit_price") == pytest.approx(2.45, abs=0.001)


@pytest.mark.asyncio
async def test_broker_stop_placed_after_entry(db):
    """
    With broker_stop_enabled, a resting sell-to-close STOP order is placed
    after the entry fill, at the trade's stop price, and its order id is
    recorded on the Trade row.
    """
    from app.config import settings
    client = _make_client(fill_price=None, chain=[_option(ask=2.50)])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})

    with patch.object(settings, "broker_stop_enabled", True):
        with _patch_all_layers():
            await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is not None
    calls = client.place_option_order.call_args_list
    assert len(calls) == 2, "expected buy + broker stop"
    stop_kwargs = calls[1].kwargs
    assert stop_kwargs.get("side") == "sell_to_close"
    assert stop_kwargs.get("order_type") == "stop"
    assert stop_kwargs.get("stop_price") == pytest.approx(trade.stop_price, abs=0.001)
    assert stop_kwargs.get("quantity") == trade.quantity
    assert trade.stop_order_id == "buy001"   # mock returns the same order id


@pytest.mark.asyncio
async def test_broker_stop_disabled_no_extra_order(db):
    """broker_stop_enabled=False → only the buy order is placed."""
    from app.config import settings
    client = _make_client(fill_price=None, chain=[_option(ask=2.50)])
    client.get_order_status = AsyncMock(return_value={"status": "filled"})

    with patch.object(settings, "broker_stop_enabled", False):
        with _patch_all_layers():
            await _attempt_entry(db, client, "AAPL", "neutral")

    trade = await _get_trade(db)
    assert trade is not None
    assert len(client.place_option_order.call_args_list) == 1
    assert trade.stop_order_id is None


@pytest.mark.asyncio
async def test_limit_order_rejected_no_db_record(db):
    """Limit order rejected during poll → no trade created."""
    client = _make_client(order_status="rejected")
    with _patch_layers_limit():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0


@pytest.mark.asyncio
async def test_limit_order_unfilled_cancels_and_skips(db):
    """
    Limit order never fills (status stays 'pending') → cancel_order called
    after timeout, no trade created.
    """
    client = _make_client(order_status="pending")   # never "filled"
    with _patch_layers_limit():
        await _attempt_entry(db, client, "AAPL", "neutral")
    assert await _count_trades(db) == 0
    client.cancel_order.assert_called_once_with("buy001")


@pytest.mark.asyncio
async def test_limit_order_qty_uses_mid_for_sizing(db):
    """
    With use_limit_orders=True, qty is computed from mid_price.
    ask=2.00 → bid=1.90 → mid=1.95 → cost_per_contract=195
    budget=500 → int(500/195) = 2
    """
    from app.config import settings
    cheap = _option(ask=2.00, delta=0.40, volume=100)  # mid=1.95
    client = _make_client(order_status="filled", chain=[cheap])
    with _patch_layers_limit(), \
         patch.object(settings, "amount_per_trade", 500.0):
        await _attempt_entry(db, client, "AAPL", "neutral")
    trade = await _get_trade(db)
    assert trade is not None
    assert trade.quantity == 2
