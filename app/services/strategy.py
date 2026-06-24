"""
Core trading strategy — entry and exit logic.

Entry gate stack (all must pass in order):
  Layer 1  — check_entry_signal(): trend_15min AND price_vs_vwap AND pullback_to_vwap
  Layer 2  — check_bounce():       last N completed 1-min bars all close on correct VWAP side
  Layer 3  — check_momentum():     last completed bar is a green/red momentum candle
  Layer 4  — check_vwap_slope():   intraday VWAP direction agrees with trade direction
  Layer 5  — get_market_regime():  SPY 15-min trend gate (config-driven, cached)
  Layer 6  — IV filter:            checked in scheduler after contract selection

Pre-entry guards (in scheduler, before Layer 1):
  • Trading hours window
  • Daily P&L loss limit
  • Max open trades
  • Per-symbol open trade already exists
  • Per-symbol daily loss cap (new)
  • Cooldown after STOP / VWAP_BREAK on that symbol (new)

Exit logic (check_exit_conditions):
  TP1 → partial close + move stop to breakeven
  TP2 → close remainder
  Stop hit → close all
  VWAP break → close all
  Trend reversal (15-min EMA flips) → close all
  2:45 PM ET cutoff → close all
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.tradier import Bar

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Pure math helpers
# ---------------------------------------------------------------------------

def calculate_vwap(bars: list[Bar]) -> float:
    """
    Cumulative VWAP = Σ(typical_price × volume) / Σ(volume).
    Returns 0.0 if bars is empty or total volume is zero.
    """
    cum_tp_vol = 0.0
    cum_vol    = 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3
        cum_tp_vol += tp * b.volume
        cum_vol    += b.volume
    if cum_vol == 0:
        return 0.0
    return cum_tp_vol / cum_vol


def completed_bars(
    bars: list[Bar],
    interval_minutes: int,
    now: Optional[datetime] = None,
) -> list[Bar]:
    """
    Drop the trailing in-progress bar, if any.

    Tradier's timesales endpoint includes the bar currently being formed.
    Decision logic that claims to use "confirmed closed-bar data" (EMA trend,
    consecutive-bar confirmation, trend reversal) must exclude it — otherwise
    e.g. ema_consecutive_bars=2 is really 1 closed + 1 partial bar.

    A bar starting at T on an N-minute interval is complete once now >= T+N.
    Bar times from Tradier are naive ET (or tz-aware) — both are handled.
    """
    if not bars:
        return bars
    _now = now or datetime.now(tz=ET)
    if _now.tzinfo is None:
        _now = _now.replace(tzinfo=ET)
    last = bars[-1].time
    last_et = last.replace(tzinfo=ET) if last.tzinfo is None else last.astimezone(ET)
    if last_et + timedelta(minutes=interval_minutes) > _now:
        return bars[:-1]
    return bars


def calculate_ema(prices: list[float], period: int) -> list[float]:
    """
    Exponential Moving Average.
    Returns list of same length; first `period-1` values are NaN-seeded
    from the initial SMA.
    """
    if not prices or period <= 0:
        return []
    k    = 2 / (period + 1)
    seed = sum(prices[:period]) / period
    ema_values: list[float] = [float("nan")] * (period - 1) + [seed]
    for price in prices[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def ema_direction(
    bars_15m: list[Bar],
    period: int = 9,
    live_price: float | None = None,
) -> str:
    """
    Derive 15-min EMA trend direction.
    Returns 'bullish', 'bearish', or 'neutral'.

    live_price — when provided, the current underlying price is used instead
    of the last closed bar's close.  This makes the trend update in real-time
    (every quote refresh) rather than only at 15-min bar boundaries.
    The scheduler passes live_price=None so entry/exit decisions are always
    anchored to confirmed closed-bar data.  The UI passes live_price so the
    displayed Stock Trend column reflects the current price vs EMA.
    """
    if len(bars_15m) < period + 1:
        return "neutral"
    closes    = [b.close for b in bars_15m]
    ema_vals  = calculate_ema(closes, period)
    valid     = [(c, e) for c, e in zip(closes, ema_vals) if e == e]  # drop NaN
    if len(valid) < 2:
        return "neutral"
    price_now = live_price if live_price is not None else valid[-1][0]
    ema_now   = valid[-1][1]
    # Use a relative epsilon (0.005% of EMA) to avoid spurious trend signals
    # from floating-point drift when price and EMA are essentially equal.
    epsilon = ema_now * 0.00005
    if price_now > ema_now + epsilon:
        return "bullish"
    if price_now < ema_now - epsilon:
        return "bearish"
    return "neutral"


def check_ema_alignment(
    bars_15m: list[Bar],
    direction: str,
    bars_1m: list[Bar] | None = None,
) -> bool:
    """
    Dual-timeframe EMA alignment gate.

    Both the 15-min AND 1-min EMA(fast) must be on the correct side of
    EMA(slow) for the trade direction.  One timeframe disagreeing kills the
    entry.

      CALL: EMA(fast) > EMA(slow) on both 15-min and 1-min
      PUT:  EMA(fast) < EMA(slow) on both 15-min and 1-min

    Why dual-timeframe?
      The 15-min gate catches afternoon deterioration hours before the slow
      EMA turns.  The 1-min gate catches intrabar crossovers — a stock that
      is recovering on the 1-min chart but hasn't yet shown it on a closed
      15-min bar.  NOW PUT (2025-06-15) is the canonical example: the
      15-min EMA was still bearish but the 1-min EMA(9) had already crossed
      above EMA(21) as the stock was recovering toward VWAP.

    Returns True (allow entry) if:
      • gate is disabled (ema_alignment_enabled=False)
      • fast and slow periods are the same (degenerate case)
      • not enough bars to compute both EMAs on a given timeframe
        (that timeframe is skipped — we don't block on missing data)
      • both timeframes agree with the entry direction
    """
    if not settings.ema_alignment_enabled:
        return True

    fast_period = settings.ema_fast_period  # default 9
    slow_period = settings.ema_period       # default 21

    if fast_period == slow_period:
        return True

    min_bars = max(fast_period, slow_period) + 1

    # ── 15-min check (primary) ───────────────────────────────────────────
    if len(bars_15m) >= min_bars:
        closes_15m = [b.close for b in bars_15m]
        vf_15 = [e for e in calculate_ema(closes_15m, fast_period) if e == e]
        vs_15 = [e for e in calculate_ema(closes_15m, slow_period)  if e == e]

        if vf_15 and vs_15:
            fast_15 = vf_15[-1]
            slow_15 = vs_15[-1]

            if direction == "CALL" and fast_15 <= slow_15:
                logger.info(
                    "[L1-EMA-align] CALL blocked — 15-min EMA(%d)=%.4f ≤ EMA(%d)=%.4f "
                    "(fast < slow = 15-min downtrend)",
                    fast_period, fast_15, slow_period, slow_15,
                )
                return False
            if direction == "PUT" and fast_15 >= slow_15:
                logger.info(
                    "[L1-EMA-align] PUT blocked — 15-min EMA(%d)=%.4f ≥ EMA(%d)=%.4f "
                    "(fast > slow = 15-min uptrend)",
                    fast_period, fast_15, slow_period, slow_15,
                )
                return False

    # ── 1-min check (confirming) ─────────────────────────────────────────
    # 15-min either passed or had insufficient bars.
    # If 1-min bars aren't available skip this check (don't block on missing data).
    if bars_1m and len(bars_1m) >= min_bars:
        closes_1m = [b.close for b in bars_1m]
        vf_1 = [e for e in calculate_ema(closes_1m, fast_period) if e == e]
        vs_1 = [e for e in calculate_ema(closes_1m, slow_period)  if e == e]

        if vf_1 and vs_1:
            fast_1  = vf_1[-1]
            slow_1  = vs_1[-1]
            ref     = slow_1 if slow_1 else fast_1
            spread  = abs(fast_1 - slow_1) / ref if ref else 0.0
            min_margin = settings.ema_1m_min_margin_pct

            if direction == "CALL" and fast_1 <= slow_1:
                if spread < min_margin:
                    logger.info(
                        "[L1-EMA-align] CALL — 1-min EMA(%d)=%.4f vs EMA(%d)=%.4f "
                        "spread=%.3f%% < %.1f%% threshold → treating 1-min as neutral, 15-min decides",
                        fast_period, fast_1, slow_period, slow_1,
                        spread * 100, min_margin * 100,
                    )
                else:
                    logger.info(
                        "[L1-EMA-align] CALL blocked — 1-min EMA(%d)=%.4f ≤ EMA(%d)=%.4f "
                        "(spread=%.3f%% ≥ %.1f%% — 1-min downtrend disagrees with 15-min)",
                        fast_period, fast_1, slow_period, slow_1,
                        spread * 100, min_margin * 100,
                    )
                    return False
            if direction == "PUT" and fast_1 >= slow_1:
                if spread < min_margin:
                    logger.info(
                        "[L1-EMA-align] PUT — 1-min EMA(%d)=%.4f vs EMA(%d)=%.4f "
                        "spread=%.3f%% < %.1f%% threshold → treating 1-min as neutral, 15-min decides",
                        fast_period, fast_1, slow_period, slow_1,
                        spread * 100, min_margin * 100,
                    )
                else:
                    logger.info(
                        "[L1-EMA-align] PUT blocked — 1-min EMA(%d)=%.4f ≥ EMA(%d)=%.4f "
                        "(spread=%.3f%% ≥ %.1f%% — 1-min uptrend disagrees with 15-min)",
                        fast_period, fast_1, slow_period, slow_1,
                        spread * 100, min_margin * 100,
                    )
                    return False

    return True


def is_ema_trend_confirmed(
    bars_15m: list[Bar],
    direction: str,
    period: int = 9,
    n: int = 2,
) -> bool:
    """
    Return True only if the last `n` completed 15-min bars ALL closed on
    the correct side of the EMA for `direction`.

    This prevents entering on a freshly-flipped EMA signal.  A single
    15-min bar crossing the EMA is not enough — the trend must have been
    consistently above (bullish) or below (bearish) the EMA for at least
    `n` consecutive bars before an entry is considered.

    n=0 disables the check (always returns True).
    """
    if direction not in ("bullish", "bearish"):
        return False
    if n <= 0:
        return True  # confirmation disabled — skip check

    closes   = [b.close for b in bars_15m]
    ema_vals = calculate_ema(closes, period)
    valid    = [(c, e) for c, e in zip(closes, ema_vals) if e == e]  # drop NaN

    if len(valid) < n:
        return False

    for close, ema in valid[-n:]:
        eps = ema * 0.00005
        if direction == "bullish" and not (close > ema + eps):
            return False
        if direction == "bearish" and not (close < ema - eps):
            return False

    return True


# ---------------------------------------------------------------------------
# Adaptive VWAP band — QQQ-based
# ---------------------------------------------------------------------------

def get_adaptive_vwap_band(qqq_bars_1m: list[Bar]) -> tuple[float, str, float]:
    """
    Return (band_pct, label, qqq_dist_signed) based on how extended QQQ is
    from its own session VWAP.

    On gap-up days the entire Nasdaq basket trades well above VWAP.
    The normal tight band would block every entry because no stock has
    pulled all the way back.  This function widens the band in proportion
    to QQQ's extension ABOVE VWAP, giving the strategy room to find
    pullbacks that are meaningful relative to the current market context.

    DIRECTION-AWARE: only widens on gap-up days (QQQ above VWAP).
    On gap-down days (QQQ below VWAP) the band stays at normal regardless
    of how extended QQQ is — PUT entries at extended lows snap back hard
    and don't benefit from a wider band.

    qqq_dist_signed is returned so callers can apply direction-aware entry
    filtering (e.g. block PUT entries that only qualify because the band
    widened on a gap-up day).

    Returns (band_pct, label, qqq_dist_signed) where label includes direction and distance:
      "normal (↑0.23% VWAP)"   — gap-up but within normal threshold
      "relaxed (↑0.80% VWAP)"  — gap-up, moderate extension
      "wider (↑1.90% VWAP)"    — gap-up, strong extension
      "normal (↓2.33% VWAP)"   — gap-DOWN, always normal band
    """
    if not settings.adaptive_band_enabled or not qqq_bars_1m:
        return settings.vwap_band_pct, "normal", 0.0

    qqq_vwap = calculate_vwap(qqq_bars_1m)
    if qqq_vwap == 0:
        return settings.vwap_band_pct, "normal", 0.0

    qqq_price       = qqq_bars_1m[-1].close
    qqq_dist_signed = (qqq_price - qqq_vwap) / qqq_vwap   # + = above VWAP, - = below

    # Direction-aware: only widen the band on gap-UP days (QQQ above VWAP).
    # The original abs() made the band widen equally on gap-down days, which
    # allowed PUT entries at already-extended lows that snap back fast.
    # On gap-down days (QQQ below VWAP) we cap at zero → normal band always.
    qqq_dist = max(qqq_dist_signed, 0.0)

    _dir = f"{'↑' if qqq_dist_signed >= 0 else '↓'}{abs(qqq_dist_signed)*100:.2f}% VWAP"
    if qqq_dist >= settings.adaptive_band_wider_threshold:
        return settings.vwap_band_wider_pct, f"wider ({_dir})", qqq_dist_signed
    if qqq_dist >= settings.adaptive_band_relaxed_threshold:
        return settings.vwap_band_relaxed_pct, f"relaxed ({_dir})", qqq_dist_signed
    return settings.vwap_band_pct, f"normal ({_dir})", qqq_dist_signed


# ---------------------------------------------------------------------------
# Layer 1 — check_entry_signal()
# Checks: trend_15min AND price_vs_vwap AND pullback_to_vwap
# ---------------------------------------------------------------------------

@dataclass
class EntrySignal:
    direction: str        # "CALL" | "PUT"
    current_price: float
    vwap: float
    trend: str


def check_entry_signal(
    bars_1m: list[Bar],
    bars_15m: list[Bar],
    ticker: str = "",
    band_pct: float | None = None,
) -> Optional[EntrySignal]:
    """
    Layer 1: three-condition AND gate (the configured UI indicators).
      (a) trend_15min   — 15-min EMA-9 is bullish or bearish (not neutral)
      (b) pullback_to_vwap — price is within vwap_band_pct of VWAP
      (c) price_vs_vwap — price is on the correct VWAP side for the trend
                          (above for CALL, below for PUT)

    Returns EntrySignal if all three pass, else None.
    Bounce and momentum checks are Layers 2 and 3 — run separately.
    """
    sym = f"[{ticker}] " if ticker else ""
    effective_band = band_pct if band_pct is not None else settings.vwap_band_pct

    if not bars_1m or not bars_15m:
        return None

    vwap = calculate_vwap(bars_1m)
    if vwap == 0:
        return None

    trend = ema_direction(bars_15m, settings.ema_period)
    if trend == "neutral":
        current_price = bars_1m[-1].close
        logger.info(
            "%s[L1] trend=neutral (price=%.2f vs EMA%d) — no signal",
            sym, current_price, settings.ema_period,
        )
        return None

    # Require N consecutive 15-min bars all on the correct EMA side.
    # Blocks entries when the EMA just flipped — the trend must be established.
    if not is_ema_trend_confirmed(
        bars_15m, trend, settings.ema_period, settings.ema_consecutive_bars
    ):
        logger.info(
            "%s[L1] EMA trend '%s' not yet confirmed for %d consecutive bars — skipping",
            sym, trend, settings.ema_consecutive_bars,
        )
        return None

    direction_prelim = "CALL" if trend == "bullish" else "PUT"
    current_price = bars_1m[-1].close

    # (b) price_vs_vwap — side check FIRST (hard gate, no band tolerance)
    # Price touching VWAP from the wrong side must never trigger an entry.
    if trend == "bullish" and current_price < vwap:
        logger.info(
            "%s[L1] CALL rejected — price %.2f is BELOW VWAP %.2f (wrong side for bullish bounce)",
            sym, current_price, vwap,
        )
        return None   # price below VWAP on bullish trend — not a valid bounce
    if trend == "bearish" and current_price > vwap:
        logger.info(
            "%s[L1] PUT rejected — price %.2f is ABOVE VWAP %.2f (wrong side for bearish drop)",
            sym, current_price, vwap,
        )
        return None   # price above VWAP on bearish trend — not a valid drop

    # (c) pullback_to_vwap — price must sit in the valid zone:
    #       min_clearance < distance_from_vwap < effective_band
    #
    #   Too far  (> band)          → blocked: waiting for pullback to VWAP
    #   Just right (in the donut)  → ✅ valid entry
    #   Too close (< min_clearance)→ blocked: AT RISK, no directional conviction
    distance_pct = abs(current_price - vwap) / vwap

    if distance_pct > effective_band:
        logger.info(
            "%s[L1] %s rejected — price %.2f is %.3f%% from VWAP %.2f "
            "(band=%.2f%% — waiting for pullback to VWAP)",
            sym, direction_prelim, current_price, distance_pct * 100, vwap,
            effective_band * 100,
        )
        return None

    direction = "CALL" if trend == "bullish" else "PUT"
    logger.info(
        "%s[L1] Signal: %s  price=%.2f  vwap=%.2f  distance=%.3f%%  trend=%s",
        sym, direction, current_price, vwap, distance_pct * 100, trend,
    )
    return EntrySignal(
        direction=direction,
        current_price=current_price,
        vwap=vwap,
        trend=trend,
    )


# ---------------------------------------------------------------------------
# Layer 2 — Multi-bar bounce confirmation (hardcoded)
# ---------------------------------------------------------------------------

def check_bounce_confirmation(
    bars_1m: list[Bar],
    direction: str,
    vwap: float,
    n: Optional[int] = None,
) -> bool:
    """
    The last `n` *completed* 1-min bars must all close on the correct VWAP side.
    bars_1m[-1] is the in-progress bar and is always excluded.

    CALL: last n completed bars all closed ABOVE vwap  → sustained bounce
    PUT:  last n completed bars all closed BELOW vwap  → sustained rejection

    A single tick through VWAP doesn't count — this requires consecutive
    confirmed closes to filter out false spikes.
    """
    n = n or settings.bounce_bars_required
    if len(bars_1m) < n + 1 or vwap <= 0:
        logger.debug("[L2] Not enough bars for bounce check (need %d + 1 got %d)", n, len(bars_1m))
        return False

    confirm_bars = bars_1m[-(n + 1):-1]   # last n completed bars
    closes = [round(b.close, 2) for b in confirm_bars]

    if direction == "CALL":
        ok = all(b.close > vwap for b in confirm_bars)
        if not ok:
            logger.info(
                "[L2] Bounce NOT confirmed for CALL — need %d closes above VWAP %.2f. "
                "Last closes: %s",
                n, vwap, closes,
            )
        return ok
    else:  # PUT
        ok = all(b.close < vwap for b in confirm_bars)
        if not ok:
            logger.info(
                "[L2] Bounce NOT confirmed for PUT — need %d closes below VWAP %.2f. "
                "Last closes: %s",
                n, vwap, closes,
            )
        return ok


# ---------------------------------------------------------------------------
# Layer 3 — Momentum candle (hardcoded)
# ---------------------------------------------------------------------------

def check_momentum_candle(bars_1m: list[Bar], direction: str, ticker: str = "") -> bool:
    """
    June 9 version: the last completed 1-min bar must be a momentum candle.

      CALL: bar is green (close > open) AND close > previous bar's close
      PUT:  bar is red   (close < open) AND close < previous bar's close

    Both conditions must be met — color confirms intrabar conviction,
    falling/rising close confirms the direction vs the prior bar.
    """
    sym = f"[{ticker}] " if ticker else ""

    if len(bars_1m) < 3:
        logger.debug("%s[L3] Not enough bars for momentum check", sym)
        return False

    bar      = bars_1m[-2]   # most recently completed bar
    prev_bar = bars_1m[-3]   # bar before that

    is_red   = bar.close < bar.open
    is_green = bar.close > bar.open
    falling  = bar.close < prev_bar.close
    rising   = bar.close > prev_bar.close

    if direction == "PUT":
        ok = is_red and falling
        if not ok:
            logger.info(
                "%s[L3] Momentum NOT confirmed for PUT — "
                "last bar close=%.2f open=%.2f prev_close=%.2f "
                "(red=%s falling=%s). Waiting for downward momentum.",
                sym, bar.close, bar.open, prev_bar.close, is_red, falling,
            )
        return ok
    else:  # CALL
        ok = is_green and rising
        if not ok:
            logger.info(
                "%s[L3] Momentum NOT confirmed for CALL — "
                "last bar close=%.2f open=%.2f prev_close=%.2f "
                "(green=%s rising=%s). Waiting for upward momentum.",
                sym, bar.close, bar.open, prev_bar.close, is_green, rising,
            )
        return ok


# ---------------------------------------------------------------------------
# Layer 4 — Intraday VWAP slope (hardcoded)
# ---------------------------------------------------------------------------

def check_vwap_slope(
    bars_1m: list[Bar],
    direction: str,
    lookback: Optional[int] = None,
    threshold_pct: Optional[float] = None,
) -> bool:
    """
    Compares session VWAP now vs `lookback` 1-min bars ago.

    If VWAP itself is declining for a CALL, or rising for a PUT, the intraday
    order flow opposes the trade — entry is blocked even if Layers 1–3 pass.

    The 15-min EMA (Layer 1) can lag because it includes prior-day data. VWAP
    slope captures the *same-session* directional reality that lagging EMAs miss.

    Returns True  → slope is acceptable, entry allowed.
    Returns False → slope opposes direction, entry blocked.
    """
    lookback      = lookback      or settings.vwap_slope_lookback_bars
    threshold_pct = threshold_pct or settings.vwap_slope_threshold_pct

    if len(bars_1m) < lookback + 5:
        # Too early in session — not enough bars to compute a meaningful slope
        return True

    vwap_now  = calculate_vwap(bars_1m)
    vwap_then = calculate_vwap(bars_1m[:-lookback])

    if not vwap_now or not vwap_then or vwap_then == 0:
        return True

    slope_pct = (vwap_now - vwap_then) / vwap_then * 100

    if direction == "CALL" and slope_pct < -threshold_pct:
        logger.info(
            "[L4] VWAP slope BEARISH (%.3f%%) — blocking CALL. "
            "Intraday order flow is declining.",
            slope_pct,
        )
        return False

    if direction == "PUT" and slope_pct > threshold_pct:
        logger.info(
            "[L4] VWAP slope BULLISH (+%.3f%%) — blocking PUT. "
            "Intraday order flow is rising.",
            slope_pct,
        )
        return False

    logger.debug("[L4] VWAP slope %+.3f%% — OK for %s", slope_pct, direction)
    return True


# ---------------------------------------------------------------------------
# Layer 5 — Market regime gate (QQQ VWAP position, real-time)
# ---------------------------------------------------------------------------

def get_regime_from_vwap(qqq_bars_1m: list) -> str:
    """
    Determine market regime from QQQ's real-time position vs its session VWAP.

    This replaces the old SPY 15-min EMA approach, which lagged intraday
    reversals by 30-45 minutes.  QQQ VWAP tells us WHERE the market's
    average participant is positioned right now, not where they were
    30 minutes ago.

    Thresholds (regime_vwap_threshold, default 0.2%):
      QQQ > VWAP + threshold  → BULLISH  → block PUT  entries
      QQQ < VWAP − threshold  → BEARISH  → block CALL entries
      |QQQ − VWAP| < threshold → NEUTRAL  → allow all (choppy/transition)

    No API call — uses pre-fetched QQQ 1-min bars from the adaptive band
    fetch, so there is zero extra cost per scan cycle.

    Advantages over EMA approach:
      - Real-time: reacts within 1 bar (1 min) not 15-min EMA lag
      - No circular reference: always QQQ regardless of which ticker is scanned
      - Consistent with VWAP pullback philosophy used throughout the strategy
    """
    if not settings.regime_gate_enabled or not qqq_bars_1m:
        return "neutral"

    vwap = calculate_vwap(qqq_bars_1m)
    if not vwap or vwap == 0:
        return "neutral"

    current_price = qqq_bars_1m[-1].close
    if not current_price:
        return "neutral"

    dist_pct = (current_price - vwap) / vwap
    threshold = settings.regime_vwap_threshold  # default 0.2%

    if dist_pct > threshold:
        logger.debug(
            "[L5] QQQ BULLISH regime — price %.2f is +%.3f%% above VWAP %.2f "
            "(threshold=%.2f%%)",
            current_price, dist_pct * 100, vwap, threshold * 100,
        )
        return "bullish"
    elif dist_pct < -threshold:
        logger.debug(
            "[L5] QQQ BEARISH regime — price %.2f is %.3f%% below VWAP %.2f "
            "(threshold=%.2f%%)",
            current_price, abs(dist_pct) * 100, vwap, threshold * 100,
        )
        return "bearish"
    else:
        logger.debug(
            "[L5] QQQ NEUTRAL regime — price %.2f is %+.3f%% from VWAP %.2f "
            "(within ±%.2f%% threshold)",
            current_price, dist_pct * 100, vwap, threshold * 100,
        )
        return "neutral"


# ---------------------------------------------------------------------------
# Trend reversal confirmation (exit-side consecutive-bar check)
# ---------------------------------------------------------------------------

def is_trend_reversal_confirmed(
    bars_15m: list[Bar],
    direction: str,
    period: int = 21,
    n: int = 2,
) -> bool:
    """
    Return True only if the last `n` 15-min bars ALL closed on the OPPOSITE
    side of the EMA from `direction` — confirming a genuine reversal rather
    than a single noisy candle wiggling through the EMA.

      CALL trade: reversal confirmed when last n bars all close BELOW EMA
      PUT  trade: reversal confirmed when last n bars all close ABOVE EMA

    n=1 → original single-bar behaviour (any one bar fires the exit)
    n=2 → two consecutive bars required (default — filters midday chop)
    n=0 → TREND_REVERSAL exit disabled entirely (always returns False)

    This is the mirror image of is_ema_trend_confirmed() used at entry.
    Both functions share the same consecutive-bar EMA logic; the entry check
    asks "are bars confirming the trade direction?" while this check asks
    "are bars confirming the reversal away from it?"
    """
    if direction not in ("CALL", "PUT"):
        return False
    if n <= 0:
        return False   # reversal exit disabled

    # Map trade direction → the EMA side that constitutes a reversal
    reversal_direction = "bearish" if direction == "CALL" else "bullish"
    return is_ema_trend_confirmed(bars_15m, reversal_direction, period, n)


# ---------------------------------------------------------------------------
# Exit conditions
# ---------------------------------------------------------------------------

@dataclass
class ExitCondition:
    reason: str       # matches ExitReason enum values
    close_all: bool   # True = close full position; False = partial (TP1)
    quantity: int = 0  # qty to close (0 = use remaining_qty from trade)


def check_exit_conditions(
    *,
    direction: str,
    entry_price: float,
    current_option_price: float,
    stop_price: float,
    tp1_price: float,
    tp2_price: float,
    tp1_hit: bool,
    vwap_at_entry: float,
    current_underlying: float,
    bars_15m: list[Bar],
    remaining_qty: int,
    entry_time: Optional[datetime] = None,
    now: Optional[datetime] = None,
    stop_eval_price: Optional[float] = None,
) -> Optional[ExitCondition]:
    """
    Evaluate exit conditions in priority order (v1 — single-target, 100% exit):
    1. Stop loss             — option drops to stop_price
    2. Profit target (TP)    — option rises to tp2_price (= TAKE_PROFIT_PCT above entry)
    3. VWAP break            — underlying crosses VWAP against trade direction
    4. Trend reversal        — 15-min EMA flips against direction
                               (suppressed for TREND_REVERSAL_MIN_HOLD_MINUTES after entry
                                so a single choppy bar can't exit a brand-new position)

    Two prices serve different purposes:
      current_option_price — BID price, used for TP evaluation.
          TP only fires when you can actually receive the target (bid must reach it).
      stop_eval_price      — MID price, used for stop / trailing-stop evaluation.
          Stops use mid to prevent premature triggers from the bid temporarily dipping
          below the stop level due to wide bid-ask spreads (e.g. META trailing stop
          firing at breakeven when bid=$5.95 but mid=$6.22 > stop=$6.07).
          Defaults to current_option_price when not supplied.
    """
    tp_price        = current_option_price          # bid  — for profit target checks
    stop_price_eval = stop_eval_price if stop_eval_price is not None \
                      else current_option_price  # mid  — for stop checks

    # 1. Quick-loss exit — fires in the first N minutes if the option falls fast.
    #    Near-expiry ATM options have high gamma: a 0.5 % adverse underlying move
    #    can cause a 25–30 % option loss before VWAP_BREAK's threshold is reached.
    #    Exiting at -12 % within 5 min saves the difference vs the -27 % hard stop.
    if (
        settings.quick_loss_pct > 0
        and entry_time is not None
        and stop_price_eval < entry_price  # option is already losing
    ):
        _now   = now or datetime.now(tz=timezone.utc)
        _entry = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        held_minutes = (_now - _entry).total_seconds() / 60
        if held_minutes <= settings.quick_loss_max_minutes:
            # Respect the quiet period: don't arm quick-loss until the trade
            # has been open for at least quick_loss_min_hold_minutes.
            # The broker hard-stop at Tradier is still active during this window.
            min_hold = settings.quick_loss_min_hold_minutes
            if min_hold > 0 and held_minutes < min_hold:
                pass  # too young — let the trade breathe
            else:
                loss_pct = (entry_price - stop_price_eval) / entry_price
                if loss_pct >= settings.quick_loss_pct:
                    logger.info(
                        "[exit] QUICK_LOSS — option down %.1f%% (≥%.0f%% threshold) "
                        "at %.1f min of entry (window %d–%d min). Entry=%.2f current=%.2f",
                        loss_pct * 100, settings.quick_loss_pct * 100,
                        held_minutes, min_hold, settings.quick_loss_max_minutes,
                        entry_price, stop_price_eval,
                    )
                    return ExitCondition(reason="QUICK_LOSS", close_all=True)

    # 2. Hard stop loss
    #    Suppressed for stop_loss_min_hold_minutes after entry so the trade
    #    can breathe past initial bid-ask noise and price discovery.
    #    The quick-loss check above remains active as the emergency brake
    #    during this quiet window.
    #    Label distinguishes the original hard stop from a trailing stop that
    #    was raised above it (profit-lock) — keeps exit-reason analytics honest.
    _stop_suppressed = False
    if settings.stop_loss_min_hold_minutes > 0 and entry_time is not None:
        _now_s  = now or datetime.now(tz=timezone.utc)
        _entr_s = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        _held_m = (_now_s - _entr_s).total_seconds() / 60
        if _held_m < settings.stop_loss_min_hold_minutes:
            _stop_suppressed = True
            logger.debug(
                "[exit] Hard stop suppressed — %.1f min into trade "
                "(min hold=%d min). current=%.2f stop=%.2f",
                _held_m, settings.stop_loss_min_hold_minutes,
                stop_price_eval, stop_price,
            )

    if not _stop_suppressed and stop_price_eval <= stop_price:
        original_stop = round(entry_price * (1 - settings.stop_loss_pct), 2)
        reason = "TRAILING_STOP" if stop_price > original_stop else "STOP"
        return ExitCondition(reason=reason, close_all=True)

    # 3. Profit target — close 100 % of position
    if tp_price >= tp2_price:
        return ExitCondition(reason="TP2", close_all=True)

    # 4. VWAP break — uses a tighter exit band than the entry band.
    #    Entry band (vwap_band_pct) is intentionally wide to catch pullbacks.
    #    Exit band (vwap_exit_band_pct) must be much tighter so it fires
    #    before the hard stop on high-gamma options.
    #    e.g. entry band 0.9 % vs exit band 0.3 %: for $580 SPY, VWAP_BREAK
    #    fires when underlying drops $1.74 past entry VWAP instead of $5.22.
    if vwap_at_entry and vwap_at_entry > 0:
        exit_band = settings.vwap_exit_band_pct if settings.vwap_exit_band_pct > 0 \
                    else settings.vwap_band_pct
        band = vwap_at_entry * exit_band
        if direction == "CALL" and current_underlying < vwap_at_entry - band:
            logger.info(
                "[exit] VWAP_BREAK CALL — underlying %.4f dropped %.3f%% past entry VWAP %.4f "
                "(exit band=%.2f%%)",
                current_underlying,
                (vwap_at_entry - current_underlying) / vwap_at_entry * 100,
                vwap_at_entry, exit_band * 100,
            )
            return ExitCondition(reason="VWAP_BREAK", close_all=True)
        if direction == "PUT" and current_underlying > vwap_at_entry + band:
            logger.info(
                "[exit] VWAP_BREAK PUT — underlying %.4f rose %.3f%% past entry VWAP %.4f "
                "(exit band=%.2f%%)",
                current_underlying,
                (current_underlying - vwap_at_entry) / vwap_at_entry * 100,
                vwap_at_entry, exit_band * 100,
            )
            return ExitCondition(reason="VWAP_BREAK", close_all=True)

    # 5. Trend reversal (15-min EMA flips against trade direction)
    #
    #    Guard A — minimum hold time (TREND_REVERSAL_MIN_HOLD_MINUTES, default 10 min):
    #      Suppressed only while the trade is PROFITABLE within the hold window.
    #      If the option is already at a loss, reversal can fire immediately.
    #      This prevents whipsaw exits on entries that tick up before settling.
    #
    #    Guard B — consecutive-bar confirmation (TREND_REVERSAL_CONFIRM_BARS, default 1):
    #      Now 1 bar (down from 2).  With the EMA alignment gate at entry
    #      (requiring EMA9 > EMA21 for CALLs), we enter into double-confirmed
    #      trends — so a single bearish bar is a meaningful reversal signal,
    #      not noise.  Requiring 2 bars (30+ min) meant TREND_REVERSAL could
    #      never fire before the hard stop on short-duration losing trades.
    #
    #    Guard C (VWAP confirm) — REMOVED.
    #      Previously suppressed TREND_REVERSAL when underlying was still above
    #      entry VWAP, to distinguish EMA dips from genuine reversals.
    #      In practice this blocked all TREND_REVERSAL exits: we enter near VWAP,
    #      so the underlying is often still at/above VWAP even as the option
    #      deteriorates via gamma.  VWAP_BREAK already handles the VWAP-cross
    #      exit — TREND_REVERSAL should fire on EMA alone.
    trend = ema_direction(bars_15m, settings.ema_period)
    trend_reversal_blocked = False
    if settings.trend_reversal_min_hold_minutes > 0 and entry_time is not None:
        _now   = now or datetime.now(tz=timezone.utc)
        _entry = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        held_minutes = (_now - _entry).total_seconds() / 60
        if held_minutes < settings.trend_reversal_min_hold_minutes:
            # Only block while the trade is still profitable — if we're already
            # losing, there is nothing to protect and we should exit on reversal.
            if current_option_price >= entry_price:
                trend_reversal_blocked = True

    if not trend_reversal_blocked:
        if direction == "CALL" and trend == "bearish":
            if is_trend_reversal_confirmed(
                bars_15m, "CALL", settings.ema_period, settings.trend_reversal_confirm_bars
            ):
                logger.info(
                    "[exit] TREND_REVERSAL confirmed for CALL — "
                    "%d bar(s) below EMA, underlying=%.4f entry_vwap=%.4f",
                    settings.trend_reversal_confirm_bars,
                    current_underlying, vwap_at_entry,
                )
                return ExitCondition(reason="TREND_REVERSAL", close_all=True)

        if direction == "PUT" and trend == "bullish":
            if is_trend_reversal_confirmed(
                bars_15m, "PUT", settings.ema_period, settings.trend_reversal_confirm_bars
            ):
                logger.info(
                    "[exit] TREND_REVERSAL confirmed for PUT — "
                    "%d bar(s) above EMA, underlying=%.4f entry_vwap=%.4f",
                    settings.trend_reversal_confirm_bars,
                    current_underlying, vwap_at_entry,
                )
                return ExitCondition(reason="TREND_REVERSAL", close_all=True)

    return None


# ---------------------------------------------------------------------------
# TP / stop price computation
# ---------------------------------------------------------------------------

def compute_trade_levels(entry_price: float, direction: str) -> dict:
    """
    Simple percentage-based TP and stop (v1 — book everything at one level).

      stop = entry × (1 − STOP_LOSS_PCT)    e.g. entry $5 × (1 − 0.25) = $3.75
      tp   = entry × (1 + TAKE_PROFIT_PCT)  e.g. entry $5 × (1 + 0.35) = $6.75

    tp1_price and tp2_price are both set to the same target so the rest of
    the codebase (DB schema, exit checker) keeps working without changes.
    """
    stop = round(entry_price * (1 - settings.stop_loss_pct), 2)
    tp   = round(entry_price * (1 + settings.take_profit_pct), 2)
    return {"stop_price": stop, "tp1_price": tp, "tp2_price": tp}


def compute_trailing_stop(
    entry_price: float,
    current_option_price: float,
    current_stop: float,
    entry_time: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> float:
    """
    Return the new (possibly raised) stop price based on trailing-stop rules.

    Two stages — both triggered only when the option is *above* entry:

      Stage 1 — profit lock  (TRAILING_STOP_BREAKEVEN_PCT, default 6%)
        Once the option is ≥ 6% above entry, raise the stop to:
          entry × (1 + TRAILING_STOP_LOCK_PROFIT_PCT)
        With the default 1% lock this gives entry + 1% — covering commission
        and guaranteeing a small profit even if the trade reverses.
        Set TRAILING_STOP_LOCK_PROFIT_PCT=0 to restore pure breakeven behaviour.

      Stage 2 — trail from current  (TRAILING_STOP_TRAIL_PCT, default 10%)
        Once the option is ≥ 10% above entry, set:
          stop = current_option_price × (1 − TRAILING_STOP_TRAIL_FROM_CURRENT_PCT)
        With the default 10% buffer the stop floors 10% below wherever the
        option is trading right now.  As the price rises, the floor rises too.

        Example: entry $5.00, option now $6.00 (+20%)
          stop = $6.00 × (1 − 0.10) = $5.40

    Minimum hold time (TRAILING_STOP_MIN_HOLD_MINUTES, default 15 min):
        Neither stage fires until the trade has been open for at least this
        long.  This prevents the stop from jumping to breakeven on the very
        first management tick when the option is already above the threshold
        at entry (common with limit orders entered at the mid-price).

    The stop price NEVER moves down — if the computed candidate is lower than
    the current stop the existing stop is preserved.

    Returns the current_stop unchanged when neither threshold is reached.
    Setting both thresholds to 0 in config disables trailing stops entirely.
    """
    if not entry_price or not current_option_price:
        return current_stop

    # Minimum hold time guard: don't activate trailing stop until the trade
    # has been open long enough.  This prevents the breakeven floor from
    # firing on the very first tick when we entered at mid (below ask) and
    # the option was already above the % threshold at the moment of entry.
    min_hold = settings.trailing_stop_min_hold_minutes
    if min_hold > 0 and entry_time is not None:
        _now = now or datetime.now(tz=timezone.utc)
        _entry = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
        held_minutes = (_now - _entry).total_seconds() / 60
        if held_minutes < min_hold:
            return current_stop

    gain_pct = (current_option_price - entry_price) / entry_price

    trail_pct     = settings.trailing_stop_trail_pct
    breakeven_pct = settings.trailing_stop_breakeven_pct

    # Both disabled
    if trail_pct <= 0 and breakeven_pct <= 0:
        return current_stop

    if trail_pct > 0 and gain_pct >= trail_pct:
        # Stage 2: trail N% below current option price (dynamic floor)
        candidate = round(
            current_option_price * (1 - settings.trailing_stop_trail_from_current_pct), 2
        )
    elif breakeven_pct > 0 and gain_pct >= breakeven_pct:
        # Stage 1: lock in entry + lock_profit_pct (default 1% above entry)
        candidate = round(entry_price * (1 + settings.trailing_stop_lock_profit_pct), 2)
    else:
        # Below both thresholds — no change
        return current_stop

    # Stop only moves up
    return max(candidate, current_stop)


# ---------------------------------------------------------------------------
# Trading window helpers
# ---------------------------------------------------------------------------

def is_past_cutoff(now: Optional[datetime] = None) -> bool:
    """Return True if current ET time is at or past TRADING_END_TIME."""
    now = now or datetime.now(tz=timezone.utc)
    et  = now.astimezone(ET)
    end_h, end_m = settings.cutoff_hour, settings.cutoff_minute
    return et.hour > end_h or (et.hour == end_h and et.minute >= end_m)


def is_before_trading_start(now: Optional[datetime] = None) -> bool:
    """Return True if current ET time is before TRADING_START_TIME."""
    now = now or datetime.now(tz=timezone.utc)
    et  = now.astimezone(ET)
    start_h, start_m = settings.start_hour, settings.start_minute
    return et.hour < start_h or (et.hour == start_h and et.minute < start_m)


def is_market_open(now: Optional[datetime] = None) -> bool:
    """Return True if ET time is between 9:30 AM and 4:00 PM on a weekday."""
    now = now or datetime.now(tz=timezone.utc)
    et  = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    open_minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= open_minutes < 16 * 60


def is_past_last_entry_time(now: Optional[datetime] = None) -> bool:
    """Return True if current ET time is at or past LAST_ENTRY_TIME.

    LAST_ENTRY_TIME is earlier than TRADING_END_TIME — it blocks NEW entries
    while allowing existing trades to continue running until the cutoff.
    """
    now = now or datetime.now(tz=timezone.utc)
    et  = now.astimezone(ET)
    h, m = settings.last_entry_hour, settings.last_entry_minute
    return et.hour > h or (et.hour == h and et.minute >= m)


def is_in_trading_window(now: Optional[datetime] = None) -> bool:
    """
    Return True if within the configured entry window (start → last_entry_time),
    and NOT inside the lunch-hour noise filter (if enabled).

    Two separate time boundaries control the session:
    - LAST_ENTRY_TIME  — no new entries after this (default 14:15 ET)
    - TRADING_END_TIME — all open positions are force-closed at this time (default 14:45 ET)

    This gives any trade entered just before LAST_ENTRY_TIME a full 30 minutes
    to develop before the cutoff close.
    """
    if not is_market_open(now) or is_before_trading_start(now) or is_past_last_entry_time(now):
        return False

    if settings.lunch_break_enabled:
        _now = now or datetime.now(tz=timezone.utc)
        et   = _now.astimezone(ET)
        current_mins = et.hour * 60 + et.minute

        lb_start_h, lb_start_m = map(int, settings.lunch_break_start.split(":"))
        lb_end_h,   lb_end_m   = map(int, settings.lunch_break_end.split(":"))
        lb_start_mins = lb_start_h * 60 + lb_start_m
        lb_end_mins   = lb_end_h   * 60 + lb_end_m

        if lb_start_mins <= current_mins < lb_end_mins:
            logger.debug(
                "[window] Lunch break (%s–%s ET) — blocking new entries",
                settings.lunch_break_start, settings.lunch_break_end,
            )
            return False

    return True
