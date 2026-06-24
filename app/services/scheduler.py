"""
APScheduler background tasks.

Two jobs run while the market is open:
  scan_for_entries   : every 60 s — run the full 8-gate entry stack for each symbol
  manage_open_trades : every 30 s — check exit conditions for open trades

Entry gate order
----------------
Pre-entry guards (DB / time checks — fast, no API calls):
  G1  Trading hours window
  G2  Daily P&L loss limit
  G3  Max concurrent open trades
  G4  Per-symbol open trade already exists
  G5  Per-symbol daily loss cap        (new)
  G6  Cooldown after STOP / VWAP_BREAK (new)

Signal layers (require market data):
  L1  check_entry_signal()   — trend_15min AND price_vs_vwap AND pullback_to_vwap
  L2  check_bounce_confirmation() — last N bars close on correct VWAP side
  L3  check_momentum_candle() — last completed bar is a momentum candle
  L4  check_vwap_slope()      — intraday VWAP slope must agree with direction
  L5  get_market_regime()     — SPY 15-min trend gate (cached, config-driven)

Post-selection filter:
  L6  IV filter               — skip if ATM IV > iv_max_threshold
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func as sqlfunc

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import ExitReason, Symbol, Trade, TradeStatus
from app.services.strategy import (
    completed_bars,
    check_entry_signal,
    check_bounce_confirmation,
    check_momentum_candle,
    check_vwap_slope,
    get_regime_from_vwap,
    get_adaptive_vwap_band,
    check_exit_conditions,
    compute_trailing_stop,
    compute_trade_levels,
    is_market_open,
    is_past_cutoff,
    is_in_trading_window,
)
from app.services.tradier import get_tradier_client
from app.services.strategy_ema import (
    check_5min_trend_filter,
    check_1min_ema_cross,
    check_s2_exit_conditions,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/New_York")

# ── Entry placement lock (Fix #1: MAX_OPEN_TRADES race) ──────────────────
# Serialises the final "re-check count → place buy order → commit" step so
# that concurrent per-symbol scans cannot all slip through the global cap
# check simultaneously.  The lock is held for only ~1 API round-trip per
# entry, so it does not meaningfully slow down the scan.
_entry_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

async def _get_daily_pnl(db) -> float:
    """
    Return today's combined P&L for the MAX_DAILY_LOSS gate (G2).

    = realized P&L from closed trades today
    + worst-case unrealized from open trades today
      (treating each open trade as if it exits at its configured stop price)

    Using the stop price as the unrealized floor is conservative but correct:
    it prevents new entries while existing positions are already so underwater
    that letting them run to their stops would breach the daily cap.  This
    closed the loophole where two trades could enter simultaneously, each
    seeing only realized P&L and both bypassing the cap.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )

    # ── Realized P&L (closed trades) ────────────────────────────────────────
    closed_result = await db.execute(
        select(sqlfunc.sum(Trade.pnl)).where(
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_time >= today_start,
        )
    )
    realized = float(closed_result.scalar() or 0)

    # ── Worst-case unrealized (open trades today at stop level) ─────────────
    open_result = await db.execute(
        select(Trade).where(
            Trade.status == TradeStatus.OPEN,
            Trade.entry_time >= today_start,
        )
    )
    open_trades = open_result.scalars().all()
    unrealized_floor = sum(
        (t.stop_price - t.entry_price) * (t.remaining_qty or t.quantity) * 100
        for t in open_trades
        if t.stop_price and t.entry_price
    )

    return realized + unrealized_floor


async def _get_symbol_losses_today(db, ticker: str) -> int:
    """Count today's losing (PnL < 0) closed trades for a symbol."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(
        select(sqlfunc.count(Trade.id)).where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.pnl < 0,
            Trade.exit_time >= today_start,
        )
    )
    return int(result.scalar() or 0)


async def _get_symbol_trades_today(db, ticker: str) -> int:
    """Count all entries (open + closed) on a symbol today."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(
        select(sqlfunc.count(Trade.id)).where(
            Trade.symbol == ticker,
            Trade.entry_time >= today_start,
        )
    )
    return int(result.scalar() or 0)


async def _get_recent_bad_exit(db, ticker: str) -> Trade | None:
    """
    Return the most recent STOP or VWAP_BREAK exit on this symbol within
    the cooldown window, or None if there is none.
    """
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.cooldown_minutes
    )
    result = await db.execute(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_([ExitReason.STOP, ExitReason.VWAP_BREAK]),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_recent_trend_reversal(db, ticker: str) -> Trade | None:
    """
    Return the most recent TREND_REVERSAL exit on this symbol within the
    trend_reversal_cooldown_minutes window, or None if there is none.

    Separate from _get_recent_bad_exit so TREND_REVERSAL can have its own
    (typically shorter) cooldown without affecting STOP/VWAP_BREAK logic.
    """
    if settings.trend_reversal_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.trend_reversal_cooldown_minutes
    )
    result = await db.execute(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason == ExitReason.TREND_REVERSAL,
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_recent_tp_exit(db, ticker: str) -> Trade | None:
    """
    Return the most recent TP1, TP2 or TRAILING_STOP exit on this symbol
    within the tp_cooldown_minutes window, or None if there is none.
    (TRAILING_STOP is a profitable exit — same "move is exhausted" logic.)

    After a profitable exit the move is typically exhausted — immediately
    re-entering the same direction is chasing momentum that has already
    played out.  This gate enforces a short pause before the next entry.
    Set tp_cooldown_minutes=0 to disable.
    """
    if settings.tp_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.tp_cooldown_minutes
    )
    result = await db.execute(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_(
                [ExitReason.TP1, ExitReason.TP2, ExitReason.TRAILING_STOP]
            ),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_s2_recent_bad_exit(db, ticker: str) -> Trade | None:
    """
    S2 cooldown: return the most recent STOP or EMA_CROSS exit on this symbol
    within s2_cooldown_minutes, or None.
    EMA_CROSS is included because a signal reversal on a stock that just crossed
    back usually means the setup is no longer clean.
    """
    if settings.s2_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.s2_cooldown_minutes
    )
    result = await db.execute(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.strategy_name == "ema_cross",
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_([ExitReason.STOP, ExitReason.EMA_CROSS, ExitReason.TRAILING_STOP]),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Entry scanner (S1 — VWAP pullback)
# ---------------------------------------------------------------------------

async def scan_for_entries() -> None:
    if not is_in_trading_window():
        return

    client = get_tradier_client()

    async with AsyncSessionLocal() as db:
        # ── G2: Daily P&L guard ──────────────────────────────────────────────
        daily_pnl = await _get_daily_pnl(db)
        if daily_pnl <= -abs(settings.max_daily_loss):
            logger.info(
                "Daily loss limit reached ($%.2f) — no new entries today", daily_pnl
            )
            return

        # ── G3: Max concurrent open trades ───────────────────────────────────
        open_count_result = await db.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN)
        )
        open_trades_all = open_count_result.scalars().all()
        if len(open_trades_all) >= settings.max_open_trades:
            logger.debug(
                "Max open trades (%d) reached — skipping scan", settings.max_open_trades
            )
            return

        # Active symbols
        sym_result = await db.execute(
            select(Symbol).where(Symbol.active == True)  # noqa: E712
        )
        symbols = sym_result.scalars().all()

    # ── Fetch QQQ 1-min bars once — used for both adaptive band AND regime gate ──
    # Single API call serves two purposes:
    #   1. Adaptive VWAP band: how extended is QQQ from VWAP? → widens entry band
    #   2. L5 regime gate: which side of VWAP is QQQ on? → blocks counter-trend entries
    # This replaces the old SPY 15-min EMA regime fetch (separate API call, 5-min lag).
    qqq_bars_1m: list = []
    try:
        qqq_bars_1m = await client.get_intraday_bars(
            settings.adaptive_band_symbol, interval="1min", lookback_days=1
        )
    except Exception as exc:
        logger.warning(
            "[QQQ] Could not fetch bars: %s — adaptive band uses normal, regime neutral",
            exc,
        )

    # Adaptive VWAP band
    band_pct = settings.vwap_band_pct
    band_label = "normal (adaptive off)"
    qqq_dist_signed = 0.0
    if settings.adaptive_band_enabled and qqq_bars_1m:
        band_pct, band_label, qqq_dist_signed = get_adaptive_vwap_band(qqq_bars_1m)

    # L5 regime — QQQ VWAP position (real-time, no extra API call)
    regime = get_regime_from_vwap(qqq_bars_1m)

    logger.info(
        "[adaptive-band] %s → band=%.2f%% (%s) | regime=%s",
        settings.adaptive_band_symbol, band_pct * 100, band_label, regime.upper(),
    )

    # Run all symbol scans concurrently — each gets its own DB session so
    # there is no SQLAlchemy session-sharing across coroutines.
    # Semaphore caps at 3 concurrent scans — reduces simultaneous TCP connections
    # to Tradier, which helps avoid ReadTimeout bursts during early-session load.
    sem = asyncio.Semaphore(3)

    async def _scan_one(ticker: str) -> None:
        async with sem:
            async with AsyncSessionLocal() as sym_db:
                try:
                    await _attempt_entry(sym_db, client, ticker, regime, band_pct, qqq_dist_signed)
                except Exception as exc:
                    logger.error(
                        "scan_for_entries error for %s: %s", ticker, exc, exc_info=True
                    )

    await asyncio.gather(*[_scan_one(sym.ticker) for sym in symbols])


async def _attempt_entry(
    db, client, ticker: str, regime: str, band_pct: float,
    qqq_dist_signed: float = 0.0,
) -> None:
    """
    Run the full 8-gate entry stack for one symbol.
    Returns early (no trade) at the first failed gate.
    """
    # ── G4: Per-symbol open trade ────────────────────────────────────────
    existing = await db.execute(
        select(Trade).where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.OPEN,
        )
    )
    if existing.scalar_one_or_none():
        return

    # ── G5: Per-symbol daily loss cap ────────────────────────────────────
    if settings.max_losses_per_symbol_per_day > 0:
        sym_losses = await _get_symbol_losses_today(db, ticker)
        if sym_losses >= settings.max_losses_per_symbol_per_day:
            logger.info(
                "[%s] Per-symbol loss cap reached (%d/%d losing trades today) — "
                "no more entries on this symbol today",
                ticker, sym_losses, settings.max_losses_per_symbol_per_day,
            )
            return

    # ── G5b: Per-symbol total daily trade cap ────────────────────────────
    # Prevents the same 2-3 symbols from monopolising all MAX_OPEN_TRADES slots
    # all day while 14 other scanned symbols never get a look.
    # Once a symbol has had its quota, it is skipped for the rest of the day
    # and the slot opens up for a fresh symbol.
    if settings.max_trades_per_symbol_per_day > 0:
        sym_trades = await _get_symbol_trades_today(db, ticker)
        if sym_trades >= settings.max_trades_per_symbol_per_day:
            logger.debug(
                "[%s] Daily trade cap reached (%d/%d trades today) — "
                "skipping to allow other symbols a turn",
                ticker, sym_trades, settings.max_trades_per_symbol_per_day,
            )
            return

    # ── G6: Cooldown after STOP / VWAP_BREAK ────────────────────────────
    recent_bad = await _get_recent_bad_exit(db, ticker)
    if recent_bad:
        logger.info(
            "[%s] Cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_bad.exit_reason.value,
            recent_bad.exit_time.strftime("%H:%M UTC"),
            settings.cooldown_minutes,
        )
        return

    # ── G6b: Cooldown after TREND_REVERSAL ───────────────────────────────
    recent_tr = await _get_recent_trend_reversal(db, ticker)
    if recent_tr:
        logger.info(
            "[%s] TREND_REVERSAL cooldown active — last exit at %s (%d-min window)",
            ticker,
            recent_tr.exit_time.strftime("%H:%M UTC"),
            settings.trend_reversal_cooldown_minutes,
        )
        return

    # ── G6c: Cooldown after TP1 / TP2 ───────────────────────────────────
    # After a profitable exit the move is typically exhausted.  Re-entering
    # immediately risks chasing a spent momentum spike (e.g. NVDA TP2 at
    # 1:53 PM → new CALL at 1:57 PM right at the spike top).
    recent_tp = await _get_recent_tp_exit(db, ticker)
    if recent_tp:
        logger.info(
            "[%s] TP cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_tp.exit_reason.value,
            recent_tp.exit_time.strftime("%H:%M UTC"),
            settings.tp_cooldown_minutes,
        )
        return

    # ── Fetch market data (needed for Layers 1–5) ────────────────────────
    # 1-min bars: today only — VWAP resets each session
    # 15-min bars: multi-day lookback so EMA always has enough history
    bars_1m  = await client.get_intraday_bars(ticker, interval="1min",  lookback_days=1)
    bars_15m = await client.get_intraday_bars(ticker, interval="15min",
                                              lookback_days=settings.trend_lookback_days)
    # Drop the in-progress 15-min bar — EMA trend / consecutive-bar confirmation
    # must only see completed bars.  (1-min bars are left as-is: VWAP wants the
    # partial bar, and L2/L3 already exclude bars_1m[-1] explicitly.)
    bars_15m = completed_bars(bars_15m, 15)

    if not bars_1m or not bars_15m:
        missing = []
        if not bars_1m:  missing.append("1-min")
        if not bars_15m: missing.append("15-min")
        logger.warning(
            "[%s] No %s bars returned from Tradier — skipping entry (likely timeout or market closed)",
            ticker, " + ".join(missing),
        )
        return

    if len(bars_1m) < settings.bounce_bars_required + 3:
        return   # too few bars to run any layer

    # ── L1: Indicator signal (trend + VWAP pullback) ─────────────────────
    signal = check_entry_signal(bars_1m, bars_15m, ticker=ticker, band_pct=band_pct)
    if not signal:
        return

    direction = signal.direction
    vwap      = signal.vwap

    # ── L1.5: Direction-aware band gate ───────────────────────────────────
    # The adaptive band widens on gap-up days (QQQ above VWAP) to let CALL
    # entries find pullbacks on strongly trending days.  That widening must
    # NOT benefit PUT entries on the same days — a stock that only qualifies
    # for a PUT because the gap-up band stretched from 0.9% to 1.8% is
    # fighting the overall market direction, and these trades reliably lose
    # (NVDA PUT, SPY PUT on gap-up day are the canonical examples).
    #
    # Rule: if direction is PUT and QQQ is above VWAP (gap-up, qqq_dist_signed > 0),
    # re-verify the signal against the NORMAL band.  If it only passes the
    # wider band but not the normal band → block the entry.
    # Same logic inverted for CALL on gap-down days.
    if band_pct > settings.vwap_band_pct:   # wider/relaxed band is active
        if direction == "PUT" and qqq_dist_signed > 0:
            # Gap-up day — re-check PUT against normal band
            strict_signal = check_entry_signal(
                bars_1m, bars_15m, ticker=ticker,
                band_pct=settings.vwap_band_pct,
            )
            if not strict_signal:
                logger.info(
                    "[%s] [L1.5] PUT blocked — only qualifies under wider gap-up band "
                    "(%.2f%%), not normal band (%.2f%%). QQQ is %.2f%% above VWAP. "
                    "Fighting the trend on a gap-up day.",
                    ticker, band_pct * 100, settings.vwap_band_pct * 100,
                    qqq_dist_signed * 100,
                )
                return

        elif direction == "CALL" and qqq_dist_signed < 0:
            # Gap-down day — re-check CALL against normal band
            strict_signal = check_entry_signal(
                bars_1m, bars_15m, ticker=ticker,
                band_pct=settings.vwap_band_pct,
            )
            if not strict_signal:
                logger.info(
                    "[%s] [L1.5] CALL blocked — only qualifies under wider gap-down band "
                    "(%.2f%%), not normal band (%.2f%%). QQQ is %.2f%% below VWAP. "
                    "Fighting the trend on a gap-down day.",
                    ticker, band_pct * 100, settings.vwap_band_pct * 100,
                    abs(qqq_dist_signed) * 100,
                )
                return

    # ── L2: Multi-bar bounce confirmation ────────────────────────────────
    if not check_bounce_confirmation(bars_1m, direction, vwap):
        return

    # ── L3: Momentum candle ───────────────────────────────────────────────
    if not check_momentum_candle(bars_1m, direction, ticker=ticker):
        return

    # ── L4: Intraday VWAP slope ───────────────────────────────────────────
    if not check_vwap_slope(bars_1m, direction):
        return

    # ── L5: Market regime gate ────────────────────────────────────────────
    # SPY alone is sufficient to block a trade.  The old "double-confirmation"
    # logic (requiring BOTH SPY bearish AND stock bearish) was too permissive:
    # a brief divergence where the stock EMA ticked bullish while SPY was
    # falling allowed CALL entries on bear days — every one of which lost.
    #
    # Regime is now derived from QQQ's VWAP position (real-time, 1-min resolution)
    # rather than SPY's 15-min EMA.  No circular reference — QQQ is always the
    # proxy regardless of which ticker is being scanned, including SPY itself.
    if settings.regime_gate_enabled:
        if direction == "CALL" and regime == "bearish":
            logger.info(
                "[%s] [L5] Regime gate: QQQ BEARISH (below VWAP) — blocking CALL",
                ticker,
            )
            return
        if direction == "PUT" and regime == "bullish":
            logger.info(
                "[%s] [L5] Regime gate: QQQ BULLISH (above VWAP) — blocking PUT",
                ticker,
            )
            return
        if regime != "neutral":
            logger.debug(
                "[%s] [L5] QQQ %s — %s allowed (regime not opposing)",
                ticker, regime.upper(), direction,
            )

    logger.info(
        "[%s] All Layers 1–5 passed: %s  price=%.2f  vwap=%.2f",
        ticker, direction, signal.current_price, vwap,
    )

    # ── Contract selection ────────────────────────────────────────────────
    expirations = await client.get_option_expirations(ticker)
    if not expirations:
        return

    # Prefer next-week (or later) expiry over 0DTE to avoid same-day theta crush.
    # If today is the only available expiry we fall back to it rather than skip.
    today_str  = date.today().isoformat()
    non_0dte   = [e for e in expirations if e > today_str]
    expiration = non_0dte[0] if non_0dte else expirations[0]
    logger.debug(
        "[%s] Expiry selected: %s (0DTE available: %s, non-0DTE options: %s)",
        ticker, expiration, today_str in expirations, non_0dte,
    )

    full_chain = await client.get_options_chain(ticker, expiration)
    calls = [o for o in full_chain if o.option_type == "call"]
    puts  = [o for o in full_chain if o.option_type == "put"]
    side_chain = calls if direction == "CALL" else puts

    if not side_chain:
        return

    # Primary filter: delta range + liquidity + positive ask
    eligible = [
        o for o in side_chain
        if (o.volume or 0) >= settings.option_min_volume
        and o.ask > 0
        and settings.option_min_delta <= abs(o.delta or 0) <= settings.option_max_delta
    ]

    # Fallback 1: relax delta range, keep liquidity + positive ask
    if not eligible:
        logger.debug(
            "[%s] No contracts passed delta filter (%.2f–%.2f); relaxing delta.",
            ticker, settings.option_min_delta, settings.option_max_delta,
        )
        eligible = [
            o for o in side_chain
            if (o.volume or 0) >= settings.option_min_volume and o.ask > 0
        ]

    # Fallback 2: drop volume requirement too — at least something tradeable
    if not eligible:
        eligible = [o for o in side_chain if o.ask > 0]
    if not eligible:
        return

    price    = signal.current_price
    selected = min(eligible, key=lambda o: abs(o.strike - price))

    # ── L6: IV filter ─────────────────────────────────────────────────────
    atm_iv = client.get_atm_iv(full_chain, direction, price)
    if atm_iv is not None:
        if atm_iv > settings.iv_max_threshold:
            logger.info(
                "[%s] [L6] IV filter: ATM IV %.1f%% exceeds threshold %.1f%% — "
                "premium too expensive, skipping",
                ticker, atm_iv * 100, settings.iv_max_threshold * 100,
            )
            return
        logger.debug(
            "[%s] [L6] ATM IV %.1f%% — within threshold (%.1f%%), OK",
            ticker, atm_iv * 100, settings.iv_max_threshold * 100,
        )

    # ── Position sizing ───────────────────────────────────────────────────
    ask_price = round(selected.ask, 2)
    mid_price = round(selected.mid, 2) if selected.mid and selected.mid > 0 else ask_price

    # When limit orders are enabled, size and enter at the mid-price.
    # This saves half the spread on every entry (e.g. bid $2.40 / ask $2.50
    # → limit at $2.45 saves $0.05/contract = $1 on a 20-contract position).
    if settings.use_limit_orders and mid_price > 0:
        order_price    = mid_price
        order_type_str = "limit"
    else:
        order_price    = ask_price
        order_type_str = "market"

    cost_per_contract = order_price * 100
    if cost_per_contract <= 0:
        return

    # ── Fixed-dollar risk sizing + premium budget cap ─────────────────────
    # Risk-based qty:   risk_per_trade / (premium lost if the stop fires)
    # Budget-based qty: amount_per_trade / cost of one contract
    # Final qty is the smaller of the two.  If even 1 contract exceeds either
    # limit, SKIP the trade — never "round up" past the configured risk.
    budget_qty = int(settings.amount_per_trade / cost_per_contract)
    if settings.risk_per_trade > 0 and settings.stop_loss_pct > 0:
        risk_per_contract = cost_per_contract * settings.stop_loss_pct
        risk_qty = int(settings.risk_per_trade / risk_per_contract)
        qty = min(risk_qty, budget_qty)
    else:
        qty = budget_qty

    if qty < 1:
        logger.info(
            "[%s] Skipping — 1 contract @ $%.2f would exceed limits "
            "(premium $%.0f > budget $%.0f, or risk $%.0f > risk/trade $%.0f)",
            ticker, order_price,
            cost_per_contract, settings.amount_per_trade,
            cost_per_contract * settings.stop_loss_pct, settings.risk_per_trade,
        )
        return

    # ── Place order (inside lock to prevent MAX_OPEN_TRADES race) ────────
    # Re-check the global open count AND place the buy order while holding
    # the process-wide entry lock.  asyncio.gather() launches all per-symbol
    # scans concurrently; without this lock, multiple scans can simultaneously
    # pass the initial G3 check and each open a trade, violating the cap.
    # The lock is held only for the API call (~100-200 ms), then released.
    order: object = None
    async with _entry_lock:
        open_recheck = await db.execute(
            select(sqlfunc.count(Trade.id)).where(Trade.status == TradeStatus.OPEN)
        )
        if int(open_recheck.scalar() or 0) >= settings.max_open_trades:
            logger.debug(
                "[%s] Max open trades (%d) reached (re-check inside lock) — skipping",
                ticker, settings.max_open_trades,
            )
            return

        # Place the order while still holding the lock so the slot is reserved
        # before any other concurrent scan can slip through the count check.
        # The lock covers only the API placement call (~100-200 ms); the fill
        # poll loop below runs after the lock is released.
        if order_type_str == "limit":
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="limit",
                limit_price=order_price,
            )
        else:
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="market",
            )
    # _entry_lock released — other scans can now proceed

    if order_type_str == "limit":
        logger.info(
            "[%s] Limit order %s placed: %s x%d @ $%.2f (ask $%.2f, saving $%.2f/contract)",
            ticker, order.order_id, selected.symbol, qty, order_price, ask_price,
            ask_price - order_price,
        )
        # Poll for fill — cancel and skip if not filled within timeout.
        # We check every 2 seconds so the total wait is at most
        # limit_order_timeout_seconds (default 15 s).
        filled   = False
        deadline = datetime.now(tz=timezone.utc) + timedelta(
            seconds=settings.limit_order_timeout_seconds
        )
        while datetime.now(tz=timezone.utc) < deadline:
            await asyncio.sleep(2)
            try:
                status_data = await client.get_order_status(order.order_id)
                status_str  = (status_data.get("status") or "").lower()
            except Exception:
                status_str  = "unknown"
            if status_str == "filled":
                filled = True
                break
            if status_str in ("rejected", "canceled", "cancelled"):
                logger.info(
                    "[%s] Limit order %s was %s — aborting entry",
                    ticker, order.order_id, status_str.upper(),
                )
                return
        if not filled:
            logger.info(
                "[%s] Limit order %s not filled within %ds — canceling",
                ticker, order.order_id, settings.limit_order_timeout_seconds,
            )
            try:
                await client.cancel_order(order.order_id)
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to cancel limit order %s: %s", ticker, order.order_id, exc
                )

            # ── Post-cancel race check (Fix #3) ──────────────────────────────
            # A fill can race with the cancel: Tradier may process the fill
            # before the cancel, leaving a real live position with no Trade record.
            # Check the final order status to catch this window.
            try:
                post_status_data = await client.get_order_status(order.order_id)
                post_status_str  = (post_status_data.get("status") or "").lower()
            except Exception:
                post_status_str  = "unknown"

            if post_status_str == "filled":
                # Fill won the race — record the trade normally below.
                logger.info(
                    "[%s] Limit order %s filled during cancel window — "
                    "recording trade to avoid untracked live position.",
                    ticker, order.order_id,
                )
                filled = True
            else:
                logger.info(
                    "[%s] Limit order %s confirmed %s — skipping entry.",
                    ticker, order.order_id, post_status_str.upper(),
                )
                return

        # Use the actual fill price — the limit may have been improved by the
        # market-maker (common when placing at the mid).  Stop/TP levels will
        # be recalculated from this value so they reflect real risk.
        actual_fill = await client.get_fill_price(order.order_id)
        if actual_fill and actual_fill > 0:
            if abs(actual_fill - order_price) > 0.005:
                logger.info(
                    "[%s] Limit filled at $%.2f (limit was $%.2f, diff %+.2f)",
                    ticker, actual_fill, order_price, actual_fill - order_price,
                )
            entry_price = actual_fill
        else:
            entry_price = order_price   # fallback: fill not yet available
    else:
        # Market order — verify it was not rejected before writing to the DB.
        try:
            order_status_data = await client.get_order_status(order.order_id)
            order_status_str  = (order_status_data.get("status") or "").lower()
        except Exception:
            order_status_str  = "unknown"
        if order_status_str in ("rejected", "canceled", "cancelled"):
            logger.error(
                "[%s] Buy order %s was %s — aborting entry, no DB record created.",
                ticker, order.order_id, order_status_str.upper(),
            )
            return
        entry_price = ask_price

    levels = compute_trade_levels(entry_price, direction)

    # Fetch strategies for labelling (use first enabled)
    from app.models import Strategy
    strat_result = await db.execute(
        select(Strategy).where(Strategy.enabled == True)  # noqa: E712
    )
    strategies   = strat_result.scalars().all()
    strategy_name = strategies[0].name if strategies else "vwap_pullback"

    trade = Trade(
        symbol=ticker,
        option_symbol=selected.symbol,
        direction=signal.direction,
        strategy_name=strategy_name,
        tradier_order_id=order.order_id,
        quantity=qty,
        remaining_qty=qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        underlying_entry=signal.current_price,
        vwap_at_entry=signal.vwap,
        **levels,
    )
    db.add(trade)
    await db.commit()
    logger.info(
        "[%s] Trade OPENED: %s %s x%d @ $%.2f  SL=%.2f  TP=%.2f  "
        "(premium $%.0f, risk-at-stop $%.0f)",
        ticker, direction, selected.symbol, qty, entry_price,
        levels["stop_price"], levels["tp2_price"],
        qty * cost_per_contract,
        qty * entry_price * settings.stop_loss_pct * 100,
    )

    # ── Broker-side resting stop order ───────────────────────────────────
    # TEMPORARILY DISABLED — letting trades mature 10-15 min before any stop
    # fires.  Bot-side stop (manage loop) is the only protection right now.
    # Re-enable once the strategy proves consistent win rate.
    #
    # if settings.broker_stop_enabled:
    #     await _place_broker_stop(db, client, trade)


# ---------------------------------------------------------------------------
# Entry scanner — Strategy 2 (EMA crossover)
# ---------------------------------------------------------------------------

async def scan_for_entries_s2() -> None:
    """
    S2 entry scanner — runs on the same interval as S1 (SCAN_INTERVAL_SECONDS).
    Only active when s2_enabled=True.

    Trading window is checked against s2_trading_start_time / s2_last_entry_time.
    S2 uses its own symbol list (strategy="S2" in the symbols table).
    """
    if not settings.s2_enabled:
        return

    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=ET)

    # Trading window check using S2-specific times
    start_h, start_m = (int(x) for x in settings.s2_trading_start_time.split(":"))
    last_h,  last_m  = (int(x) for x in settings.s2_last_entry_time.split(":"))
    end_h,   end_m   = (int(x) for x in settings.s2_trading_end_time.split(":"))

    now_minutes = now_et.hour * 60 + now_et.minute
    start_min   = start_h * 60 + start_m
    last_min    = last_h  * 60 + last_m
    end_min     = end_h   * 60 + end_m

    if now_minutes < start_min or now_minutes > end_min:
        return
    if now_minutes > last_min:
        logger.debug("[S2] Past last entry time (%s ET) — no new S2 entries", settings.s2_last_entry_time)
        return

    client = get_tradier_client()

    async with AsyncSessionLocal() as db:
        # Daily P&L guard (shared with S1)
        daily_pnl = await _get_daily_pnl(db)
        if daily_pnl <= -abs(settings.max_daily_loss):
            logger.info("[S2] Daily loss limit reached — no new S2 entries today")
            return

        # S2 max concurrent positions
        s2_open_result = await db.execute(
            select(Trade).where(
                Trade.status == TradeStatus.OPEN,
                Trade.strategy_name == "ema_cross",
            )
        )
        s2_open_count = len(s2_open_result.scalars().all())
        if s2_open_count >= settings.s2_max_open_trades:
            logger.debug("[S2] Max S2 open trades (%d) reached", settings.s2_max_open_trades)
            return

        # Fetch active S2 symbols
        sym_result = await db.execute(
            select(Symbol).where(Symbol.active == True, Symbol.strategy == "S2")  # noqa: E712
        )
        symbols = sym_result.scalars().all()

    if not symbols:
        return

    sem = asyncio.Semaphore(3)

    async def _scan_s2_one(ticker: str) -> None:
        async with sem:
            async with AsyncSessionLocal() as sym_db:
                try:
                    await _attempt_entry_s2(sym_db, client, ticker)
                except Exception as exc:
                    logger.error(
                        "[S2] scan_for_entries_s2 error for %s: %s", ticker, exc, exc_info=True
                    )

    await asyncio.gather(*[_scan_s2_one(sym.ticker) for sym in symbols])


async def _attempt_entry_s2(db, client, ticker: str) -> None:
    """
    S2 entry gate stack:

    Pre-entry guards (DB):
      G1  Per-symbol S2 open trade exists
      G2  S2 cooldown after recent exit on this symbol

    Signal layers (market data):
      L1  5-min trend filter: price vs EMA200 + EMA9 vs EMA21
      L2  1-min EMA cross: EMA9 crossed EMA21 this bar + volume > prev bar
    """
    # ── G1: Per-symbol S2 open trade ────────────────────────────────────
    existing = await db.execute(
        select(Trade).where(
            Trade.symbol == ticker,
            Trade.strategy_name == "ema_cross",
            Trade.status == TradeStatus.OPEN,
        )
    )
    if existing.scalar_one_or_none():
        return

    # ── G2: S2 cooldown ─────────────────────────────────────────────────
    recent_exit = await _get_s2_recent_bad_exit(db, ticker)
    if recent_exit:
        logger.info(
            "[S2][%s] Cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_exit.exit_reason.value,
            recent_exit.exit_time.strftime("%H:%M UTC"),
            settings.s2_cooldown_minutes,
        )
        return

    # ── Fetch market data ────────────────────────────────────────────────
    bars_1m = await client.get_intraday_bars(ticker, interval="1min", lookback_days=1)
    bars_5m = await client.get_intraday_bars(ticker, interval="5min", lookback_days=5)

    if not bars_1m or not bars_5m:
        logger.warning("[S2][%s] Missing bars (1m=%d 5m=%d) — skipping", ticker, len(bars_1m or []), len(bars_5m or []))
        return

    # ── L1: 5-min trend filter ────────────────────────────────────────────
    # Try CALL direction first; PUT if CALL fails
    call_ok = check_5min_trend_filter(bars_5m, "CALL")
    put_ok  = check_5min_trend_filter(bars_5m, "PUT")

    if not call_ok and not put_ok:
        logger.debug("[S2][%s] 5-min trend filter: neither CALL nor PUT aligns", ticker)
        return

    # ── L2: 1-min EMA cross trigger ───────────────────────────────────────
    direction: str | None = None
    if call_ok and check_1min_ema_cross(bars_1m, "CALL"):
        direction = "CALL"
    elif put_ok and check_1min_ema_cross(bars_1m, "PUT"):
        direction = "PUT"

    if direction is None:
        return  # no fresh cross this tick

    logger.info("[S2][%s] ✓ %s — 5-min trend + 1-min EMA cross confirmed", ticker, direction)

    # ── Contract selection (same logic as S1) ─────────────────────────────
    current_price = bars_1m[-1].close

    expirations = await client.get_option_expirations(ticker)
    if not expirations:
        return

    today_str  = date.today().isoformat()
    non_0dte   = [e for e in expirations if e > today_str]
    expiration = non_0dte[0] if non_0dte else expirations[0]

    full_chain = await client.get_options_chain(ticker, expiration)
    side_chain = [o for o in full_chain if o.option_type == ("call" if direction == "CALL" else "put")]

    if not side_chain:
        return

    eligible = [
        o for o in side_chain
        if (o.volume or 0) >= settings.option_min_volume
        and o.ask > 0
        and settings.option_min_delta <= abs(o.delta or 0) <= settings.option_max_delta
    ]
    if not eligible:
        eligible = [o for o in side_chain if (o.volume or 0) >= settings.option_min_volume and o.ask > 0]
    if not eligible:
        eligible = [o for o in side_chain if o.ask > 0]
    if not eligible:
        return

    selected = min(eligible, key=lambda o: abs(o.strike - current_price))

    # ── Position sizing ───────────────────────────────────────────────────
    ask_price  = round(selected.ask, 2)
    mid_price  = round(selected.mid, 2) if selected.mid and selected.mid > 0 else ask_price

    if settings.use_limit_orders and mid_price > 0:
        order_price    = mid_price
        order_type_str = "limit"
    else:
        order_price    = ask_price
        order_type_str = "market"

    cost_per_contract = order_price * 100
    if cost_per_contract <= 0:
        return

    budget_qty = int(settings.s2_amount_per_trade / cost_per_contract)
    if settings.s2_risk_per_trade > 0 and settings.s2_stop_loss_pct > 0:
        risk_per_contract = cost_per_contract * settings.s2_stop_loss_pct
        risk_qty = int(settings.s2_risk_per_trade / risk_per_contract)
        qty = min(risk_qty, budget_qty)
    else:
        qty = budget_qty

    if qty < 1:
        logger.info(
            "[S2][%s] Skipping — 1 contract @ $%.2f exceeds S2 size limits "
            "(budget $%.0f, risk $%.0f)",
            ticker, order_price, settings.s2_amount_per_trade, settings.s2_risk_per_trade,
        )
        return

    # ── Place order (inside lock) ─────────────────────────────────────────
    order: object = None
    async with _entry_lock:
        # Re-check S2 cap inside lock
        s2_recheck = await db.execute(
            select(sqlfunc.count(Trade.id)).where(
                Trade.status == TradeStatus.OPEN,
                Trade.strategy_name == "ema_cross",
            )
        )
        if int(s2_recheck.scalar() or 0) >= settings.s2_max_open_trades:
            logger.debug("[S2][%s] S2 cap reached (re-check inside lock) — skipping", ticker)
            return

        if order_type_str == "limit":
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="limit",
                limit_price=order_price,
            )
        else:
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="market",
            )

    # ── Limit order fill poll (same as S1) ────────────────────────────────
    if order_type_str == "limit":
        logger.info(
            "[S2][%s] Limit order %s placed: %s x%d @ $%.2f",
            ticker, order.order_id, selected.symbol, qty, order_price,
        )
        filled   = False
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=settings.limit_order_timeout_seconds)
        while datetime.now(tz=timezone.utc) < deadline:
            await asyncio.sleep(2)
            try:
                status_data = await client.get_order_status(order.order_id)
                status_str  = (status_data.get("status") or "").lower()
            except Exception:
                status_str = "unknown"
            if status_str == "filled":
                filled = True
                break
            if status_str in ("rejected", "canceled", "cancelled"):
                logger.info("[S2][%s] Limit order %s %s — aborting", ticker, order.order_id, status_str.upper())
                return
        if not filled:
            try:
                await client.cancel_order(order.order_id)
            except Exception:
                pass
            # Post-cancel race check
            try:
                post = await client.get_order_status(order.order_id)
                if (post.get("status") or "").lower() == "filled":
                    filled = True
                else:
                    return
            except Exception:
                return

        actual_fill = await client.get_fill_price(order.order_id)
        entry_price = actual_fill if actual_fill and actual_fill > 0 else order_price
    else:
        try:
            order_status_data = await client.get_order_status(order.order_id)
            order_status_str  = (order_status_data.get("status") or "").lower()
        except Exception:
            order_status_str = "unknown"
        if order_status_str in ("rejected", "canceled", "cancelled"):
            logger.error("[S2][%s] Buy order %s was %s — aborting", ticker, order.order_id, order_status_str.upper())
            return
        entry_price = ask_price

    stop_price = round(entry_price * (1.0 - settings.s2_stop_loss_pct), 2)

    trade = Trade(
        symbol=ticker,
        option_symbol=selected.symbol,
        direction=direction,
        strategy_name="ema_cross",
        tradier_order_id=order.order_id,
        quantity=qty,
        remaining_qty=qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        underlying_entry=current_price,
        stop_price=stop_price,
        tp1_price=None,
        tp2_price=round(entry_price * (1.0 + settings.s2_take_profit_pct), 2)
                  if settings.s2_take_profit_pct > 0 else None,
    )
    db.add(trade)
    await db.commit()
    _tp_str = (f"  TP={round(entry_price * (1.0 + settings.s2_take_profit_pct), 2):.2f}"
               f" (+{settings.s2_take_profit_pct*100:.0f}%)"
               if settings.s2_take_profit_pct > 0 else "  TP=EMA cross only")
    logger.info(
        "[S2][%s] Trade OPENED: %s %s x%d @ $%.2f  SL=%.2f%s",
        ticker, direction, selected.symbol, qty, entry_price, stop_price, _tp_str,
    )


async def _place_broker_stop(db, client, trade: Trade) -> None:
    """Place a resting sell-to-close stop order at the broker and record its id."""
    try:
        stop_order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=trade.remaining_qty or trade.quantity,
            order_type="stop",
            stop_price=trade.stop_price,
        )
        if stop_order.order_id:
            trade.stop_order_id = stop_order.order_id
            await db.commit()
            logger.info(
                "[%s] Broker stop placed: order %s @ $%.2f",
                trade.symbol, stop_order.order_id, trade.stop_price,
            )
        else:
            logger.error(
                "[%s] Broker stop order returned no order_id (raw=%s) — "
                "bot-side stop remains the only protection for trade %d",
                trade.symbol, stop_order.raw, trade.id,
            )
    except Exception as exc:
        logger.error(
            "[%s] Failed to place broker stop for trade %d: %s — "
            "bot-side stop remains the only protection",
            trade.symbol, trade.id, exc,
        )


# ---------------------------------------------------------------------------
# Open trade manager
# ---------------------------------------------------------------------------

async def manage_open_trades() -> None:
    if not is_market_open():
        return

    client = get_tradier_client()
    cutoff = is_past_cutoff()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN)
        )
        trades = result.scalars().all()

        for trade in trades:
            try:
                # ── Broker-stop reconciliation ──────────────────────────────
                # TEMPORARILY DISABLED — broker stop placement is off, so no
                # stop_order_id will be set on new trades.  This block is kept
                # for backward-compat with any pre-existing trades that may
                # already have a stop_order_id from before this change.
                # Re-enable alongside the placement block above.
                #
                # if trade.stop_order_id:
                #     if await _reconcile_broker_stop(db, client, trade):
                #         continue

                # ── Strategy 2 (EMA cross) management ───────────────────────
                if trade.strategy_name == "ema_cross":
                    await _manage_s2_trade(db, client, trade, cutoff)
                    continue

                # Force close at daily cutoff
                if cutoff:
                    await _close_trade(db, client, trade, ExitReason.CUTOFF)
                    continue

                # Two prices are needed for different purposes:
                #
                #   bid_price — used for TP2 evaluation.
                #     TP2 should only fire when you can actually *receive* the
                #     target price.  A market sell fills at the bid, so using
                #     mid would trigger TP2 at a price you can never achieve on
                #     wide-spread options (GOOGL: mid=$2.79 → bid=$1.90 = real loss).
                #
                #   mid_price — used for STOP / trailing-stop evaluation.
                #     Stops should fire when the market has genuinely moved against
                #     the position.  Using bid for stops causes premature triggers
                #     whenever the bid temporarily dips below the stop level due to
                #     wide spreads, even though the mid (true market price) is still
                #     above the stop.  (META: bid=$5.95 < trail-stop=$6.07 → fired
                #     at breakeven even though mid=$6.22 was safely above the stop.)
                opt_q = await client.get_option_quote(trade.option_symbol)
                if not opt_q:
                    continue
                bid, ask, last = opt_q.bid, opt_q.ask, opt_q.last
                bid_price = bid if bid and bid > 0 else last
                mid_price = (bid + ask) / 2 if (bid and ask and bid > 0 and ask > 0) else last
                if not bid_price or not mid_price:
                    continue  # no valid price — skip this tick

                # ── Trailing stop: raise stop as trade moves in our favour ──
                # Uses mid_price — we want to track genuine option appreciation,
                # not bid bounces caused by wide spreads.
                if trade.entry_price and trade.stop_price is not None:
                    new_stop = compute_trailing_stop(
                        entry_price=trade.entry_price,
                        current_option_price=mid_price,
                        current_stop=trade.stop_price,
                        entry_time=trade.entry_time,
                    )
                    if new_stop != trade.stop_price:
                        gain_pct = (
                            (mid_price - trade.entry_price)
                            / trade.entry_price * 100
                        )
                        logger.info(
                            "[%s] Trailing stop raised: $%.2f → $%.2f "
                            "(option +%.1f%% — entry $%.2f  current $%.2f)",
                            trade.symbol,
                            trade.stop_price, new_stop,
                            gain_pct,
                            trade.entry_price, mid_price,
                        )
                        trade.stop_price = new_stop
                        await db.commit()

                        # Keep the broker-side resting stop in sync.
                        if trade.stop_order_id:
                            try:
                                await client.modify_order(
                                    trade.stop_order_id,
                                    order_type="stop",
                                    stop_price=new_stop,
                                )
                                logger.info(
                                    "[%s] Broker stop %s raised to $%.2f",
                                    trade.symbol, trade.stop_order_id, new_stop,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Could not raise broker stop %s to $%.2f: %s "
                                    "(bot-side stop is at the new level)",
                                    trade.symbol, trade.stop_order_id, new_stop, exc,
                                )

                # Current underlying + 15-min bars.
                # Same lookback as entry so the exit-side EMA(21) sees the same
                # history as the entry-side EMA — previously the default
                # lookback_days=1 made entry and exit evaluate different trends.
                underlying_q = await client.get_quote(trade.symbol)
                bars_15m     = await client.get_intraday_bars(
                    trade.symbol, interval="15min",
                    lookback_days=settings.trend_lookback_days,
                )
                bars_15m = completed_bars(bars_15m, 15)  # drop in-progress bar

                exit_cond = check_exit_conditions(
                    direction=trade.direction.value,
                    entry_price=trade.entry_price,
                    current_option_price=bid_price,   # bid  — for TP evaluation
                    stop_eval_price=mid_price,         # mid  — for stop evaluation
                    stop_price=trade.stop_price or 0,
                    tp1_price=trade.tp1_price or 999_999,
                    tp2_price=trade.tp2_price or 999_999,
                    tp1_hit=trade.tp1_hit,
                    vwap_at_entry=trade.vwap_at_entry or 0,
                    current_underlying=underlying_q.last,
                    bars_15m=bars_15m,
                    remaining_qty=trade.remaining_qty or trade.quantity,
                    entry_time=trade.entry_time,
                )

                if not exit_cond:
                    continue

                # Pass bid_price as the trigger quote so _close_trade records
                # the realistically achievable exit price for P&L calculation.
                # ── v1: all exits close 100 % of the position ──────────────
                await _close_trade(
                    db, client, trade,
                    ExitReason[exit_cond.reason],
                    bid_price,
                )

            except Exception as exc:
                logger.error(
                    "manage_open_trades error trade %d: %s", trade.id, exc, exc_info=True
                )

        # ── Orphan auto-stop backstop ──────────────────────────────────────
        # Check any Tradier positions Ajoy isn't tracking and auto-close
        # them if they've dropped below the configured stop-loss threshold.
        try:
            await _manage_orphan_stops(db, client)
        except Exception as exc:
            logger.error("_manage_orphan_stops failed: %s", exc, exc_info=True)


async def _manage_s2_trade(db, client, trade: Trade, cutoff: bool) -> None:
    """
    S2 (EMA cross) exit manager for a single open trade.

    Exit priority:
      1. Daily cutoff (same as S1)
      2. check_s2_exit_conditions():
         a. Hard stop (−10%, min-hold respected)
         b. Trailing stop cascade (breakeven +10%, trail +20%)
         c. Opposite EMA cross on 1-min
    """
    # 1. Force close at cutoff
    if cutoff:
        await _close_trade(db, client, trade, ExitReason.CUTOFF)
        return

    # 2. Fetch current option quote
    opt_q = await client.get_option_quote(trade.option_symbol)
    if not opt_q:
        return

    bid, ask, last = opt_q.bid, opt_q.ask, opt_q.last
    bid_price = bid if bid and bid > 0 else last
    mid_price = (bid + ask) / 2 if (bid and ask and bid > 0 and ask > 0) else last
    if not bid_price or not mid_price:
        return

    # 3. Manual take-profit override — checked against bid so it only fires when
    #    the trader can actually receive that price.  Uses TP2 field (same as S1 UI).
    if trade.tp2_price and bid_price >= trade.tp2_price:
        logger.info(
            "[S2][%s] Trade %d: manual TP hit — bid $%.2f ≥ tp2 $%.2f",
            trade.symbol, trade.id, bid_price, trade.tp2_price,
        )
        await _close_trade(db, client, trade, ExitReason.TP2, bid_price)
        return

    # 4. Fetch 1-min bars for EMA cross exit detection
    bars_1m = await client.get_intraday_bars(trade.symbol, interval="1min", lookback_days=1)
    if not bars_1m:
        logger.warning("[S2][%s] Trade %d: no 1-min bars — skipping exit check", trade.symbol, trade.id)
        return

    # 5. Evaluate S2 exit conditions
    exit_cond = check_s2_exit_conditions(
        bars_1m=bars_1m,
        direction=trade.direction.value,
        entry_price=trade.entry_price,
        current_price=mid_price,      # use mid for stop/trail evaluation
        stop_price=trade.stop_price or round(trade.entry_price * (1.0 - settings.s2_stop_loss_pct), 2),
        be_stop_set=trade.be_stop_set,
        entry_time=trade.entry_time,
    )

    if exit_cond is None:
        return

    if not exit_cond.close_all and exit_cond.new_stop is not None:
        # Raise the stop (breakeven or trail) — don't close yet
        old_stop = trade.stop_price
        trade.stop_price = exit_cond.new_stop
        if exit_cond.reason == "TRAILING_STOP" and not trade.be_stop_set:
            # Mark breakeven as set only when new_stop == entry_price (breakeven raise)
            if abs(exit_cond.new_stop - trade.entry_price) < 0.005:
                trade.be_stop_set = True
        await db.commit()
        logger.info(
            "[S2][%s] Trade %d stop raised: $%.2f → $%.2f",
            trade.symbol, trade.id, old_stop or 0, exit_cond.new_stop,
        )
        return

    # Map S2 reason string to ExitReason enum
    reason_map = {
        "STOP": ExitReason.STOP,
        "TRAILING_STOP": ExitReason.TRAILING_STOP,
        "EMA_CROSS": ExitReason.EMA_CROSS,
        "CUTOFF": ExitReason.CUTOFF,
    }
    reason = reason_map.get(exit_cond.reason, ExitReason.STOP)
    await _close_trade(db, client, trade, reason, bid_price)


async def _manage_orphan_stops(db, client) -> None:
    """
    Auto-stop backstop for orphaned Tradier positions.

    A position is an orphan if it exists in Tradier but has no matching open
    Ajoy Trade record.  These are NOT managed by the normal exit logic, so we
    add a safety net here: if an orphan's current price has dropped ≥
    STOP_LOSS_PCT below its cost-per-unit, we place a market sell immediately.

    This runs at the end of every manage_open_trades() cycle.
    """
    try:
        positions = await client.get_positions()
    except Exception as exc:
        logger.warning("orphan stop-check: could not fetch Tradier positions: %s", exc)
        return

    if not positions:
        return

    # Collect all open Ajoy option symbols (set for O(1) lookup)
    result = await db.execute(select(Trade).where(Trade.status == TradeStatus.OPEN))
    ajoy_symbols: set[str] = {t.option_symbol for t in result.scalars().all()}

    for pos in positions:
        if pos.symbol in ajoy_symbols:
            continue  # managed by normal exit logic

        qty = pos.quantity
        cost_basis_total = pos.cost_basis or 0.0
        cost_per_unit = cost_basis_total / (qty * 100) if qty else 0.0
        if cost_per_unit <= 0:
            continue

        # Fetch current option price
        try:
            q = await client.get_option_quote(pos.symbol)
            if not q:
                continue
            current = (q.bid + q.ask) / 2 if q.bid and q.ask else q.last
            if not current:
                continue
        except Exception as exc:
            logger.warning(
                "orphan stop-check: could not quote %s: %s", pos.symbol, exc
            )
            continue

        loss_pct = (cost_per_unit - current) / cost_per_unit
        if loss_pct < settings.stop_loss_pct:
            continue  # still above stop threshold — no action

        logger.info(
            "[ORPHAN] Auto-stop triggered for %s x%d: cost=%.4f current=%.4f "
            "loss=%.1f%% (threshold %.1f%%) — placing market sell",
            pos.symbol, qty,
            cost_per_unit, current,
            loss_pct * 100, settings.stop_loss_pct * 100,
        )
        try:
            await client.place_option_order(
                option_symbol=pos.symbol,
                side="sell_to_close",
                quantity=qty,
                order_type="market",
            )
            logger.info(
                "[ORPHAN] Market sell submitted for %s x%d", pos.symbol, qty
            )
        except Exception as exc:
            logger.error(
                "[ORPHAN] Failed to auto-stop %s: %s", pos.symbol, exc, exc_info=True
            )


async def _finalize_broker_stop_close(db, client, trade: Trade) -> bool:
    """
    Record the DB close for a trade whose broker-side resting stop order filled.
    The position is already flat at the broker — no sell order is placed here.
    """
    fill = await client.get_fill_price(trade.stop_order_id)
    exit_price = fill or trade.stop_price or trade.entry_price
    qty = trade.remaining_qty or trade.quantity

    original_stop = round(trade.entry_price * (1 - settings.stop_loss_pct), 2)
    reason = (
        ExitReason.TRAILING_STOP
        if (trade.stop_price or 0) > original_stop
        else ExitReason.STOP
    )

    rounded_exit      = round(exit_price, 2)
    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = rounded_exit
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = reason
    trade.pnl         = round(
        (trade.pnl or 0) + (rounded_exit - trade.entry_price) * qty * 100, 2
    )
    await db.commit()
    logger.info(
        "[%s] Trade %d CLOSED via broker-side %s @ $%.2f  PnL=$%.2f",
        trade.symbol, trade.id, reason.value, rounded_exit, trade.pnl,
    )
    return True


async def _reconcile_broker_stop(db, client, trade: Trade) -> bool:
    """
    Check the broker-side resting stop order's status.

    filled                       → close the DB trade, return True
    canceled/rejected/expired    → clear stop_order_id (loudly), return False
    open/pending/anything else   → return False (no action)
    """
    try:
        status_data = await client.get_order_status(trade.stop_order_id)
        status_str  = (status_data.get("status") or "").lower()
    except Exception as exc:
        logger.warning(
            "[%s] Trade %d: could not check broker stop %s: %s",
            trade.symbol, trade.id, trade.stop_order_id, exc,
        )
        return False

    if status_str == "filled":
        return await _finalize_broker_stop_close(db, client, trade)

    if status_str in ("canceled", "cancelled", "rejected", "expired"):
        logger.warning(
            "[%s] Trade %d: broker stop %s is %s — clearing it. "
            "Bot-side stop is now the only protection for this position.",
            trade.symbol, trade.id, trade.stop_order_id, status_str.upper(),
        )
        trade.stop_order_id = None
        await db.commit()

    return False


async def _close_trade(
    db,
    client,
    trade: Trade,
    reason: ExitReason,
    exit_price: float | None = None,
) -> bool:
    """
    Place a sell_to_close order and close the trade in the DB.

    Returns True on success, False if the order was rejected/failed
    (DB record is left OPEN so the next manage cycle can retry).

    Exit-price priority
    -------------------
    1. Caller-supplied production mid-quote (most accurate — this is the real
       market price that triggered the exit signal).  Passed for TP2, STOP,
       VWAP_BREAK, TREND_REVERSAL.
    2. Sandbox fill price — used ONLY when no production price is available
       (e.g. CUTOFF, where we close without a preceding quote check).
       NOTE: sandbox fills are *synthetic* and diverge from real market prices,
       so they must not override a known production quote.
    3. Fresh production mid-quote fetched here as last resort.
    4. Entry price — absolute worst case so P&L records as 0.
    """
    qty = trade.remaining_qty or trade.quantity

    # ── Step 0: cancel the broker-side resting stop first ───────────────────
    # The resting sell order reserves the contracts — placing a second
    # sell_to_close while it is live risks rejection or a double-sell.
    # A fill can race the cancel, so re-check status after canceling.
    if trade.stop_order_id:
        try:
            await client.cancel_order(trade.stop_order_id)
        except Exception as exc:
            logger.warning(
                "[%s] Trade %d: cancel of broker stop %s failed: %s",
                trade.symbol, trade.id, trade.stop_order_id, exc,
            )
        try:
            st_data = await client.get_order_status(trade.stop_order_id)
            st_str  = (st_data.get("status") or "").lower()
        except Exception:
            st_str = "unknown"
        if st_str == "filled":
            # Stop won the race — position already flat; record that close.
            logger.info(
                "[%s] Trade %d: broker stop filled during cancel window — "
                "recording broker-stop exit instead of %s",
                trade.symbol, trade.id, reason.value,
            )
            return await _finalize_broker_stop_close(db, client, trade)
        if st_str not in ("canceled", "cancelled", "expired", "rejected"):
            # Cancel not yet confirmed — don't risk a double-sell.  Leave the
            # trade OPEN; the next manage cycle will retry the whole exit.
            logger.warning(
                "[%s] Trade %d: broker stop %s still %s after cancel — "
                "deferring exit to next cycle",
                trade.symbol, trade.id, trade.stop_order_id, st_str.upper(),
            )
            return False
        trade.stop_order_id = None
        await db.commit()

    # ── Step 1: place the sell order ────────────────────────────────────────
    try:
        sell_order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=qty,
            order_type="market",
        )
    except Exception as exc:
        logger.error(
            "[%s] Trade %d: sell_to_close API call failed — leaving OPEN to retry. %s",
            trade.symbol, trade.id, exc,
        )
        return False

    # ── Step 2: confirm the sell order actually filled ──────────────────────
    # We must verify the order reached "filled" status before closing the DB
    # record.  In production, market orders usually fill within milliseconds,
    # but pending/submitted/partially_filled states can occur on illiquid options.
    # If we close the DB record before the fill is confirmed we believe the
    # position is flat while a real live position still exists in Tradier.
    fill = await client.get_fill_price(sell_order.order_id)

    try:
        order_status = await client.get_order_status(sell_order.order_id)
        status_str   = (order_status.get("status") or "").lower()
    except Exception:
        status_str = "unknown"

    if status_str in ("rejected", "canceled", "cancelled"):
        logger.error(
            "[%s] Trade %d: sell order %s was %s — NOT closing in DB. "
            "Position is still live in Tradier. Manual intervention may be required.",
            trade.symbol, trade.id, sell_order.order_id, status_str.upper(),
        )
        return False  # Leave the trade OPEN so it shows up in Open Positions

    if status_str != "filled" and not fill:
        # Order placed but not yet confirmed as filled (pending / submitted /
        # partially_filled / unknown).  Leave the trade OPEN — the next
        # manage_open_trades cycle will call _close_trade again and retry.
        logger.warning(
            "[%s] Trade %d: sell order %s status=%s — not yet filled. "
            "Leaving trade OPEN to retry on next management cycle.",
            trade.symbol, trade.id, sell_order.order_id, status_str.upper(),
        )
        return False

    # ── Step 3: resolve exit price — actual Tradier fill is most accurate ────
    # Prefer the actual fill price from Tradier — it reflects what the
    # position was sold for (bid price for market sells, slightly below mid).
    #
    # Sanity-check the fill against the trigger quote.
    # The trigger quote is the option price at the moment the exit condition
    # fired.  By the time the market order routes to the exchange, the option
    # can have moved significantly — especially for near-expiry ATM options
    # where a fast underlying reversal can cause a 50-100% price bounce in
    # a matter of seconds.
    #
    # In LIVE mode (USE_SANDBOX=0) the exchange fill IS the authoritative
    # price — use generous bounds so legitimate bounces aren't discarded.
    # In SANDBOX mode prices can be stale; apply tighter bounds.
    #
    # Historical bug: bounds of 2% upper / 12% lower caused a real $2.10
    # fill (option bounced from $1.05 trigger) to be rejected and replaced
    # with the $1.05 trigger quote, overstating the loss by ~$250.
    if settings.use_sandbox:
        _FILL_UPPER_PCT = 0.02   # sandbox: price should stay near trigger
        _FILL_LOWER_PCT = 0.12
    else:
        _FILL_UPPER_PCT = 1.00   # live: option can double between check and fill
        _FILL_LOWER_PCT = 0.50   # live: can lose half in fast gap-down
    if fill and exit_price:
        signed_dev = (fill - exit_price) / exit_price   # positive = fill above trigger
        if signed_dev > _FILL_UPPER_PCT:
            logger.warning(
                "[%s] Trade %d exit: fill $%.2f is %.0f%% ABOVE trigger $%.2f "
                "— market sells can't improve above bid, using trigger quote",
                trade.symbol, trade.id, fill, signed_dev * 100, exit_price,
            )
            fill = None
        elif signed_dev < -_FILL_LOWER_PCT:
            logger.warning(
                "[%s] Trade %d exit: fill $%.2f deviates %.0f%% below trigger $%.2f "
                "— likely stale sandbox price, using trigger quote",
                trade.symbol, trade.id, fill, abs(signed_dev) * 100, exit_price,
            )
            fill = None
        elif abs(signed_dev) > 0.01:
            logger.info(
                "[%s] Trade %d exit: actual fill $%.2f vs trigger quote $%.2f "
                "(diff %+.2f — using fill price for P&L)",
                trade.symbol, trade.id, fill, exit_price, fill - exit_price,
            )
    if fill:
        exit_price = fill
    elif not exit_price:
        try:
            q = await client.get_option_quote(trade.option_symbol)
            if q:
                exit_price = (q.bid + q.ask) / 2 if q.bid and q.ask else q.last
        except Exception:
            pass
    if not exit_price:
        exit_price = trade.entry_price  # worst-case: record at cost

    # ── Step 4: persist the close ────────────────────────────────────────────
    partial_pnl      = trade.pnl or 0
    rounded_exit     = round(exit_price, 2)   # round FIRST, then use same value for P&L
    close_pnl        = (rounded_exit - trade.entry_price) * qty * 100

    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = rounded_exit
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = reason
    trade.pnl         = round(partial_pnl + close_pnl, 2)
    await db.commit()
    logger.info(
        "[%s] Trade %d CLOSED via %s @ $%.2f  PnL=$%.2f",
        trade.symbol, trade.id, reason.value, exit_price, trade.pnl,
    )
    return True


# ---------------------------------------------------------------------------
# Startup orphan close
# ---------------------------------------------------------------------------

async def close_orphaned_open_trades() -> None:
    """
    On bot startup, close any OPEN trades that survived past the force-close
    window.  This handles the case where the bot was stopped/restarted after
    15:16 ET — the scheduler's cutoff job never ran, leaving live positions
    dangling in Tradier overnight.

    Fires once immediately when the scheduler starts.  Safe to run multiple
    times: trades already CLOSED are skipped.
    """
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=ET)
    cutoff_h = settings.cutoff_hour
    cutoff_m = settings.cutoff_minute

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Trade).where(Trade.status == TradeStatus.OPEN))
        open_trades = result.scalars().all()

    if not open_trades:
        return

    past_cutoff = (
        now_et.hour > cutoff_h
        or (now_et.hour == cutoff_h and now_et.minute >= cutoff_m)
    )

    # Also close on weekend / outside market hours where no new bars will arrive
    # (is_market_open checks the calendar via strategy helpers).
    # Simple time check: outside 09:30–16:00 ET on weekdays counts as "closed".
    market_closed = not is_market_open()

    if not (past_cutoff or market_closed):
        logger.debug(
            "[startup] %d open trade(s) found — within trading window, no orphan close needed",
            len(open_trades),
        )
        return

    logger.warning(
        "[startup] %d orphaned open trade(s) found past cutoff / market closed — "
        "force-closing now",
        len(open_trades),
    )
    client = get_tradier_client()
    async with AsyncSessionLocal() as db:
        for trade in open_trades:
            # Re-fetch inside session
            result = await db.execute(select(Trade).where(Trade.id == trade.id))
            t = result.scalar_one_or_none()
            if not t or t.status != TradeStatus.OPEN:
                continue
            logger.info(
                "[startup] Force-closing orphaned trade %d %s %s (entered %s ET)",
                t.id, t.symbol, t.direction.value, t.entry_time,
            )
            closed = await _close_trade(db, client, t, ExitReason.CUTOFF)
            if not closed:
                logger.error(
                    "[startup] Could not close orphaned trade %d %s — "
                    "manual intervention required in Tradier",
                    t.id, t.symbol,
                )


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled via config")
        return

    # Show the dual-environment config at startup so logs are self-explanatory.
    logger.info(
        "Tradier: market data → %s | orders → %s (account %s)",
        settings.tradier_base_url,
        settings.tradier_base_url_sandbox,
        settings.tradier_account_id,
    )

    scheduler.add_job(
        scan_for_entries, "interval",
        seconds=settings.scan_interval_seconds,
        id="scan_entries", replace_existing=True,
    )
    scheduler.add_job(
        scan_for_entries_s2, "interval",
        seconds=settings.scan_interval_seconds,
        id="scan_entries_s2", replace_existing=True,
    )
    scheduler.add_job(
        manage_open_trades, "interval",
        seconds=settings.manage_interval_seconds,
        id="manage_trades", replace_existing=True,
    )
    # One-shot startup job: close any open trades that survived past the
    # force-close window (bot was stopped before 15:16 ET cutoff job ran).
    scheduler.add_job(
        close_orphaned_open_trades, "date",
        run_date=datetime.now(tz=scheduler.timezone),
        id="startup_orphan_close", replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — scan every %ds, manage every %ds, window %s–%s ET  "
        "| regime gate=%s (%s) | IV max=%.0f%% | cooldown=%dm | "
        "sym loss cap=%d/day",
        settings.scan_interval_seconds, settings.manage_interval_seconds,
        settings.trading_start_time, settings.trading_end_time,
        "ON" if settings.regime_gate_enabled else "OFF",
        settings.regime_gate_symbol,
        settings.iv_max_threshold * 100,
        settings.cooldown_minutes,
        settings.max_losses_per_symbol_per_day,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
