"""
PUT Scalp mode ("PS", Jul 23 2026) regression tests.

Covers:
  Entry  — Trend (15m EMA bearish) + Thesis (below session VWAP beyond band)
           agreement opens a PS trade with fixed +8%/−7% brackets, half size,
           strategy_name "put_scalp"
  Entry  — blocked when trend is not bearish / thesis not below VWAP /
           PS cooldown active
  Scan   — calendar+clock guard (ghost-trade lesson) and enable toggle
  Manage — PS runner arms at +5% GAIN (not TP proximity) with 2% trail /
           +2% floor; stays dormant below the arm threshold
  Exit   — hard stop suppressed for the PS 6-min hold window (override param)
"""
import os, pytest, pytest_asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_ps_test.db"

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Direction, ExitReason, Trade, TradeStatus
from app.config import settings
from app.services import scheduler as sched
from app.services.scheduler import (
    _attempt_put_scalp,
    manage_open_trades,
    scan_for_put_scalp,
)
from app.services.strategy import check_exit_conditions
from app.services.tradier import OptionQuote, OrderResult, Quote
from tests.conftest import make_bar, rising_bars, falling_bars


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_env():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_ps_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        sched._bar_cache.clear()
        yield session, Session
    await engine.dispose()


def _put_option(strike=185.0, delta=0.40, volume=100, ask=2.50, bid=None):
    return OptionQuote(
        symbol=f"AAPL240119P{int(strike * 1000):08d}",
        underlying="AAPL",
        expiration_date="2099-12-31",
        option_type="put",
        strike=strike,
        bid=bid if bid is not None else round(ask - 0.10, 2),
        ask=ask,
        last=round(ask - 0.05, 2),
        volume=volume,
        open_interest=500,
        delta=delta,
    )


def _client(chain=None, opt_bid=None, opt_ask=None):
    c = MagicMock()
    c.place_option_order = AsyncMock(return_value=OrderResult(order_id="ps1", status="ok"))
    c.get_order_status   = AsyncMock(return_value={"status": "filled"})
    c.get_fill_price     = AsyncMock(return_value=None)
    c.cancel_order       = AsyncMock(return_value={"status": "ok"})
    c.modify_order       = AsyncMock(return_value={"status": "ok"})
    c.get_positions      = AsyncMock(return_value=[])
    c.get_option_expirations = AsyncMock(return_value=["2099-12-31"])
    c.get_options_chain  = AsyncMock(return_value=chain or [_put_option()])
    c.get_quote = AsyncMock(return_value=Quote(
        symbol="AAPL", last=184.0, bid=183.9, ask=184.1, volume=1_000_000
    ))
    if opt_bid is not None:
        c.get_option_quote = AsyncMock(return_value=Quote(
            symbol="OPT", last=opt_bid, bid=opt_bid,
            ask=opt_ask or opt_bid + 0.10, volume=1000,
        ))
    return c


# Bearish world: falling 15m trend, 1m session well below its own VWAP.
_BEAR_15M = falling_bars(200.0, 40, 0.5)
_BEAR_1M  = falling_bars(190.0, 30, 0.2)
# Bullish/flat variants for the block tests
_BULL_15M = rising_bars(180.0, 40, 0.5)
_RISE_1M  = rising_bars(180.0, 30, 0.2)


@contextmanager
def _ps_entry_env(bars_1m, bars_15m, bars_5m=None):
    async def fake_get_bars(_c, ticker, interval, lookback_days):
        if interval == "1min":
            return bars_1m
        if interval == "5min":
            # default: red 5m bars so the no-green guard passes
            return bars_5m if bars_5m is not None else falling_bars(190.0, 10, 0.3)
        return bars_15m

    async def no_energy_block(*a, **k):
        return False

    with patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch("app.services.scheduler._symbol_energy_blocks", side_effect=no_energy_block), \
         patch("app.services.scheduler.check_momentum_candle", return_value=True), \
         patch.object(settings, "put_scalp_enabled", True), \
         patch.object(settings, "use_limit_orders", False), \
         patch.object(settings, "broker_stop_enabled", False), \
         patch.object(settings, "broker_tp_enabled", False), \
         patch.object(settings, "amount_per_trade", 500.0), \
         patch.object(settings, "option_min_premium", 1.0), \
         patch.object(settings, "option_min_volume", 10), \
         patch.object(settings, "put_scalp_risk_per_trade", 75.0), \
         patch.object(settings, "put_scalp_sl_pct", 0.07), \
         patch.object(settings, "put_scalp_tp_pct", 0.08), \
         patch.object(settings, "put_scalp_max_spread_pct", 0.08), \
         patch.object(settings, "put_scalp_max_open", 1), \
         patch.object(settings, "put_scalp_cooldown_minutes", 30), \
         patch.object(settings, "put_scalp_no_green_5m_enabled", True), \
         patch.object(settings, "put_scalp_max_bounce_from_low_pct", 0.005), \
         patch.object(settings, "max_losses_per_symbol_per_day", 0), \
         patch.object(settings, "max_trades_per_symbol_per_day", 0):
        yield


def _mk_ps_trade(entry=2.00, stop=1.86, tp2=2.16, minutes_ago=30,
                 status=TradeStatus.OPEN, exit_minutes_ago=None,
                 exit_reason=None, symbol="AAPL"):
    t = Trade(
        symbol=symbol,
        option_symbol="AAPL240119P00185000",
        direction=Direction.PUT,
        strategy_name="put_scalp",
        tradier_order_id="ps-buy",
        quantity=2,
        remaining_qty=2,
        entry_price=entry,
        entry_time=datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago),
        underlying_entry=185.0,
        vwap_at_entry=187.0,
        stop_price=stop,
        tp1_price=tp2,
        tp2_price=tp2,
        original_stop_price=stop,
        status=status,
    )
    if exit_minutes_ago is not None:
        t.exit_time = datetime.now(tz=timezone.utc) - timedelta(minutes=exit_minutes_ago)
        t.exit_reason = exit_reason or ExitReason.STOP
        t.exit_price = stop
    return t


async def _one_trade(session, strategy="put_scalp") -> Trade | None:
    res = await session.execute(
        select(Trade).where(Trade.strategy_name == strategy,
                            Trade.status == TradeStatus.OPEN)
    )
    return res.scalars().first()


# ===========================================================================
# Entry
# ===========================================================================

@pytest.mark.asyncio
async def test_ps_entry_on_trend_thesis_agreement(db_env):
    """Bearish 15m trend + last price below session VWAP band → PS PUT opened
    with fixed +8%/−7% brackets sized at the PS half-risk."""
    db, _ = db_env
    client = _client()
    with _ps_entry_env(_BEAR_1M, _BEAR_15M):
        await _attempt_put_scalp(db, client, "AAPL")

    t = await _one_trade(db)
    assert t is not None
    assert t.strategy_name == "put_scalp"
    assert t.direction == Direction.PUT
    # market order path → entry at ask 2.50
    assert t.entry_price == pytest.approx(2.50)
    # same float expression as the code (1 − 0.07 ≠ 0.93 in float)
    assert t.stop_price == pytest.approx(round(2.50 * (1 - 0.07), 2))
    assert t.tp2_price == pytest.approx(round(2.50 * (1 + 0.08), 2))
    assert t.original_stop_price == t.stop_price
    # sizing: risk/contract = 250 × 0.07 = $17.5 → 75/17.5 = 4, budget 500/250 = 2
    assert t.quantity == 2


@pytest.mark.asyncio
async def test_ps_blocked_when_trend_not_bearish(db_env):
    db, _ = db_env
    client = _client()
    with _ps_entry_env(_BEAR_1M, _BULL_15M):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_ps_blocked_when_thesis_not_below_vwap(db_env):
    """Bearish trend but price at/above session VWAP → state disagreement."""
    db, _ = db_env
    client = _client()
    with _ps_entry_env(_RISE_1M, _BEAR_15M):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_ps_cooldown_blocks_reentry(db_env):
    """The signal is a STATE — after any PS exit the symbol must pause, or
    the mode would machine-gun re-entries all afternoon."""
    db, _ = db_env
    db.add(_mk_ps_trade(status=TradeStatus.CLOSED, exit_minutes_ago=5))
    await db.commit()

    client = _client()
    with _ps_entry_env(_BEAR_1M, _BEAR_15M):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_ps_wide_spread_blocked(db_env):
    """PS has its own tighter spread gate — 12% would eat the whole 8% TP."""
    db, _ = db_env
    # bid 2.10 / ask 2.50 → spread 0.40 on mid 2.30 ≈ 17% > 8%
    client = _client(chain=[_put_option(ask=2.50, bid=2.10)])
    with _ps_entry_env(_BEAR_1M, _BEAR_15M):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_ps_blocked_by_green_5m_bar(db_env):
    """AMZN #176 / INTC #181 regression: a green last completed 5-min candle
    means a bounce is underway — PS must not short into it."""
    db, _ = db_env
    client = _client()
    green_5m = rising_bars(184.0, 10, 0.3)
    with _ps_entry_env(_BEAR_1M, _BEAR_15M, bars_5m=green_5m):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_ps_blocked_when_price_bounced_off_low(db_env):
    """Price >0.5% above the session low = stale breakdown — blocked even
    with trend/thesis still bearish."""
    db, _ = db_env
    # falling to ~184.2, then a pop to 185.5 (+0.7% off the low) — still
    # below VWAP (~187) beyond the band, so trend+thesis remain "true".
    bounced = falling_bars(190.0, 30, 0.2) + [make_bar(185.5, open_=185.6)]
    client = _client()
    with _ps_entry_env(bounced, _BEAR_15M):
        await _attempt_put_scalp(db, client, "AAPL")
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


# ===========================================================================
# Scan guards
# ===========================================================================

@pytest.mark.asyncio
async def test_ps_scan_blocked_when_market_closed():
    """Ghost-trade lesson: clock AND calendar must both pass."""
    client = MagicMock()
    with patch.object(settings, "put_scalp_enabled", True), \
         patch("app.services.scheduler.is_in_trading_window", return_value=True), \
         patch("app.services.scheduler.is_market_open", return_value=False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client):
        await scan_for_put_scalp()
    client.get_option_expirations.assert_not_called()


@pytest.mark.asyncio
async def test_ps_scan_noop_when_disabled():
    client = MagicMock()
    with patch.object(settings, "put_scalp_enabled", False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client):
        await scan_for_put_scalp()
    client.get_option_expirations.assert_not_called()


# ===========================================================================
# Manage — PS runner overrides
# ===========================================================================

@contextmanager
def _ps_manage_env(Session, client, runner_ok=True):
    async def fake_get_bars(_c, ticker, interval, lookback_days):
        return falling_bars(185.0, 10, 0.05)

    with patch("app.services.scheduler.is_market_open", return_value=True), \
         patch("app.services.scheduler.is_past_cutoff", return_value=False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client), \
         patch("app.services.scheduler.AsyncSessionLocal", Session), \
         patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch("app.services.scheduler.should_activate_runner",
               MagicMock(return_value=runner_ok)) as sar, \
         patch.object(settings, "broker_stop_enabled", False), \
         patch.object(settings, "broker_tp_enabled", False), \
         patch.object(settings, "runner_mode_enabled", True), \
         patch.object(settings, "put_scalp_runner_arm_pct", 0.05), \
         patch.object(settings, "put_scalp_runner_trail_pct", 0.02), \
         patch.object(settings, "put_scalp_runner_floor_lock_pct", 0.02), \
         patch.object(settings, "put_scalp_stop_min_hold_minutes", 6), \
         patch.object(settings, "signal_conflict_exit_enabled", False), \
         patch.object(settings, "urgent_exit_limit_enabled", False), \
         patch.object(settings, "struct_disable_vwap_break", True):
        yield sar


@pytest.mark.asyncio
async def test_ps_runner_arms_at_gain_threshold(db_env):
    """bid 2.11 = +5.5% over entry 2.00 (arm = +5%) → runner on, TP waived,
    stop = max(mid × 0.98, entry × 1.02) = 2.12."""
    db, Session = db_env
    trade = _mk_ps_trade()          # entry 2.00, tp2 2.16
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.11, opt_ask=2.21)      # mid 2.16
    with _ps_manage_env(Session, client) as sar:
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is True
    assert trade.tp2_price is None
    assert trade.tp1_price == pytest.approx(2.16)
    assert trade.stop_price == pytest.approx(round(2.16 * 0.98, 2))
    assert trade.status == TradeStatus.OPEN
    # PS bypasses the TP-proximity check via proximity_pct=1.0
    assert sar.call_args.kwargs.get("proximity_pct") == 1.0


@pytest.mark.asyncio
async def test_ps_runner_dormant_below_gain_threshold(db_env):
    """bid 2.05 = +2.5% < +5% arm → no runner, brackets untouched."""
    db, Session = db_env
    trade = _mk_ps_trade()
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.05, opt_ask=2.09)
    with _ps_manage_env(Session, client) as sar:
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is False
    assert trade.tp2_price == pytest.approx(2.16)
    sar.assert_not_called()
    assert trade.status == TradeStatus.OPEN


# ===========================================================================
# Exit — PS stop min-hold override
# ===========================================================================

def test_ps_stop_min_hold_override():
    """Stop suppressed at 4 min into the 6-min PS hold, fires after it."""
    now = datetime.now(tz=timezone.utc)
    common = dict(
        direction="PUT",
        entry_price=2.50,
        current_option_price=2.20,   # bid
        stop_eval_price=2.20,        # mid below stop 2.33
        stop_price=2.33,
        tp1_price=2.70,
        tp2_price=2.70,
        tp1_hit=False,
        vwap_at_entry=0,
        current_underlying=184.0,
        remaining_qty=2,
        now=now,
        stop_min_hold_minutes=6,
    )
    assert check_exit_conditions(
        **common, entry_time=now - timedelta(minutes=4)
    ) is None

    cond = check_exit_conditions(
        **common, entry_time=now - timedelta(minutes=7)
    )
    assert cond is not None
    assert cond.reason in ("STOP", "TRAILING_STOP")
