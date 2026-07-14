"""
Strategy 2 — EMA Pullback entry and exit logic.

Entry rules (3-step, all must pass)
-------------------------------------
  Step 1 — 5-min Trend Filter (cached; only recomputes when a new 5-min candle closes):
    CALL : EMA9 > EMA21  AND  Close > VWAP  AND  EMA9 slope > 0  AND  EMA21 slope > 0
    PUT  : EMA9 < EMA21  AND  Close < VWAP  AND  EMA9 slope < 0  AND  EMA21 slope < 0
    Slopes: EMA(current) > EMA(previous)  (upward) / EMA(current) < EMA(previous) (downward)

  Step 2 — 1-min Pullback to the 5-min EMA9 level (bars[-2], the previous completed bar):
    CALL : pullback-bar Low  <= 5m-EMA9   OR  Close within 0.10% of 5m-EMA9
    PUT  : pullback-bar High >= 5m-EMA9   OR  Close within 0.10% of 5m-EMA9

  Step 3 — 1-min Confirmation candle (bars[-1], the most recent completed bar):
    CALL : candle is bullish (close > open)  AND  close > pullback-bar High  (breaks prior range)
    PUT  : candle is bearish (close < open)  AND  close < pullback-bar Low   (breaks prior range)

  Note: Steps 2 and 3 are checked on CONSECUTIVE bars.  The pullback bar (bars[-2])
  must touch EMA9; the confirmation bar (bars[-1]) must then close through its range.
  This prevents single-wick "hammer" entries and requires a confirmed breakout after
  the pullback has completed.

Filters (applied post contract-selection in scheduler)
-------------------------------------------------------
  Spread   : option bid/ask spread must be ≤ s2_max_spread_pct of the mid price
  Volume   : underlying 1-min volume must be ≥ 20-bar rolling average
             (s2_volume_lookback, default 20)

Exit rules
----------
  Opposite EMA cross  : EMA(fast) crosses back through EMA(slow) on 5-min
  Hard stop           : option price drops ≥ S2_STOP_LOSS_PCT
  Breakeven           : option price +S2_BREAKEVEN_PCT → stop → entry
  Trailing            : option price +S2_TRAIL_PCT → trail S2_TRAIL_FROM_CURRENT_PCT below current
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date as _date, timezone
from typing import Optional

from app.config import settings
from app.services.strategy import calculate_ema, calculate_vwap, completed_bars
from app.services.tradier import Bar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared exit dataclass (mirrors S1's ExitCondition)
# ---------------------------------------------------------------------------

@dataclass
class S2ExitCondition:
    reason: str           # STOP | TRAILING_STOP | EMA_CROSS | CUTOFF | MANUAL
    close_all: bool = True
    new_stop: Optional[float] = None   # only set when we're raising the stop (not closing)
    close_all_override: bool = False   # set to True when new_stop is also set but we want to close


# ---------------------------------------------------------------------------
# Helper: get last N valid EMA values from a price list
# ---------------------------------------------------------------------------

def _last_ema(prices: list[float], period: int) -> float | None:
    """Return the most recent valid EMA value, or None if not enough data."""
    if len(prices) < period:
        return None
    vals = calculate_ema(prices, period)
    for v in reversed(vals):
        if v == v:  # not NaN
            return v
    return None


def _last_two_emas(prices: list[float], period: int) -> tuple[float | None, float | None]:
    """Return (second-to-last, last) valid EMA values."""
    if len(prices) < period + 1:
        return None, None
    vals = calculate_ema(prices, period)
    valid = [v for v in vals if v == v]
    if len(valid) < 2:
        return None, None
    return valid[-2], valid[-1]


def _today_bars(bars: list[Bar]) -> list[Bar]:
    """
    Filter to the most recent session's bars only (for VWAP reset each day).
    Uses the LAST BAR's date rather than the server clock — bar times are ET
    while the server may run in another timezone, and the two dates diverge
    around midnight boundaries.
    """
    if not bars:
        return bars
    last_day = bars[-1].time.date()
    return [b for b in bars if b.time.date() == last_day]


# ---------------------------------------------------------------------------
# Per-ticker trend filter cache
# Updated only when a new completed 5-min bar appears (by bar timestamp).
# ---------------------------------------------------------------------------

_trend_cache: dict[str, dict] = {}
# Format: { ticker: { "last_bar_time": datetime, "CALL": bool, "PUT": bool } }


# ---------------------------------------------------------------------------
# 1. 5-min Trend Filter (with caching)
# ---------------------------------------------------------------------------

def check_5min_trend_filter(
    bars_5m: list[Bar],
    direction: str,
    ticker: str = "",
) -> bool:
    """
    Return True if the 5-min trend aligns with `direction`.

    CALL : EMA9 > EMA21  AND  Close > VWAP  AND  EMA9 slope↑  AND  EMA21 slope↑
    PUT  : EMA9 < EMA21  AND  Close < VWAP  AND  EMA9 slope↓  AND  EMA21 slope↓

    The result is cached per ticker and only recomputed when a new completed
    5-min candle appears — so the trend doesn't change every 1-min tick.
    Falls through to True only when there is genuinely insufficient history.
    """
    ema_fast = settings.s2_ema_fast   # 9
    ema_slow = settings.s2_ema_slow   # 21

    bars = completed_bars(bars_5m, interval_minutes=5)

    if len(bars) < ema_slow + 2:
        logger.debug(
            "[S2-5m-filter][%s] Not enough 5-min bars (%d) for EMA(%d) — filter skipped",
            ticker, len(bars), ema_slow,
        )
        return True  # don't block on data scarcity

    last_bar_time = bars[-1].time
    cached = _trend_cache.get(ticker, {})

    # ── Use cached result if the last completed bar hasn't changed ───────────
    if cached.get("last_bar_time") == last_bar_time:
        result = cached.get(direction, True)
        logger.debug(
            "[S2-5m-filter][%s] Using cached trend — %s=%s",
            ticker, direction, result,
        )
        return result

    # ── New candle — recompute ───────────────────────────────────────────────
    closes = [b.close for b in bars]

    ema9_prev, ema9_now  = _last_two_emas(closes, ema_fast)
    ema21_prev, ema21_now = _last_two_emas(closes, ema_slow)

    if any(v is None for v in (ema9_prev, ema9_now, ema21_prev, ema21_now)):
        logger.debug("[S2-5m-filter][%s] Could not compute EMAs — filter skipped", ticker)
        _trend_cache[ticker] = {"last_bar_time": last_bar_time, "CALL": True, "PUT": True}
        return True

    # VWAP from today's 5-min bars only (session VWAP resets daily)
    today_5m = _today_bars(bars)
    vwap = calculate_vwap(today_5m) if today_5m else 0.0
    current_close = bars[-1].close

    # CALL: all four conditions must hold
    call_ok = bool(
        ema9_now > ema21_now
        and (current_close > vwap if vwap > 0 else True)
        and ema9_now > ema9_prev    # EMA9 slope > 0
        and ema21_now > ema21_prev  # EMA21 slope > 0
    )

    # PUT: exactly opposite
    put_ok = bool(
        ema9_now < ema21_now
        and (current_close < vwap if vwap > 0 else True)
        and ema9_now < ema9_prev    # EMA9 slope < 0
        and ema21_now < ema21_prev  # EMA21 slope < 0
    )

    _trend_cache[ticker] = {
        "last_bar_time": last_bar_time,
        "CALL": call_ok,
        "PUT": put_ok,
    }

    if direction == "CALL":
        if not call_ok:
            logger.info(
                "[S2-5m-filter][%s] CALL blocked — EMA9=%.4f EMA21=%.4f "
                "close=%.4f VWAP=%.4f EMA9_slope=%s EMA21_slope=%s",
                ticker, ema9_now, ema21_now, current_close, vwap,
                "↑" if ema9_now > ema9_prev else "↓",
                "↑" if ema21_now > ema21_prev else "↓",
            )
        else:
            logger.debug(
                "[S2-5m-filter][%s] CALL OK — EMA9=%.4f > EMA21=%.4f "
                "close=%.4f > VWAP=%.4f slopes both↑",
                ticker, ema9_now, ema21_now, current_close, vwap,
            )
        return call_ok
    else:
        if not put_ok:
            logger.info(
                "[S2-5m-filter][%s] PUT blocked — EMA9=%.4f EMA21=%.4f "
                "close=%.4f VWAP=%.4f EMA9_slope=%s EMA21_slope=%s",
                ticker, ema9_now, ema21_now, current_close, vwap,
                "↑" if ema9_now > ema9_prev else "↓",
                "↑" if ema21_now > ema21_prev else "↓",
            )
        else:
            logger.debug(
                "[S2-5m-filter][%s] PUT OK — EMA9=%.4f < EMA21=%.4f "
                "close=%.4f < VWAP=%.4f slopes both↓",
                ticker, ema9_now, ema21_now, current_close, vwap,
            )
        return put_ok


# ---------------------------------------------------------------------------
# 1b. EMA cross freshness filter
# ---------------------------------------------------------------------------

def check_ema_cross_freshness(
    bars_5m: list[Bar],
    direction: str,
    ticker: str = "",
) -> bool:
    """
    Return True if the EMA9/EMA21 cross in `direction` occurred within the
    last s2_cross_max_bars_old completed 5-min bars.

    Prevents entering stale trends: the EMA9/21 order can stay in place for
    hours after an initial impulse.  A cross that happened 10+ bars ago (50+
    minutes) often signals an exhausted move, not a fresh opportunity.

    Set S2_CROSS_MAX_BARS_OLD=0 in .env to disable (allow all cross ages).

    CALL : looks for the most recent bar where EMA9 crossed ABOVE EMA21.
    PUT  : looks for the most recent bar where EMA9 crossed BELOW EMA21.

    SESSION-AWARE (Jul 2026): only crosses within TODAY's session bars count.
    A cross from a prior day is stale by definition, no matter the bar count —
    and a fixed count alone can't express that (a large max admits yesterday's
    trends early in the window; a small max blocks the opening drive late in
    the window).

    Returns False (block) if no cross is found within today's session.
    Returns True (allow) when there is insufficient data to evaluate.
    """
    max_bars = settings.s2_cross_max_bars_old
    if max_bars <= 0:
        return True  # disabled

    bars = completed_bars(bars_5m, interval_minutes=5)
    ema_fast = settings.s2_ema_fast
    ema_slow = settings.s2_ema_slow

    if len(bars) < ema_slow + 2:
        return True  # not enough history — don't block on data scarcity

    closes = [b.close for b in bars]
    fast_vals = calculate_ema(closes, ema_fast)
    slow_vals = calculate_ema(closes, ema_slow)

    n = len(bars)
    cross_bars_ago: int | None = None

    # Session boundary: only crosses from TODAY's session count as fresh.
    # A fixed bar-count alone can't do this job — early in the entry window
    # a large max admits yesterday-afternoon trends, while late in the window
    # a small max blocks legitimate opening-drive crosses.  The date check
    # separates the two questions cleanly.
    session_date = bars[-1].time.date()

    # Walk backwards from the most recent completed bar to find the last cross
    for i in range(n - 1, 0, -1):
        if bars[i].time.date() != session_date:
            # Walked past today's open without finding a cross — the trend
            # predates today's session and is stale by definition.
            break

        f_now  = fast_vals[i]
        f_prev = fast_vals[i - 1]
        s_now  = slow_vals[i]
        s_prev = slow_vals[i - 1]

        # Skip bars where EMA hasn't converged (NaN)
        if any(v != v for v in (f_now, f_prev, s_now, s_prev)):
            continue

        if direction == "CALL":
            if f_prev <= s_prev and f_now > s_now:
                cross_bars_ago = (n - 1) - i  # 0 = cross on the most recent bar
                break
        else:  # PUT
            if f_prev >= s_prev and f_now < s_now:
                cross_bars_ago = (n - 1) - i
                break

    if cross_bars_ago is None:
        # No cross within today's session → trend carried over from a prior
        # day (or predates available history) — too old either way.
        logger.info(
            "[S2-freshness][%s] No %s EMA cross found in TODAY's session — "
            "trend predates today, blocking",
            ticker, direction,
        )
        return False

    ok = cross_bars_ago <= max_bars
    if not ok:
        logger.info(
            "[S2-freshness][%s] %s cross is %d bars old (max %d bars = %d min) — "
            "exhausted move, blocking",
            ticker, direction, cross_bars_ago, max_bars, max_bars * 5,
        )
    else:
        logger.debug(
            "[S2-freshness][%s] %s cross %d bars old — within %d-bar window ✓",
            ticker, direction, cross_bars_ago, max_bars,
        )
    return ok


# ---------------------------------------------------------------------------
# 2. Get current 5-min EMA9 level (used as the pullback reference)
# ---------------------------------------------------------------------------

def get_5min_ema9(bars_5m: list[Bar]) -> float | None:
    """
    Return the most recent completed 5-min bar's EMA9 value.
    Used as the price level for the 1-min pullback check.
    Returns None if there is insufficient history.
    """
    bars = completed_bars(bars_5m, interval_minutes=5)
    if not bars:
        return None
    closes = [b.close for b in bars]
    return _last_ema(closes, settings.s2_ema_fast)


# ---------------------------------------------------------------------------
# 3. 1-min Pullback check
# ---------------------------------------------------------------------------

def check_1min_pullback(
    bars_1m: list[Bar],
    direction: str,
    ema9_5m: float,
    ticker: str = "",
) -> bool:
    """
    Return True if the PREVIOUS completed 1-min bar (bars[-2]) pulled back to
    (or through) the 5-min EMA9 level.

    Checking bars[-2] — not bars[-1] — enforces the sequential two-bar pattern:
      bars[-2] = pullback bar  (must touch EMA9)
      bars[-1] = confirmation bar  (checked separately by check_1min_confirmation)

    CALL : pullback-bar Low  <= ema9_5m  OR  Close within 0.10% of ema9_5m
    PUT  : pullback-bar High >= ema9_5m  OR  Close within 0.10% of ema9_5m
    """
    bars = completed_bars(bars_1m, interval_minutes=1)
    if len(bars) < 2:
        logger.debug(
            "[S2-pullback][%s] Need ≥2 completed 1-min bars for pullback check (have %d)",
            ticker, len(bars),
        )
        return False

    if ema9_5m <= 0:
        logger.debug("[S2-pullback][%s] EMA9 level is zero — skipping pullback check", ticker)
        return False

    pullback_bar = bars[-2]   # the bar that must have touched EMA9
    threshold = ema9_5m * 0.001  # 0.10% of EMA9

    if direction == "CALL":
        ok = pullback_bar.low <= ema9_5m or abs(pullback_bar.close - ema9_5m) <= threshold
        if not ok:
            logger.debug(
                "[S2-pullback][%s] CALL — no pullback: pullback-bar low=%.4f close=%.4f "
                "EMA9=%.4f (need low≤EMA9 or close within %.4f)",
                ticker, pullback_bar.low, pullback_bar.close, ema9_5m, threshold,
            )
        else:
            logger.info(
                "[S2-pullback][%s] CALL pullback ✓ — pullback-bar low=%.4f close=%.4f EMA9=%.4f",
                ticker, pullback_bar.low, pullback_bar.close, ema9_5m,
            )
        return ok
    else:  # PUT
        ok = pullback_bar.high >= ema9_5m or abs(pullback_bar.close - ema9_5m) <= threshold
        if not ok:
            logger.debug(
                "[S2-pullback][%s] PUT — no pullback: pullback-bar high=%.4f close=%.4f "
                "EMA9=%.4f (need high≥EMA9 or close within %.4f)",
                ticker, pullback_bar.high, pullback_bar.close, ema9_5m, threshold,
            )
        else:
            logger.info(
                "[S2-pullback][%s] PUT pullback ✓ — pullback-bar high=%.4f close=%.4f EMA9=%.4f",
                ticker, pullback_bar.high, pullback_bar.close, ema9_5m,
            )
        return ok


# ---------------------------------------------------------------------------
# 4. 1-min Confirmation candle
# ---------------------------------------------------------------------------

def check_1min_confirmation(
    bars_1m: list[Bar],
    direction: str,
    ema9_5m: float | None = None,
    ticker: str = "",
) -> bool:
    """
    Return True if the most recently completed 1-min candle (bars[-1]) confirms
    the entry by breaking out through the pullback bar's range AND closing on
    the correct side of the 5-min EMA9 level.

    bars[-2] = pullback bar (the one that touched EMA9 — checked by check_1min_pullback)
    bars[-1] = confirmation bar (this candle must break bars[-2]'s range)

    CALL : bars[-1] is bullish (close > open)
           AND  close > bars[-2] High        (breaks prior range)
           AND  close > ema9_5m              (reclaimed EMA9 — weak bounces that stall
                                              below EMA9 are filtered out)

    PUT  : bars[-1] is bearish (close < open)
           AND  close < bars[-2] Low         (breaks prior range)
           AND  close < ema9_5m              (rejected back below EMA9)

    The EMA9 close filter (ema9_5m) is optional: when None the check is skipped
    (unit tests that don't supply a level still work).
    """
    bars = completed_bars(bars_1m, interval_minutes=1)
    if len(bars) < 2:
        logger.debug("[S2-confirm][%s] Not enough 1-min bars for confirmation check", ticker)
        return False

    current  = bars[-1]
    previous = bars[-2]

    if direction == "CALL":
        bullish     = current.close > current.open
        breaks_high = current.close > previous.high
        above_ema9  = (ema9_5m is None) or (current.close > ema9_5m)
        ok = bullish and breaks_high and above_ema9
        if not ok:
            logger.info(
                "[S2-confirm][%s] CALL confirmation failed — "
                "candle %s (open=%.4f close=%.4f) prev_high=%.4f ema9=%.4f above_ema9=%s",
                ticker,
                "bullish" if bullish else "bearish/doji",
                current.open, current.close, previous.high,
                ema9_5m or 0.0, above_ema9,
            )
        else:
            logger.info(
                "[S2-confirm][%s] CALL confirmed ✓ — close=%.4f > prev_high=%.4f > ema9=%.4f",
                ticker, current.close, previous.high, ema9_5m or 0.0,
            )
        return ok
    else:  # PUT
        bearish    = current.close < current.open
        breaks_low = current.close < previous.low
        below_ema9 = (ema9_5m is None) or (current.close < ema9_5m)
        ok = bearish and breaks_low and below_ema9
        if not ok:
            logger.info(
                "[S2-confirm][%s] PUT confirmation failed — "
                "candle %s (open=%.4f close=%.4f) prev_low=%.4f ema9=%.4f below_ema9=%s",
                ticker,
                "bearish" if bearish else "bullish/doji",
                current.open, current.close, previous.low,
                ema9_5m or 0.0, below_ema9,
            )
        else:
            logger.info(
                "[S2-confirm][%s] PUT confirmed ✓ — close=%.4f < prev_low=%.4f < ema9=%.4f",
                ticker, current.close, previous.low, ema9_5m or 0.0,
            )
        return ok


# ---------------------------------------------------------------------------
# 5. Post-contract-selection filters
# ---------------------------------------------------------------------------

def check_option_spread(
    bid: float,
    ask: float,
    max_spread_pct: float = 0.10,
    ticker: str = "",
) -> bool:
    """
    Return True if the option's bid/ask spread is acceptable.

    Spread check: (ask - bid) / mid ≤ max_spread_pct
    Default: 10% of the mid price.  Override via caller (or config).
    """
    if bid <= 0 or ask <= 0:
        return True  # can't evaluate — don't block
    mid = (bid + ask) / 2
    if mid <= 0:
        return True
    spread_pct = (ask - bid) / mid
    ok = spread_pct <= max_spread_pct
    if not ok:
        logger.info(
            "[S2-spread][%s] Spread too wide — bid=%.2f ask=%.2f spread=%.1f%% > max %.1f%%",
            ticker, bid, ask, spread_pct * 100, max_spread_pct * 100,
        )
    return ok


def check_volume_filter(
    bars_1m: list[Bar],
    lookback: int = 20,
    ticker: str = "",
) -> bool:
    """
    Return True if the most recently COMPLETED 1-min bar's volume is at or
    above the rolling `lookback`-bar average.  Low-volume bars signal thin
    order flow and increase slippage risk.

    Tradier includes the currently-forming bar as the last element of the
    intraday response.  We strip it with completed_bars() so we compare a
    full completed bar against the historical average — not a partial bar
    that has accumulated only a fraction of its final volume (which would
    almost always read below the average and block every valid entry).

    Returns True (allow) when there are fewer than `lookback` bars available —
    don't block on data scarcity early in the session.
    """
    bars = completed_bars(bars_1m, interval_minutes=1)

    if len(bars) < lookback + 1:
        return True  # not enough history — pass through

    recent = bars[-(lookback + 1):-1]  # `lookback` completed bars before the last
    avg_vol = sum(b.volume for b in recent) / len(recent) if recent else 0
    current_vol = bars[-1].volume       # most recently completed bar

    ok = current_vol >= avg_vol
    if not ok:
        logger.info(
            "[S2-volume][%s] Volume below average — current=%d < avg=%.0f (%d-bar) — "
            "skipping (thin order flow)",
            ticker, current_vol, avg_vol, lookback,
        )
    return ok


# ---------------------------------------------------------------------------
# 5b. Structure exit — 1-min close through the 5-min EMA9
#
# WHY: the old signal exit (5-min EMA9/21 cross) needs 10–15 minutes to flip
# after price reverses — on short-dated options the position has already lost
# 15–20% by then, so the hard stop always fired first (live data: only 2
# EMA_CROSS exits in 43 trades, both losers).  The entry thesis is "price
# bounced off the 5-min EMA9" — so the correct invalidation is price closing
# back through EMA9, which this detects within 2–3 minutes.
# ---------------------------------------------------------------------------

def check_s2_structure_exit(
    bars_1m: list[Bar],
    direction: str,
    ema9_5m: float,
    confirm_bars: int | None = None,
    margin_pct: float | None = None,
    ticker: str = "",
) -> bool:
    """
    Return True (exit) when the last `confirm_bars` COMPLETED 1-min bars all
    closed on the wrong side of the 5-min EMA9 by at least `margin_pct`.

      CALL: all closes < ema9 × (1 − margin)  → thesis invalidated, exit
      PUT : all closes > ema9 × (1 + margin)  → thesis invalidated, exit

    Two consecutive closes (default) filter single-bar wicks; the margin
    filters closes that are essentially *on* the EMA.
    """
    confirm_bars = confirm_bars if confirm_bars is not None else settings.s2_structure_exit_bars
    margin_pct   = margin_pct   if margin_pct   is not None else settings.s2_structure_exit_margin_pct

    if ema9_5m <= 0 or confirm_bars <= 0:
        return False

    bars = completed_bars(bars_1m, interval_minutes=1)
    if len(bars) < confirm_bars:
        return False

    recent = bars[-confirm_bars:]
    if direction == "CALL":
        broken = all(b.close < ema9_5m * (1 - margin_pct) for b in recent)
    else:  # PUT
        broken = all(b.close > ema9_5m * (1 + margin_pct) for b in recent)

    if broken:
        logger.info(
            "[S2-struct-exit][%s] %s invalidated — last %d 1-min closes on wrong "
            "side of 5m-EMA9 %.4f (closes: %s)",
            ticker, direction, confirm_bars, ema9_5m,
            [round(b.close, 4) for b in recent],
        )
    return broken


# ---------------------------------------------------------------------------
# 6. S2 exit conditions
# ---------------------------------------------------------------------------

def check_s2_exit_conditions(
    bars: list[Bar],
    direction: str,
    entry_price: float,
    current_price: float,
    stop_price: float,
    be_stop_set: bool,
    entry_time: Optional[datetime] = None,
    now: Optional[datetime] = None,
    interval_minutes: int = 5,
    bid_price: Optional[float] = None,
    bars_1m: Optional[list[Bar]] = None,
    ema9_5m: Optional[float] = None,
) -> Optional[S2ExitCondition]:
    """
    Evaluate S2 exit conditions in priority order:

    0. Quick-loss early exit (first s2_quick_loss_max_minutes only)
    1. Hard stop (min-hold respected via s2_stop_loss_min_hold_minutes)
    2. Trailing stop (breakeven → trail cascade)
       Stage 1: gain ≥ s2_breakeven_pct  → stop = entry × (1 + s2_trailing_stop_lock_profit_pct)
       Stage 2: gain ≥ s2_trail_pct      → stop trails s2_trail_from_current_pct below current
    3. Opposite EMA cross (signal-based exit)

    Returns an S2ExitCondition when an exit is warranted, else None.
    When the trailing stop should be *raised* (not yet hit), returns an
    S2ExitCondition with close_all=False and new_stop set.

    bid_price: when provided, the hard stop trigger is evaluated against the
    bid (the actual exit price) rather than the mid.  This prevents the stop
    from firing while mid == stop_price but bid is already 5-10% lower due to
    a wide spread — ensuring the breakeven stop actually exits near breakeven.
    Gain computation and trail-stop levels continue to use current_price (mid)
    for accuracy.
    """
    _now = now or datetime.now(tz=timezone.utc)

    # Stop is checked against bid when available (we exit at bid, so bid must
    # actually breach the stop level before we act).  Falls back to mid if bid
    # is not supplied (e.g. in unit tests).
    _stop_check_price = bid_price if bid_price is not None else current_price

    # ── 0. Quick-loss early exit ─────────────────────────────────────────────
    # If the option drops s2_quick_loss_pct% within the first
    # s2_quick_loss_max_minutes of the trade, exit immediately.
    # Catches bad entries that go sharply against us before gamma amplifies the
    # loss further — saves the gap between quick_loss_pct and the hard stop.
    if settings.s2_quick_loss_pct > 0 and entry_time is not None:
        _entry_ql = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        held_min_ql = (_now - _entry_ql).total_seconds() / 60
        in_window = held_min_ql <= settings.s2_quick_loss_max_minutes
        past_quiet = held_min_ql >= settings.s2_quick_loss_min_hold_minutes
        if in_window and past_quiet:
            loss_pct = (entry_price - _stop_check_price) / entry_price
            if loss_pct >= settings.s2_quick_loss_pct:
                logger.info(
                    "[S2-exit] QUICK_LOSS — option down %.1f%% (≥%.0f%% threshold) "
                    "at %.1f min of entry (window 0–%d min). Entry=%.2f current=%.2f",
                    loss_pct * 100, settings.s2_quick_loss_pct * 100,
                    held_min_ql, settings.s2_quick_loss_max_minutes,
                    entry_price, _stop_check_price,
                )
                return S2ExitCondition(reason="QUICK_LOSS", close_all=True)

    # ── Hard stop min-hold ───────────────────────────────────────────────────
    stop_suppressed = False
    min_hold = settings.s2_stop_loss_min_hold_minutes
    if min_hold > 0 and entry_time is not None:
        _entry = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        held_min = (_now - _entry).total_seconds() / 60
        if held_min < min_hold:
            stop_suppressed = True
            logger.debug(
                "[S2-exit] Hard stop suppressed — %.1f min into trade (min_hold=%d)",
                held_min, min_hold,
            )

    # ── 1. Hard stop ────────────────────────────────────────────────────────
    if not stop_suppressed and _stop_check_price <= stop_price:
        original_stop = round(entry_price * (1.0 - settings.s2_stop_loss_pct), 2)
        reason = "TRAILING_STOP" if stop_price > original_stop else "STOP"
        logger.info(
            "[S2-exit] %s — bid %.2f ≤ stop %.2f (entry %.2f, mid %.2f)",
            reason, _stop_check_price, stop_price, entry_price, current_price,
        )
        return S2ExitCondition(reason=reason, close_all=True)

    # ── 2. Trailing stop cascade ─────────────────────────────────────────────
    gain_pct = (current_price - entry_price) / entry_price

    if gain_pct >= settings.s2_trail_pct:
        # Full trail mode — stop trails s2_trail_from_current_pct below current price
        new_stop = round(current_price * (1.0 - settings.s2_trail_from_current_pct), 2)
        if new_stop > stop_price:
            logger.debug(
                "[S2-exit] Trail raised: %.2f → %.2f (gain=%.1f%%)",
                stop_price, new_stop, gain_pct * 100,
            )
            return S2ExitCondition(reason="TRAILING_STOP", close_all=False, new_stop=new_stop)

    elif gain_pct >= settings.s2_breakeven_pct and not be_stop_set:
        # Move stop to entry + lock_profit_pct (not plain entry) so the exit
        # is net-positive after bid-ask spread cost when stop fires at the bid.
        lock_pct = settings.s2_trailing_stop_lock_profit_pct
        new_stop = round(entry_price * (1.0 + lock_pct), 2)
        if new_stop > stop_price:
            logger.info(
                "[S2-exit] Breakeven stop raised to %.2f (entry %.2f + %.0f%% lock, gain=%.1f%%)",
                new_stop, entry_price, lock_pct * 100, gain_pct * 100,
            )
            return S2ExitCondition(reason="TRAILING_STOP", close_all=False, new_stop=new_stop)

    # ── 3. Signal exit ───────────────────────────────────────────────────────
    # Structure exit (default): 1-min closes back through the 5-min EMA9 —
    # reacts within 2–3 minutes of the thesis breaking.  The legacy 5-min
    # EMA9/21 cross took 10–15 min to flip, by which point the hard stop had
    # always fired first (it saved zero trades in live data).
    if (
        settings.s2_structure_exit_enabled
        and bars_1m is not None
        and ema9_5m is not None
        and ema9_5m > 0
    ):
        if check_s2_structure_exit(bars_1m, direction, ema9_5m):
            return S2ExitCondition(reason="STRUCT_EXIT", close_all=True)
    else:
        # Legacy signal exit — opposite EMA cross on 5-min bars
        opposite = "PUT" if direction == "CALL" else "CALL"
        if _check_ema_cross_signal(bars, opposite, interval_minutes=interval_minutes):
            logger.info(
                "[S2-exit] EMA_CROSS — opposite %s cross detected → exiting %s position",
                opposite, direction,
            )
            return S2ExitCondition(reason="EMA_CROSS", close_all=True)

    return None


# ---------------------------------------------------------------------------
# Internal: detect EMA cross on bars (used by exit logic only)
# No volume gate, no candle color gate — speed of exit matters.
# ---------------------------------------------------------------------------

def _check_ema_cross_signal(bars_in: list[Bar], direction: str, interval_minutes: int = 5) -> bool:
    """
    Exit-side EMA cross detector.
    direction here is the *opposite* of the trade direction.
    Works on any bar interval; defaults to 5-min.
    """
    bars = completed_bars(bars_in, interval_minutes=interval_minutes)

    ema_fast = settings.s2_ema_fast
    ema_slow = settings.s2_ema_slow
    if len(bars) < ema_slow + 2:
        return False

    closes = [b.close for b in bars]
    fast_prev, fast_now = _last_two_emas(closes, ema_fast)
    slow_prev, slow_now = _last_two_emas(closes, ema_slow)

    if any(v is None for v in (fast_prev, fast_now, slow_prev, slow_now)):
        return False

    if direction == "CALL":
        return (fast_prev <= slow_prev) and (fast_now > slow_now)
    else:
        return (fast_prev >= slow_prev) and (fast_now < slow_now)
