"""
End-to-end tests for the July 2026 redesign, exercising the REAL scheduler
entry/manage code paths against a fully mocked Tradier client:

  S1 entry   — structural levels stored on the trade, sizing from the
               structural stop, R/R gate skips no-room setups
  S1 manage  — runner activation (TP waived, stop raised, broker TP
               cancelled), runner trail exit labeled RUNNER
  S2 manage  — structure exit (1-min closes through 5m-EMA9) closes the
               trade; runner activation near TP with momentum
  S2 entry   — strict PUT 15-min gate blocks, PUT kill switch blocks
  Chop gate  — blocks on low range/ATR ratio, passes on trend days
"""
import os, pytest, pytest_asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_e2e_test.db"

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Direction, ExitReason, Strategy, Trade, TradeStatus
from app.config import settings
from app.services import scheduler as sched
from app.services.scheduler import (
    _attempt_entry,
    _attempt_entry_s2,
    _manage_s2_trade,
    manage_open_trades,
)
from app.services.strategy import EntrySignal
from app.services.tradier import OptionQuote, OrderResult, Quote
from tests.conftest import make_bar, rising_bars, falling_bars


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_env():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_e2e_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        session.add(Strategy(name="vwap_pullback", enabled=True))
        await session.commit()
        # reset scheduler module caches between tests
        sched._bar_cache.clear()
        sched._chop_cache = None
        sched._atr_cache.clear()
        yield session, Session
    await engine.dispose()


def _option(strike=150.0, delta=0.40, volume=100, ask=2.50, option_type="call"):
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


def _client(bars_1m=None, opt_bid=None, opt_ask=None, chain=None, fill=2.40):
    c = MagicMock()
    c.place_option_order = AsyncMock(return_value=OrderResult(order_id="ord1", status="ok"))
    c.get_order_status   = AsyncMock(return_value={"status": "filled"})
    c.get_fill_price     = AsyncMock(return_value=fill)
    c.cancel_order       = AsyncMock(return_value={"status": "ok"})
    c.modify_order       = AsyncMock(return_value={"status": "ok"})
    c.get_positions      = AsyncMock(return_value=[])
    c.get_option_expirations = AsyncMock(return_value=["2099-12-31"])
    c.get_options_chain  = AsyncMock(return_value=chain or [_option()])
    c.get_atm_iv         = MagicMock(return_value=0.50)
    c.get_intraday_bars  = AsyncMock(return_value=bars_1m or rising_bars(150.0, 30, 0.05))
    c.get_daily_bars     = AsyncMock(return_value=[])
    c.get_quote = AsyncMock(return_value=Quote(
        symbol="AAPL", last=150.0, bid=149.9, ask=150.1, volume=1_000_000
    ))
    if opt_bid is not None:
        c.get_option_quote = AsyncMock(return_value=Quote(
            symbol="OPT", last=opt_bid, bid=opt_bid, ask=opt_ask or opt_bid + 0.10,
            volume=1000,
        ))
    return c


_SIGNAL = EntrySignal(direction="CALL", current_price=150.0, vwap=149.8, trend="bullish")

from contextlib import contextmanager

@contextmanager
def _s1_layers(structural=True):
    with patch("app.services.scheduler.check_entry_signal",        return_value=_SIGNAL), \
         patch("app.services.scheduler.check_bounce_confirmation", return_value=True), \
         patch("app.services.scheduler.check_momentum_candle",     return_value=True), \
         patch("app.services.scheduler.check_vwap_slope",          return_value=True), \
         patch.object(settings, "use_limit_orders", False), \
         patch.object(settings, "broker_stop_enabled", False), \
         patch.object(settings, "broker_tp_enabled", False), \
         patch.object(settings, "amount_per_trade", 500.0), \
         patch.object(settings, "risk_per_trade", 150.0), \
         patch.object(settings, "structural_levels_enabled", structural), \
         patch.object(settings, "struct_min_stop_pct", 0.08), \
         patch.object(settings, "struct_max_stop_pct", 0.30), \
         patch.object(settings, "struct_min_reward_risk", 1.2), \
         patch.object(settings, "struct_stop_buffer_pct", 0.001):
        yield


async def _one_trade(session) -> Trade | None:
    res = await session.execute(select(Trade))
    return res.scalars().first()


def _mk_trade(strategy="vwap_pullback", direction=Direction.CALL, entry=2.00,
              stop=1.62, tp2=2.50, minutes_ago=30, runner=False, symbol="AAPL"):
    return Trade(
        symbol=symbol,
        option_symbol="AAPL240119C00150000",
        direction=direction,
        strategy_name=strategy,
        tradier_order_id="buy1",
        quantity=2,
        remaining_qty=2,
        entry_price=entry,
        entry_time=datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago),
        underlying_entry=150.0,
        vwap_at_entry=149.8,
        stop_price=stop,
        tp1_price=None,
        tp2_price=tp2,
        status=TradeStatus.OPEN,
        runner_mode=runner,
    )


# ===========================================================================
# S1 entry — structural levels
# ===========================================================================

@pytest.mark.asyncio
async def test_s1_entry_stores_structural_levels(db_env):
    """
    Bars 150.00→151.45 rising, VWAP 149.8, entry price 150.0, ask 2.50, delta 0.40.
    stop_u = vwap×0.999 = 149.6502 → raw risk 5.6% → clamped to 8% → stop 2.30
    target_u = session high 151.45 → tp = 2.50 + 0.4×1.45 = 3.08
    qty: budget int(500/250)=2, risk int(150/(250×0.08))=7 → 2
    """
    db, _ = db_env
    client = _client()
    with _s1_layers():
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    t = await _one_trade(db)
    assert t is not None
    assert t.stop_price == pytest.approx(2.30)
    assert t.tp2_price == pytest.approx(3.08)
    assert t.quantity == 2


@pytest.mark.asyncio
async def test_s1_entry_skipped_when_no_room_to_target(db_env):
    """Session high (149.45) below entry price (150.0) → no room → R/R gate skips."""
    db, _ = db_env
    client = _client(bars_1m=rising_bars(148.0, 30, 0.05))  # highs max 149.43
    with _s1_layers():
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_s1_entry_falls_back_to_pct_levels_without_delta(db_env):
    """No delta on the contract → percentage levels, not a skipped trade."""
    db, _ = db_env
    client = _client(chain=[_option(delta=None)])
    with _s1_layers(), \
         patch.object(settings, "stop_loss_pct", 0.20), \
         patch.object(settings, "take_profit_pct", 0.25):
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)

    t = await _one_trade(db)
    assert t is not None
    assert t.stop_price == pytest.approx(2.00)   # 2.50 × 0.80
    assert t.tp2_price == pytest.approx(3.12)    # round(2.50 × 1.25, 2) — cents


# ===========================================================================
# S1 manage — runner mode end-to-end via manage_open_trades()
# ===========================================================================

@contextmanager
def _manage_env(Session, client, bars_1m):
    async def fake_get_bars(_c, ticker, interval, lookback_days):
        return bars_1m
    with patch("app.services.scheduler.is_market_open", return_value=True), \
         patch("app.services.scheduler.is_past_cutoff", return_value=False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client), \
         patch("app.services.scheduler.AsyncSessionLocal", Session), \
         patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch.object(settings, "broker_stop_enabled", False), \
         patch.object(settings, "broker_tp_enabled", False), \
         patch.object(settings, "runner_mode_enabled", True), \
         patch.object(settings, "runner_proximity_pct", 0.05), \
         patch.object(settings, "runner_trail_pct", 0.08), \
         patch.object(settings, "runner_floor_lock_pct", 0.03), \
         patch.object(settings, "signal_conflict_exit_enabled", False), \
         patch.object(settings, "urgent_exit_limit_enabled", False), \
         patch.object(settings, "structural_levels_enabled", True), \
         patch.object(settings, "struct_disable_vwap_break", True), \
         patch.object(settings, "stop_loss_pct", 0.19), \
         patch.object(settings, "quick_loss_max_minutes", 5):
        yield


@pytest.mark.asyncio
async def test_s1_runner_activates_near_tp_with_momentum(db_env):
    """bid 2.45 within 5% of TP 2.50 + momentum candle → TP waived, stop raised."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.50)
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.45, opt_ask=2.55)          # mid 2.50
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is True
    assert trade.tp2_price is None
    assert trade.tp1_price == pytest.approx(2.50)          # original target kept
    # runner floor = mid 2.50 × 0.92 = 2.30 (also ≥ standard trail 2.50×0.9=2.25)
    assert trade.stop_price == pytest.approx(2.30)
    assert trade.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_runner_never_waives_manual_tp(db_env):
    """GOOGL #140 regression: a HUMAN-set TP must survive runner conditions."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.50)
    trade.tp_manual = True
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.45, opt_ask=2.55)          # in the runner zone
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is False
    assert trade.tp2_price == pytest.approx(2.50)          # target untouched
    assert trade.status == TradeStatus.OPEN                # bid 2.45 < 2.50


@pytest.mark.asyncio
async def test_runner_floor_never_below_entry(db_env):
    """GOOGL #140 regression: activation floor clamps to entry+1%."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.20, stop=1.62)                # tight TP near entry
    db.add(trade); await db.commit()

    # bid 2.12 ≥ 2.20×0.95; mid 2.14 → plain floor 1.97 < entry → clamp 2.00×1.03=2.06
    client = _client(opt_bid=2.12, opt_ask=2.16)
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is True
    assert trade.stop_price == pytest.approx(2.06)         # entry × (1 + floor lock 3%)


@pytest.mark.asyncio
async def test_s1_tp_fires_normally_when_momentum_faded(db_env):
    """At the TP but last completed candle is red → runner NOT activated, TP2 exit."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.50)
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.52, opt_ask=2.60)
    with _manage_env(Session, client, falling_bars(150.0, 10, 0.05)):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.runner_mode is False
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.TP2


@pytest.mark.asyncio
async def test_s1_runner_trail_exit_labeled_runner(db_env):
    """Runner trade whose trail catches price → CLOSED with reason RUNNER."""
    db, Session = db_env
    trade = _mk_trade(tp2=None, runner=True, stop=2.30)
    trade.tp1_price = 2.50
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.26, opt_ask=2.32)           # mid 2.29 ≤ stop 2.30
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.RUNNER


# ===========================================================================
# S2 manage — structure exit + runner
# ===========================================================================

@contextmanager
def _s2_env(bars_5m, bars_1m):
    async def fake_get_bars(_c, ticker, interval, lookback_days):
        return bars_5m if interval == "5min" else bars_1m
    with patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch.object(settings, "runner_mode_enabled", True), \
         patch.object(settings, "runner_proximity_pct", 0.05), \
         patch.object(settings, "runner_trail_pct", 0.08), \
         patch.object(settings, "s2_structure_exit_enabled", True), \
         patch.object(settings, "s2_structure_exit_bars", 2), \
         patch.object(settings, "s2_structure_exit_margin_pct", 0.0005), \
         patch.object(settings, "s2_stop_loss_pct", 0.19), \
         patch.object(settings, "s2_stop_loss_min_hold_minutes", 0), \
         patch.object(settings, "s2_quick_loss_max_minutes", 5), \
         patch.object(settings, "s2_breakeven_pct", 0.15), \
         patch.object(settings, "s2_trail_pct", 0.20), \
         patch.object(settings, "s2_trail_from_current_pct", 0.08):
        yield


@pytest.mark.asyncio
async def test_s2_structure_exit_closes_trade(db_env):
    """1-min closes far below the 5m-EMA9 → STRUCT_EXIT close."""
    db, _ = db_env
    trade = _mk_trade(strategy="ema_cross", tp2=3.00)
    db.add(trade); await db.commit()

    bars_5m = rising_bars(150.0, 40, 0.2)     # EMA9 ends near ~156-157
    bars_1m = falling_bars(150.0, 10, 0.05)   # closes ≈149.6-150 « EMA9
    client = _client(opt_bid=2.10, opt_ask=2.20)
    with _s2_env(bars_5m, bars_1m):
        await _manage_s2_trade(db, client, trade, cutoff=False)

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.STRUCT_EXIT


@pytest.mark.asyncio
async def test_s2_runner_activates_and_stays_open(db_env):
    """bid near TP with momentum → runner on, TP disarmed, trade stays open."""
    db, _ = db_env
    trade = _mk_trade(strategy="ema_cross", tp2=3.00)
    db.add(trade); await db.commit()

    bars_5m = rising_bars(150.0, 40, 0.2)
    # 1-min bars ABOVE the 5m EMA9 (no structure exit) with momentum
    bars_1m = rising_bars(158.0, 10, 0.05)
    client = _client(opt_bid=2.90, opt_ask=3.00)          # bid ≥ 3.00×0.95
    with _s2_env(bars_5m, bars_1m):
        await _manage_s2_trade(db, client, trade, cutoff=False)

    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN
    assert trade.runner_mode is True
    assert trade.tp2_price is None
    assert trade.tp1_price == pytest.approx(3.00)
    # runner floor = bid 2.90 × 0.92 = 2.67 (trail may raise it further)
    assert trade.stop_price >= 2.67 - 1e-9


@pytest.mark.asyncio
async def test_s2_runner_trail_exit_labeled_runner(db_env):
    """Runner S2 trade: bid drops through the raised stop → RUNNER exit."""
    db, _ = db_env
    trade = _mk_trade(strategy="ema_cross", tp2=None, runner=True, stop=2.67)
    trade.tp1_price = 3.00
    db.add(trade); await db.commit()

    bars_5m = rising_bars(150.0, 40, 0.2)
    bars_1m = rising_bars(158.0, 10, 0.05)
    client = _client(opt_bid=2.60, opt_ask=2.70)          # bid 2.60 ≤ stop 2.67
    with _s2_env(bars_5m, bars_1m):
        await _manage_s2_trade(db, client, trade, cutoff=False)

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.RUNNER


# ===========================================================================
# S2 entry — PUT-side gates
# ===========================================================================

@pytest.mark.asyncio
async def test_s2_strict_put_gate_blocks_on_bullish_15m(db_env):
    """5-min says PUT but 15-min is bullish → strict gate blocks before contracts."""
    db, _ = db_env

    async def fake_get_bars(_c, ticker, interval, lookback_days):
        if interval == "5min":
            return falling_bars(155.0, 40, 0.2)   # PUT trend on 5-min
        if interval == "15min":
            return rising_bars(150.0, 40, 0.2)    # bullish 15-min
        return falling_bars(155.0, 30, 0.05)

    client = _client()
    with patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch("app.services.scheduler.check_ema_cross_freshness", return_value=True), \
         patch.object(settings, "s2_puts_enabled", True), \
         patch.object(settings, "s2_put_15m_strict", True):
        await _attempt_entry_s2(db, client, "AAPL")

    assert await _one_trade(db) is None
    client.get_option_expirations.assert_not_called()


@pytest.mark.asyncio
async def test_s2_put_kill_switch_blocks_all_puts(db_env):
    db, _ = db_env

    async def fake_get_bars(_c, ticker, interval, lookback_days):
        return falling_bars(155.0, 40, 0.2)

    client = _client()
    with patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch("app.services.scheduler.check_ema_cross_freshness", return_value=True), \
         patch.object(settings, "s2_puts_enabled", False):
        await _attempt_entry_s2(db, client, "AAPL")

    assert await _one_trade(db) is None
    client.get_option_expirations.assert_not_called()


# ===========================================================================
# Chop gate — scan-level blocking
# ===========================================================================

def _session_bars(rng: float, base=500.0, n=30):
    """n 1-min bars whose total session range is `rng`."""
    bars = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        c = base + rng * frac
        bars.append(make_bar(round(c, 2), open_=round(c - 0.01, 2)))
    return bars


@pytest.mark.asyncio
async def test_chop_gate_blocks_low_range_day(db_env):
    sched._chop_cache = None
    client = _client()
    qqq = _session_bars(rng=1.0)      # range 1.0 vs ATR 5.0 → ratio 0.2 < 0.5
    with patch.object(settings, "chop_filter_enabled", True), \
         patch.object(settings, "chop_min_range_ratio", 0.5), \
         patch.object(settings, "chop_filter_start_time", "00:00"), \
         patch("app.services.scheduler._get_daily_atr", new=AsyncMock(return_value=(5.0, 0.0))):
        blocked = await sched._chop_gate_blocks(client, qqq)
    assert blocked is True


# ===========================================================================
# Energy gate + contract quality floors (Jul 14 batch)
# ===========================================================================

@pytest.mark.asyncio
async def test_energy_gate_blocks_flat_symbol(db_env):
    """Symbol with tiny true range vs its own ATR is 'not in play' → block."""
    db, _ = db_env
    flat_bars = [make_bar(150.0 + 0.02 * (i % 3), open_=150.0)
                 for i in range(30)]  # ~6-cent range
    client = _client(bars_1m=flat_bars)
    with patch("app.services.scheduler._get_daily_atr",
               new=AsyncMock(return_value=(5.0, 150.0))), \
         patch.object(settings, "energy_min_range_ratio", 0.5):
        blocked = await sched._symbol_energy_blocks(
            client, "FLAT", flat_bars, check_floor=True, check_ceiling=False)
    assert blocked is True


@pytest.mark.asyncio
async def test_energy_gate_passes_in_play_symbol(db_env):
    db, _ = db_env
    moving = rising_bars(150.0, 30, 0.15)   # ~4.4-point range vs ATR 5.0
    client = _client(bars_1m=moving)
    with patch("app.services.scheduler._get_daily_atr",
               new=AsyncMock(return_value=(5.0, 150.0))), \
         patch.object(settings, "energy_min_range_ratio", 0.5):
        blocked = await sched._symbol_energy_blocks(
            client, "MOVER", moving, check_floor=True, check_ceiling=False)
    assert blocked is False


@pytest.mark.asyncio
async def test_vol_ceiling_blocks_too_hot_symbol(db_env):
    """HOOD regression: range 3.6× own ATR → too hot → block."""
    db, _ = db_env
    wild = rising_bars(100.0, 30, 0.6)      # ~17.4-point range vs ATR 5.0
    client = _client(bars_1m=wild)
    with patch("app.services.scheduler._get_daily_atr",
               new=AsyncMock(return_value=(5.0, 100.0))), \
         patch.object(settings, "energy_max_range_ratio", 2.5):
        blocked = await sched._symbol_energy_blocks(
            client, "HOT", wild, check_floor=False, check_ceiling=True)
    assert blocked is True


@pytest.mark.asyncio
async def test_vol_ceiling_passes_normal_trend(db_env):
    """A healthy 0.9× ATR trend day must pass the ceiling."""
    db, _ = db_env
    normal = rising_bars(100.0, 30, 0.15)   # ~4.4-point range vs ATR 5.0
    client = _client(bars_1m=normal)
    with patch("app.services.scheduler._get_daily_atr",
               new=AsyncMock(return_value=(5.0, 100.0))), \
         patch.object(settings, "energy_max_range_ratio", 2.5):
        blocked = await sched._symbol_energy_blocks(
            client, "OK", normal, check_floor=False, check_ceiling=True)
    assert blocked is False


@pytest.mark.asyncio
async def test_min_premium_blocks_cheap_contract(db_env):
    """F #146 regression: a $0.50 contract must be skipped at selection."""
    db, _ = db_env
    client = _client(chain=[_option(ask=0.50)])
    with _s1_layers(), patch.object(settings, "option_min_premium", 1.00):
        await _attempt_entry(db, client, "AAPL", "neutral", settings.vwap_band_pct)
    assert await _one_trade(db) is None
    client.place_option_order.assert_not_called()


# ===========================================================================
# Ghost-trade regressions (Jul 20): weekend guard + stale entry-order sweep
# ===========================================================================

@pytest.mark.asyncio
async def test_s2_scan_blocked_when_market_closed(db_env):
    """Sat Jul 18: S2 scanned inside its clock window on a CLOSED day and
    placed the ghost orders.  The scanner must now check the calendar."""
    from app.services.scheduler import scan_for_entries_s2
    client = _client()
    with patch.object(settings, "s2_enabled", True), \
         patch("app.services.scheduler.is_market_open", return_value=False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client):
        await scan_for_entries_s2()
    client.get_intraday_bars.assert_not_called()
    client.place_option_order.assert_not_called()


@pytest.mark.asyncio
async def test_startup_sweep_cancels_stale_buys_only(db_env):
    """Resting BUY orders are cancelled at startup; sell orders left alone."""
    from app.services.scheduler import cancel_stale_entry_orders
    client = _client()
    client.get_open_orders = AsyncMock(return_value=[
        {"id": 111, "side": "buy_to_open", "option_symbol": "NVDA260720P00202500", "quantity": 2},
        {"id": 222, "side": "sell_to_close", "option_symbol": "WMT260724P00114000", "quantity": 4},
        {"id": 333, "side": "buy_to_open", "option_symbol": "AMZN260720P00247500", "quantity": 2},
    ])
    with patch("app.services.scheduler.get_tradier_client", return_value=client):
        await cancel_stale_entry_orders()
    cancelled = {c.args[0] for c in client.cancel_order.call_args_list}
    assert cancelled == {"111", "333"}          # buys cancelled, sell untouched


# ===========================================================================
# External-close reconciliation on rejected sells (SOFI #137 regression)
# ===========================================================================

@pytest.mark.asyncio
async def test_rejected_sell_reconciles_when_position_gone(db_env):
    """
    Close attempt → Tradier REJECTS the sell → position absent from the
    account but an external fill exists → trade reconciled as MANUAL at the
    actual fill instead of staying stuck OPEN.
    """
    from app.services.scheduler import _close_trade
    from app.services.tradier import Position
    db, _ = db_env
    trade = _mk_trade(tp2=None, stop=1.62)
    trade.stop_order_id = "zombie1"
    db.add(trade); await db.commit()

    client = _client(opt_bid=1.50, opt_ask=1.60)
    client.get_order_status = AsyncMock(return_value={"status": "rejected"})
    client.get_fill_price   = AsyncMock(return_value=None)
    # Another position exists (positive confirmation the fetch worked),
    # but NOT this trade's contract.
    client.get_positions = AsyncMock(return_value=[
        Position(symbol="OTHER240119C00100000", quantity=1, cost_basis=100.0,
                 date_acquired=None),
    ])
    client.get_last_sell_fill = AsyncMock(return_value=2.60)

    with patch.object(settings, "exit_limit_orders_enabled", False):
        ok = await _close_trade(db, client, trade, ExitReason.CUTOFF, 1.50)

    assert ok is True
    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.MANUAL       # closed outside Ajoy
    assert trade.exit_price == pytest.approx(2.60)      # actual external fill
    client.cancel_order.assert_any_call("zombie1")      # zombie stop cleaned up


@pytest.mark.asyncio
async def test_rejected_sell_stays_open_when_position_still_live(db_env):
    """Position still exists at the broker → rejection keeps the trade OPEN."""
    from app.services.scheduler import _close_trade
    from app.services.tradier import Position
    db, _ = db_env
    trade = _mk_trade(tp2=None, stop=1.62)
    db.add(trade); await db.commit()

    client = _client(opt_bid=1.50, opt_ask=1.60)
    client.get_order_status = AsyncMock(return_value={"status": "rejected"})
    client.get_fill_price   = AsyncMock(return_value=None)
    client.get_positions = AsyncMock(return_value=[
        Position(symbol=trade.option_symbol, quantity=2, cost_basis=400.0,
                 date_acquired=None),
    ])

    with patch.object(settings, "exit_limit_orders_enabled", False):
        ok = await _close_trade(db, client, trade, ExitReason.CUTOFF, 1.50)

    assert ok is False
    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


# ===========================================================================
# Exit execution — limit at mid with market fallback
# ===========================================================================

@pytest.mark.asyncio
async def test_patient_exit_sells_via_limit_at_mid(db_env):
    """TP2 (patient) exit → limit sell at the mid, exit price = limit fill."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.50)
    db.add(trade); await db.commit()

    # bid 2.52 ≥ TP 2.50 → TP2 fires; mid = (2.52+2.60)/2 = 2.56
    client = _client(opt_bid=2.52, opt_ask=2.60, fill=2.56)
    with _manage_env(Session, client, falling_bars(150.0, 10, 0.05)), \
         patch.object(settings, "exit_limit_orders_enabled", True), \
         patch.object(settings, "exit_limit_timeout_seconds", 4):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.TP2
    sell_kwargs = client.place_option_order.call_args.kwargs
    assert sell_kwargs["order_type"] == "limit"
    assert sell_kwargs["limit_price"] == pytest.approx(2.56)
    assert trade.exit_price == pytest.approx(2.56)   # mid, not the 2.52 bid


@pytest.mark.asyncio
async def test_urgent_stop_exit_sells_via_market_when_marketable_disabled(db_env):
    """With the marketable-limit feature off, urgent exits go straight to market."""
    db, Session = db_env
    trade = _mk_trade(stop=1.62, tp2=2.50)           # 1.62 = original stop (−19%)
    db.add(trade); await db.commit()

    client = _client(opt_bid=1.55, opt_ask=1.65, fill=1.55)   # mid 1.60 ≤ stop
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)), \
         patch.object(settings, "exit_limit_orders_enabled", True), \
         patch.object(settings, "urgent_exit_limit_enabled", False), \
         patch.object(settings, "signal_conflict_exit_enabled", False):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.STOP
    sell_kwargs = client.place_option_order.call_args.kwargs
    assert sell_kwargs["order_type"] == "market"


@pytest.mark.asyncio
async def test_urgent_stop_uses_marketable_limit_at_bid_minus_3pct(db_env):
    """Jul 23: urgent exits place a limit at bid × 0.97 — caps velocity slippage."""
    db, Session = db_env
    trade = _mk_trade(stop=1.62, tp2=2.50)
    db.add(trade); await db.commit()

    client = _client(opt_bid=1.55, opt_ask=1.65, fill=1.52)
    with _manage_env(Session, client, rising_bars(150.0, 10, 0.05)), \
         patch.object(settings, "urgent_exit_limit_enabled", True), \
         patch.object(settings, "urgent_exit_limit_pct", 0.03), \
         patch.object(settings, "urgent_exit_limit_timeout_seconds", 4), \
         patch.object(settings, "signal_conflict_exit_enabled", False):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.STOP
    sell_kwargs = client.place_option_order.call_args.kwargs
    assert sell_kwargs["order_type"] == "limit"
    assert sell_kwargs["limit_price"] == pytest.approx(round(1.55 * 0.97, 2))


@pytest.mark.asyncio
async def test_s1_signal_fade_exit_when_trend_and_thesis_flip(db_env):
    """CALL held while 15-min trend turns bearish AND underlying sits below
    session VWAP beyond the band → SIGNAL_FADE via marketable limit,
    conflict timestamp stored."""
    db, Session = db_env
    trade = _mk_trade(stop=1.00, tp2=None)           # nothing else can fire
    db.add(trade); await db.commit()

    async def fake_get_bars(_c, ticker, interval, lookback_days):
        if interval == "15min":
            return falling_bars(160.0, 40, 0.3)      # bearish completed trend
        return rising_bars(152.0, 30, 0.01)          # session VWAP ≈ 152 » last 150.0

    client = _client(opt_bid=2.10, opt_ask=2.20, fill=2.05)
    with patch("app.services.scheduler.is_market_open", return_value=True), \
         patch("app.services.scheduler.is_past_cutoff", return_value=False), \
         patch("app.services.scheduler.get_tradier_client", return_value=client), \
         patch("app.services.scheduler.AsyncSessionLocal", Session), \
         patch("app.services.scheduler._get_bars", side_effect=fake_get_bars), \
         patch.object(settings, "broker_stop_enabled", False), \
         patch.object(settings, "broker_tp_enabled", False), \
         patch.object(settings, "runner_mode_enabled", False), \
         patch.object(settings, "signal_conflict_exit_enabled", True), \
         patch.object(settings, "urgent_exit_limit_enabled", True), \
         patch.object(settings, "urgent_exit_limit_timeout_seconds", 4), \
         patch.object(settings, "struct_disable_vwap_break", True), \
         patch.object(settings, "quick_loss_pct", 0.0):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SIGNAL_FADE
    assert trade.signal_conflict_time is not None


@pytest.mark.asyncio
async def test_s2_signal_fade_when_opposite_trend_validates(db_env):
    """S2 CALL while the 5-min filter fully validates PUT → SIGNAL_FADE."""
    db, _ = db_env
    trade = _mk_trade(strategy="ema_cross", stop=1.00, tp2=None, symbol="ZZFADE")
    db.add(trade); await db.commit()

    bars_5m = falling_bars(155.0, 40, 0.3)           # PUT-valid: falling, below VWAP
    bars_1m = falling_bars(150.0, 10, 0.05)
    client = _client(opt_bid=2.10, opt_ask=2.20, fill=2.05)
    with patch("app.services.scheduler._get_bars",
               side_effect=lambda _c, t, interval, lookback_days:
               bars_5m if interval == "5min" else bars_1m), \
         patch.object(settings, "signal_conflict_exit_enabled", True), \
         patch.object(settings, "urgent_exit_limit_enabled", True), \
         patch.object(settings, "urgent_exit_limit_timeout_seconds", 4), \
         patch.object(settings, "s2_quick_loss_pct", 0.0):
        await _manage_s2_trade(db, client, trade, cutoff=False)

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.SIGNAL_FADE
    assert trade.signal_conflict_time is not None


@pytest.mark.asyncio
async def test_exit_limit_timeout_falls_back_to_market(db_env):
    """Unfilled exit limit → cancelled → market sell closes the trade."""
    db, Session = db_env
    trade = _mk_trade(tp2=2.50)
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.52, opt_ask=2.60)
    client.place_option_order = AsyncMock(side_effect=[
        OrderResult(order_id="L1", status="ok"),   # limit — never fills
        OrderResult(order_id="M1", status="ok"),   # market fallback
    ])

    def _status(order_id):
        if order_id == "L1":
            return {"status": "canceled", "exec_quantity": 0, "avg_fill_price": 0}
        return {"status": "filled", "avg_fill_price": 2.52}
    client.get_order_status = AsyncMock(side_effect=_status)
    client.get_fill_price = AsyncMock(
        side_effect=lambda oid: 2.52 if oid == "M1" else None
    )

    with _manage_env(Session, client, falling_bars(150.0, 10, 0.05)), \
         patch.object(settings, "exit_limit_orders_enabled", True), \
         patch.object(settings, "exit_limit_timeout_seconds", 0):
        await manage_open_trades()

    await db.refresh(trade)
    assert trade.status == TradeStatus.CLOSED
    calls = client.place_option_order.call_args_list
    assert calls[0].kwargs["order_type"] == "limit"
    assert calls[1].kwargs["order_type"] == "market"
    assert trade.exit_price == pytest.approx(2.52)
    client.cancel_order.assert_any_call("L1")


@pytest.mark.asyncio
async def test_exit_limit_partial_fill_books_slice_exactly(db_env):
    """2/5 filled on the limit → slice booked into pnl, market-sell 3, no double-sell."""
    db, _ = db_env
    from app.services.scheduler import _execute_exit_sell
    trade = _mk_trade(tp2=3.00)
    trade.quantity = 5
    trade.remaining_qty = 5
    db.add(trade); await db.commit()

    client = _client(opt_bid=2.52, opt_ask=2.60)
    client.place_option_order = AsyncMock(side_effect=[
        OrderResult(order_id="L1", status="ok"),
        OrderResult(order_id="M1", status="ok"),
    ])

    def _status(order_id):
        if order_id == "L1":
            return {"status": "canceled", "exec_quantity": 2, "avg_fill_price": 2.56}
        return {"status": "filled", "avg_fill_price": 2.50}
    client.get_order_status = AsyncMock(side_effect=_status)

    with patch.object(settings, "exit_limit_orders_enabled", True), \
         patch.object(settings, "exit_limit_timeout_seconds", 0):
        result = await _execute_exit_sell(db, client, trade, 5, ExitReason.TP2)

    assert result.failed is False and result.already_flat is False
    assert result.order.order_id == "M1"
    await db.refresh(trade)
    assert trade.remaining_qty == 3
    # partial slice: (2.56 − 2.00) × 2 × 100 = $112
    assert trade.pnl == pytest.approx(112.0)
    assert client.place_option_order.call_args_list[1].kwargs["quantity"] == 3


# ===========================================================================
# Broker disaster stop — buffered below the bot stop
# ===========================================================================

@pytest.mark.asyncio
async def test_broker_stop_placed_with_disaster_buffer(db_env):
    """Broker stop rests broker_stop_buffer_pct BELOW the bot's working stop."""
    from app.services.scheduler import _place_broker_stop
    db, _ = db_env
    trade = _mk_trade(stop=2.00, tp2=3.00)
    db.add(trade); await db.commit()

    client = _client()
    with patch.object(settings, "broker_stop_buffer_pct", 0.08):
        await _place_broker_stop(db, client, trade)

    kwargs = client.place_option_order.call_args.kwargs
    assert kwargs["order_type"] == "stop"
    assert kwargs["stop_price"] == pytest.approx(1.84)   # 2.00 × 0.92
    assert trade.stop_order_id == "ord1"


def test_broker_stop_price_floor():
    from app.services.scheduler import _broker_stop_price
    with patch.object(settings, "broker_stop_buffer_pct", 0.08):
        assert _broker_stop_price(2.00) == pytest.approx(1.84)
        assert _broker_stop_price(0.01) == 0.01   # never below a cent
    with patch.object(settings, "broker_stop_buffer_pct", 0.0):
        assert _broker_stop_price(2.00) == pytest.approx(2.00)


# ===========================================================================
# External-close reconciliation — actual fill parsing
# ===========================================================================

class TestExtractLastSellFill:
    from app.services.tradier import extract_last_sell_fill as _f

    def test_finds_latest_filled_sell(self):
        from app.services.tradier import extract_last_sell_fill
        orders = [
            {"status": "filled", "side": "sell_to_close", "option_symbol": "NFLX260710P00076000",
             "avg_fill_price": 0.95, "transaction_date": "2026-07-08T15:00:00Z"},
            {"status": "filled", "side": "sell_to_close", "option_symbol": "NFLX260710P00076000",
             "avg_fill_price": 0.88, "transaction_date": "2026-07-08T16:30:00Z"},
        ]
        assert extract_last_sell_fill(orders, "NFLX260710P00076000") == pytest.approx(0.88)

    def test_ignores_buys_unfilled_and_other_symbols(self):
        from app.services.tradier import extract_last_sell_fill
        orders = [
            {"status": "filled", "side": "buy_to_open", "option_symbol": "X", "avg_fill_price": 1.0},
            {"status": "canceled", "side": "sell_to_close", "option_symbol": "X", "avg_fill_price": 1.1},
            {"status": "filled", "side": "sell_to_close", "option_symbol": "Y", "avg_fill_price": 1.2},
        ]
        assert extract_last_sell_fill(orders, "X") is None

    def test_empty_and_malformed(self):
        from app.services.tradier import extract_last_sell_fill
        assert extract_last_sell_fill([], "X") is None
        assert extract_last_sell_fill(
            [{"status": "filled", "side": "sell", "symbol": "X", "avg_fill_price": "bad"}], "X"
        ) is None


@pytest.mark.asyncio
async def test_chop_gate_passes_trend_day(db_env):
    sched._chop_cache = None
    client = _client()
    qqq = _session_bars(rng=4.0)      # ratio 0.8 ≥ 0.5
    with patch.object(settings, "chop_filter_enabled", True), \
         patch.object(settings, "chop_min_range_ratio", 0.5), \
         patch.object(settings, "chop_filter_start_time", "00:00"), \
         patch("app.services.scheduler._get_daily_atr", new=AsyncMock(return_value=(5.0, 0.0))):
        blocked = await sched._chop_gate_blocks(client, qqq)
    assert blocked is False
