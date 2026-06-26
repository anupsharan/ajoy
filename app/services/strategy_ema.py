"""
Strategy 2 — EMA Crossover entry and exit logic.

Entry rules
-----------
  5-min EMA cross trigger (last two completed 5-min bars):
    CALL : previous bar had EMA(9) ≤ EMA(21), trigger bar has EMA(9) > EMA(21)
           AND trigger bar closes green (close > open)
    PUT  : previous bar had EMA(9) ≥ EMA(21), trigger bar has EMA(9) < EMA(21)
           AND trigger bar closes red  (close < open)

  Note: The cross detection implies trend alignment — no separate trend filter needed.
        Volume gate removed (5-min bars are less noisy than 1-min).

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
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.services.strategy import calculate_ema, completed_bars
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


# ---------------------------------------------------------------------------
# 1. 5-min trend filter
# ---------------------------------------------------------------------------

def check_5min_trend_filter(bars_5m: list[Bar], direction: str) -> bool:
    """
    Return True if the 5-min trend aligns with `direction`.

    CALL : EMA(9) > EMA(21) on 5-min
    PUT  : EMA(9) < EMA(21) on 5-min

    The EMA(200) price gate was removed — it's a 2.5-day lagging average that
    reliably blocks valid opening-range signals without adding directional value.
    The EMA(9) vs EMA(21) spread is sufficient to confirm trend alignment.

    Falls back to True only when there is genuinely not enough history —
    we skip rather than block on data gaps.
    """
    bars = completed_bars(bars_5m, interval_minutes=5)

    ema_fast = settings.s2_ema_fast    # 9
    ema_slow = settings.s2_ema_slow    # 21

    if len(bars) < ema_slow + 1:
        logger.debug(
            "[S2-5m-filter] Not enough 5-min bars (%d) for EMA(%d) — filter skipped (pass-through)",
            len(bars), ema_slow,
        )
        return True   # don't block on data scarcity; S2 entry still requires the 1-min trigger

    closes = [b.close for b in bars]

    ema_fast_val = _last_ema(closes, ema_fast)
    ema_slow_val = _last_ema(closes, ema_slow)

    if ema_fast_val is None or ema_slow_val is None:
        logger.debug("[S2-5m-filter] Could not compute EMAs — filter skipped")
        return True

    if direction == "CALL":
        spread_ok = ema_fast_val > ema_slow_val
        if not spread_ok:
            logger.info(
                "[S2-5m-filter] CALL blocked — EMA(%d)=%.4f ≤ EMA(%d)=%.4f on 5-min (downtrend)",
                ema_fast, ema_fast_val, ema_slow, ema_slow_val,
            )
            return False
        logger.debug(
            "[S2-5m-filter] CALL OK — EMA%d=%.4f > EMA%d=%.4f on 5-min",
            ema_fast, ema_fast_val, ema_slow, ema_slow_val,
        )
        return True

    else:  # PUT
        spread_ok = ema_fast_val < ema_slow_val
        if not spread_ok:
            logger.info(
                "[S2-5m-filter] PUT blocked — EMA(%d)=%.4f ≥ EMA(%d)=%.4f on 5-min (uptrend)",
                ema_fast, ema_fast_val, ema_slow, ema_slow_val,
            )
            return False
        logger.debug(
            "[S2-5m-filter] PUT OK — EMA%d=%.4f < EMA%d=%.4f on 5-min",
            ema_fast, ema_fast_val, ema_slow, ema_slow_val,
        )
        return True


# ---------------------------------------------------------------------------
# 2. 1-min EMA crossover trigger
# ---------------------------------------------------------------------------

def check_1min_ema_cross(bars_1m: list[Bar], direction: str) -> bool:
    """
    Return True if the most recent completed 1-min bar is a fresh EMA cross.

    CALL : previous bar EMA(fast) ≤ EMA(slow) → trigger bar EMA(fast) > EMA(slow)
    PUT  : previous bar EMA(fast) ≥ EMA(slow) → trigger bar EMA(fast) < EMA(slow)

    Volume confirmation (if s2_volume_confirm=True):
        trigger bar volume must exceed the previous bar volume.

    We use completed_bars() so the live forming bar is excluded.
    """
    bars = completed_bars(bars_1m, interval_minutes=1)

    ema_fast = settings.s2_ema_fast   # 9
    ema_slow = settings.s2_ema_slow   # 21
    min_bars = ema_slow + 2           # need at least slow_period + 1 completed bars

    if len(bars) < min_bars:
        logger.debug(
            "[S2-1m-cross] Not enough 1-min bars (%d, need %d)",
            len(bars), min_bars,
        )
        return False

    closes = [b.close for b in bars]

    fast_prev, fast_now = _last_two_emas(closes, ema_fast)
    slow_prev, slow_now = _last_two_emas(closes, ema_slow)

    if any(v is None for v in (fast_prev, fast_now, slow_prev, slow_now)):
        logger.debug("[S2-1m-cross] Could not compute EMAs — no cross detected")
        return False

    trigger_vol  = bars[-1].volume
    previous_vol = bars[-2].volume

    if direction == "CALL":
        crossed = (fast_prev <= slow_prev) and (fast_now > slow_now)
        if not crossed:
            logger.debug(
                "[S2-1m-cross] No bullish cross — EMA%d prev=%.4f now=%.4f | EMA%d prev=%.4f now=%.4f",
                ema_fast, fast_prev, fast_now, ema_slow, slow_prev, slow_now,
            )
            return False
    else:  # PUT
        crossed = (fast_prev >= slow_prev) and (fast_now < slow_now)
        if not crossed:
            logger.debug(
                "[S2-1m-cross] No bearish cross — EMA%d prev=%.4f now=%.4f | EMA%d prev=%.4f now=%.4f",
                ema_fast, fast_prev, fast_now, ema_slow, slow_prev, slow_now,
            )
            return False

    # Volume confirmation
    if settings.s2_volume_confirm and trigger_vol <= previous_vol:
        logger.info(
            "[S2-1m-cross] %s cross found but volume insufficient — trigger=%d ≤ prev=%d",
            direction, trigger_vol, previous_vol,
        )
        return False

    # Candle color confirmation — cross bar must close in the direction of the trade.
    # Filters out crosses on doji / indecision bars where EMA crossed but price
    # was actually moving against the entry.
    trigger_bar = bars[-1]
    if direction == "CALL" and trigger_bar.close <= trigger_bar.open:
        logger.info(
            "[S2-1m-cross] CALL cross found but trigger bar is red/doji "
            "(open=%.2f close=%.2f) — not a bullish confirmation candle",
            trigger_bar.open, trigger_bar.close,
        )
        return False
    if direction == "PUT" and trigger_bar.close >= trigger_bar.open:
        logger.info(
            "[S2-1m-cross] PUT cross found but trigger bar is green/doji "
            "(open=%.2f close=%.2f) — not a bearish confirmation candle",
            trigger_bar.open, trigger_bar.close,
        )
        return False

    logger.info(
        "[S2-1m-cross] ✓ %s cross confirmed — EMA%d crossed EMA%d | "
        "candle %s | vol %d > %d",
        direction, ema_fast, ema_slow,
        "green" if direction == "CALL" else "red",
        trigger_vol, previous_vol,
    )
    return True


# ---------------------------------------------------------------------------
# 3. 5-min EMA crossover trigger (entry + exit signal)
# ---------------------------------------------------------------------------

def check_5min_ema_cross(bars_5m: list[Bar], direction: str) -> bool:
    """
    Return True if the most recently completed 5-min bar shows a fresh EMA cross.

    CALL : previous bar EMA(fast) ≤ EMA(slow) → trigger bar EMA(fast) > EMA(slow)
           AND trigger bar is green (close > open)
    PUT  : previous bar EMA(fast) ≥ EMA(slow) → trigger bar EMA(fast) < EMA(slow)
           AND trigger bar is red  (close < open)

    Uses completed 5-min bars with multi-day lookback so EMA values are stable
    from market open.  No volume gate — 5-min bar volume is less noisy than 1-min.
    The cross itself implies trend alignment, so no separate trend filter is needed.
    """
    bars = completed_bars(bars_5m, interval_minutes=5)

    ema_fast = settings.s2_ema_fast   # 9
    ema_slow = settings.s2_ema_slow   # 21
    min_bars = ema_slow + 2           # need at least slow_period + 1 completed bars

    if len(bars) < min_bars:
        logger.debug(
            "[S2-5m-cross] Not enough 5-min bars (%d, need %d)",
            len(bars), min_bars,
        )
        return False

    closes = [b.close for b in bars]

    fast_prev, fast_now = _last_two_emas(closes, ema_fast)
    slow_prev, slow_now = _last_two_emas(closes, ema_slow)

    if any(v is None for v in (fast_prev, fast_now, slow_prev, slow_now)):
        logger.debug("[S2-5m-cross] Could not compute EMAs — no cross detected")
        return False

    if direction == "CALL":
        crossed = (fast_prev <= slow_prev) and (fast_now > slow_now)
        if not crossed:
            logger.debug(
                "[S2-5m-cross] No bullish cross — EMA%d prev=%.4f now=%.4f | EMA%d prev=%.4f now=%.4f",
                ema_fast, fast_prev, fast_now, ema_slow, slow_prev, slow_now,
            )
            return False
    else:  # PUT
        crossed = (fast_prev >= slow_prev) and (fast_now < slow_now)
        if not crossed:
            logger.debug(
                "[S2-5m-cross] No bearish cross — EMA%d prev=%.4f now=%.4f | EMA%d prev=%.4f now=%.4f",
                ema_fast, fast_prev, fast_now, ema_slow, slow_prev, slow_now,
            )
            return False

    # Candle color confirmation — cross bar must close in the direction of the trade.
    trigger_bar = bars[-1]
    if direction == "CALL" and trigger_bar.close <= trigger_bar.open:
        logger.info(
            "[S2-5m-cross] CALL cross found but trigger bar is red/doji "
            "(open=%.2f close=%.2f) — not a bullish confirmation candle",
            trigger_bar.open, trigger_bar.close,
        )
        return False
    if direction == "PUT" and trigger_bar.close >= trigger_bar.open:
        logger.info(
            "[S2-5m-cross] PUT cross found but trigger bar is green/doji "
            "(open=%.2f close=%.2f) — not a bearish confirmation candle",
            trigger_bar.open, trigger_bar.close,
        )
        return False

    logger.info(
        "[S2-5m-cross] ✓ %s cross confirmed on 5-min — EMA%d crossed EMA%d | candle %s",
        direction, ema_fast, ema_slow,
        "green" if direction == "CALL" else "red",
    )
    return True


# ---------------------------------------------------------------------------
# 4. S2 exit conditions
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
) -> Optional[S2ExitCondition]:
    """
    Evaluate S2 exit conditions in priority order:

    1. Hard stop (min-hold respected via s2_stop_loss_min_hold_minutes)
    2. Trailing stop (breakeven → trail cascade)
    3. Opposite EMA cross (signal-based exit)

    Returns an S2ExitCondition when an exit is warranted, else None.
    When the trailing stop should be *raised* (not yet hit), returns an
    S2ExitCondition with close_all=False and new_stop set.
    """
    _now = now or datetime.now(tz=timezone.utc)

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
    if not stop_suppressed and current_price <= stop_price:
        original_stop = round(entry_price * (1.0 - settings.s2_stop_loss_pct), 2)
        reason = "TRAILING_STOP" if stop_price > original_stop else "STOP"
        logger.info(
            "[S2-exit] %s — price %.2f ≤ stop %.2f (entry %.2f)",
            reason, current_price, stop_price, entry_price,
        )
        return S2ExitCondition(reason=reason, close_all=True)

    # ── 2. Trailing stop cascade ─────────────────────────────────────────────
    gain_pct = (current_price - entry_price) / entry_price

    if gain_pct >= settings.s2_trail_pct:
        # Full trail mode — stop trails 5% below current price
        new_stop = round(current_price * (1.0 - settings.s2_trail_from_current_pct), 2)
        if new_stop > stop_price:
            logger.debug(
                "[S2-exit] Trail raised: %.2f → %.2f (gain=%.1f%%)",
                stop_price, new_stop, gain_pct * 100,
            )
            return S2ExitCondition(reason="TRAILING_STOP", close_all=False, new_stop=new_stop)

    elif gain_pct >= settings.s2_breakeven_pct and not be_stop_set:
        # Move stop to breakeven
        new_stop = round(entry_price, 2)
        if new_stop > stop_price:
            logger.info(
                "[S2-exit] Breakeven stop set at entry %.2f (gain=%.1f%%)",
                entry_price, gain_pct * 100,
            )
            return S2ExitCondition(reason="TRAILING_STOP", close_all=False, new_stop=new_stop)

    # ── 3. Opposite EMA cross (signal exit) ──────────────────────────────────
    opposite = "PUT" if direction == "CALL" else "CALL"
    if _check_ema_cross_signal(bars, opposite, interval_minutes=interval_minutes):
        logger.info(
            "[S2-exit] EMA_CROSS — opposite %s cross detected → exiting %s position",
            opposite, direction,
        )
        return S2ExitCondition(reason="EMA_CROSS", close_all=True)

    return None


# ---------------------------------------------------------------------------
# Internal: detect EMA cross on 1-min bars without volume requirement
# (exit cross doesn't need volume confirm — speed of exit matters)
# ---------------------------------------------------------------------------

def _check_ema_cross_signal(bars_in: list[Bar], direction: str, interval_minutes: int = 5) -> bool:
    """
    Exit-side EMA cross detector — no volume gate, no candle color gate.
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
