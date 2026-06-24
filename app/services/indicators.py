"""
Indicator registry, seed data, and evaluation logic.

Each indicator has:
  - A static definition (key, name, description, category, default_active)
  - An async evaluate() function that returns IndicatorResult

The 4 "core strategy" indicators are active by default:
  trend_15min, price_vs_vwap, pullback_to_vwap, volume_spike

PCR and RSI are available but off by default (good add-ons).
news_based_vwap is defined but its evaluate() is a no-op stub.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.services.strategy import (
    Bar,
    calculate_ema,
    calculate_vwap,
    ema_direction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IndicatorResult:
    key: str
    fires: bool             # True = this indicator agrees with `direction`
    direction: str          # "CALL" | "PUT" — what direction was checked
    value: Optional[float]  # raw numeric value (RSI score, PCR, etc.)
    reason: str             # human-readable explanation shown in logs/UI


# ---------------------------------------------------------------------------
# Static registry (source of truth for seed data)
# ---------------------------------------------------------------------------

INDICATOR_REGISTRY: list[dict] = [
    {
        "key": "trend_15min",
        "name": "Trend (15-min)",
        "description": (
            "Price above EMA-9 with higher highs (bullish) or below "
            "with lower lows (bearish)"
        ),
        "category": "trend",
        "default_active": True,   # ← part of core strategy
    },
    {
        "key": "price_vs_vwap",
        "name": "Price vs VWAP",
        "description": "Price above VWAP for calls, below VWAP for puts",
        "category": "trend",
        "default_active": True,
    },
    {
        "key": "pullback_to_vwap",
        "name": "VWAP Pullback",
        "description": "Price retraces within VWAP_BAND_PCT of VWAP before continuing in trend direction",
        "category": "entry",
        "default_active": True,
    },
    {
        "key": "volume_spike",
        "name": "Volume Spike",
        "description": (
            f"Current bar volume ≥ {settings.volume_spike_multiplier:.0f}× "
            f"the {settings.volume_spike_lookback}-bar rolling average "
            "(bounce confirmation)"
        ),
        "category": "entry",
        "default_active": True,
    },
    {
        "key": "pcr",
        "name": "PCR Confirmation",
        "description": (
            f"Put-Call Ratio > {settings.pcr_bullish_above} confirms bullish sentiment; "
            f"< {settings.pcr_bearish_below} confirms bearish"
        ),
        "category": "sentiment",
        "default_active": False,
    },
    {
        "key": "rsi",
        "name": "RSI (14)",
        "description": (
            f"RSI < {settings.rsi_oversold:.0f} confirms bullish momentum (oversold); "
            f"RSI > {settings.rsi_overbought:.0f} confirms bearish momentum (overbought). "
            "Thresholds configurable via RSI_OVERSOLD / RSI_OVERBOUGHT in .env."
        ),
        "category": "momentum",
        "default_active": False,
    },
    {
        "key": "news_based_vwap",
        "name": "News-Based VWAP",
        "description": (
            "Uses MarketAUX news sentiment (positive → CALL, negative → PUT) "
            "confirmed by VWAP position. Requires MARKETAUX_API_KEY in .env."
        ),
        "category": "sentiment",
        "default_active": False,
    },
]

# Quick lookup by key
REGISTRY_BY_KEY: dict[str, dict] = {r["key"]: r for r in INDICATOR_REGISTRY}


# ---------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------

async def seed_indicators(db) -> None:
    """
    Insert default indicators if they don't already exist.
    Idempotent — safe to call on every startup.
    """
    from sqlalchemy import select
    from app.models import Indicator

    for defn in INDICATOR_REGISTRY:
        existing = await db.execute(
            select(Indicator).where(Indicator.key == defn["key"])
        )
        if existing.scalar_one_or_none():
            continue  # already seeded

        ind = Indicator(
            key=defn["key"],
            name=defn["name"],
            description=defn["description"],
            category=defn["category"],
            active=defn["default_active"],
        )
        db.add(ind)

    await db.commit()


# ---------------------------------------------------------------------------
# RSI calculation
# ---------------------------------------------------------------------------

def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """
    Wilder's RSI.  Returns NaN if not enough data.
    """
    if len(closes) < period + 1:
        return float("nan")

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    # Initial averages (simple mean over first `period` values)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for the rest
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Individual evaluators
# ---------------------------------------------------------------------------

def eval_trend_15min(bars_15m: list[Bar], direction: str) -> IndicatorResult:
    """EMA-9 on 15-min bars; confirm bullish/bearish alignment."""
    period = settings.ema_period
    if len(bars_15m) < period + 2:
        return IndicatorResult(
            key="trend_15min", fires=False, direction=direction,
            value=None, reason="Not enough 15-min bars"
        )

    closes = [b.close for b in bars_15m]
    ema_vals = calculate_ema(closes, period)
    valid = [(c, e) for c, e in zip(closes, ema_vals) if e == e]
    if len(valid) < 2:
        return IndicatorResult(
            key="trend_15min", fires=False, direction=direction,
            value=None, reason="EMA not yet seeded"
        )

    price_now, ema_now = valid[-1]
    price_prev, ema_prev = valid[-2]

    bullish = price_now > ema_now and price_now > price_prev
    bearish = price_now < ema_now and price_now < price_prev

    trend = "bullish" if bullish else ("bearish" if bearish else "neutral")
    fires = (direction == "CALL" and bullish) or (direction == "PUT" and bearish)

    return IndicatorResult(
        key="trend_15min",
        fires=fires,
        direction=direction,
        value=round(ema_now, 4),
        reason=f"EMA-9={ema_now:.2f}  price={price_now:.2f}  trend={trend}",
    )


def eval_price_vs_vwap(bars_1m: list[Bar], direction: str) -> IndicatorResult:
    """Price must be on the correct side of VWAP for the trade direction."""
    if not bars_1m:
        return IndicatorResult(
            key="price_vs_vwap", fires=False, direction=direction,
            value=None, reason="No bars"
        )

    vwap = calculate_vwap(bars_1m)
    price = bars_1m[-1].close
    if vwap == 0:
        return IndicatorResult(
            key="price_vs_vwap", fires=False, direction=direction,
            value=None, reason="VWAP=0 (no volume data)"
        )

    fires = (direction == "CALL" and price > vwap) or (direction == "PUT" and price < vwap)
    side = "above" if price > vwap else "below"
    return IndicatorResult(
        key="price_vs_vwap",
        fires=fires,
        direction=direction,
        value=round(vwap, 4),
        reason=f"price={price:.2f}  VWAP={vwap:.2f}  ({side} VWAP)",
    )


def eval_pullback_to_vwap(bars_1m: list[Bar], direction: str) -> IndicatorResult:
    """Price must currently be within VWAP_BAND_PCT of VWAP (the pullback zone)."""
    if not bars_1m:
        return IndicatorResult(
            key="pullback_to_vwap", fires=False, direction=direction,
            value=None, reason="No bars"
        )

    vwap = calculate_vwap(bars_1m)
    price = bars_1m[-1].close
    if vwap == 0:
        return IndicatorResult(
            key="pullback_to_vwap", fires=False, direction=direction,
            value=None, reason="VWAP=0"
        )

    dist_pct = abs(price - vwap) / vwap
    band = settings.vwap_band_pct
    fires = dist_pct <= band
    return IndicatorResult(
        key="pullback_to_vwap",
        fires=fires,
        direction=direction,
        value=round(dist_pct * 100, 4),
        reason=(
            f"price={price:.2f}  VWAP={vwap:.2f}  "
            f"dist={dist_pct*100:.3f}%  "
            f"(threshold={band*100:.2f}%)"
        ),
    )


def eval_volume_spike(bars_1m: list[Bar], direction: str) -> IndicatorResult:
    """Current bar volume ≥ N × rolling average of last K bars."""
    lookback = settings.volume_spike_lookback
    mult = settings.volume_spike_multiplier

    if len(bars_1m) < lookback + 1:
        return IndicatorResult(
            key="volume_spike", fires=False, direction=direction,
            value=None, reason=f"Need ≥{lookback+1} bars, have {len(bars_1m)}"
        )

    current_vol = bars_1m[-1].volume
    avg_vol = sum(b.volume for b in bars_1m[-lookback - 1:-1]) / lookback

    if avg_vol == 0:
        return IndicatorResult(
            key="volume_spike", fires=False, direction=direction,
            value=None, reason="Avg volume=0"
        )

    ratio = current_vol / avg_vol
    fires = ratio >= mult
    return IndicatorResult(
        key="volume_spike",
        fires=fires,
        direction=direction,
        value=round(ratio, 2),
        reason=f"vol={current_vol}  avg={avg_vol:.0f}  ratio={ratio:.2f}× (need {mult}×)",
    )


def eval_rsi(bars_1m: list[Bar], direction: str) -> IndicatorResult:
    """RSI(14) on 1-min closes."""
    period = settings.rsi_period
    closes = [b.close for b in bars_1m]
    rsi = calculate_rsi(closes, period)

    if rsi != rsi:  # NaN
        return IndicatorResult(
            key="rsi", fires=False, direction=direction,
            value=None, reason=f"Need ≥{period+1} bars, have {len(bars_1m)}"
        )

    oversold = settings.rsi_oversold
    overbought = settings.rsi_overbought

    if direction == "CALL":
        fires = rsi < oversold
        reason = f"RSI={rsi:.1f}  (oversold threshold={oversold})"
    else:
        fires = rsi > overbought
        reason = f"RSI={rsi:.1f}  (overbought threshold={overbought})"

    return IndicatorResult(
        key="rsi", fires=fires, direction=direction,
        value=round(rsi, 2), reason=reason
    )


def eval_pcr(
    calls: list,   # list[OptionQuote]
    puts: list,    # list[OptionQuote]
    direction: str,
) -> IndicatorResult:
    """
    Put-Call Ratio from options chain volume.
    Contrarian interpretation:
      PCR > pcr_bullish_above → fear → bullish signal for CALLs
      PCR < pcr_bearish_below → greed → bearish signal for PUTs
    """
    total_call_vol = sum(getattr(o, "volume", 0) for o in calls)
    total_put_vol = sum(getattr(o, "volume", 0) for o in puts)

    if total_call_vol == 0:
        return IndicatorResult(
            key="pcr", fires=False, direction=direction,
            value=None, reason="No call volume data"
        )

    pcr = total_put_vol / total_call_vol
    bull_thresh = settings.pcr_bullish_above
    bear_thresh = settings.pcr_bearish_below

    if direction == "CALL":
        fires = pcr > bull_thresh
        reason = f"PCR={pcr:.2f}  (bullish if > {bull_thresh})"
    else:
        fires = pcr < bear_thresh
        reason = f"PCR={pcr:.2f}  (bearish if < {bear_thresh})"

    return IndicatorResult(
        key="pcr", fires=fires, direction=direction,
        value=round(pcr, 3), reason=reason
    )


def eval_news_based_vwap(direction: str) -> IndicatorResult:
    """Stub — requires MarketAUX integration (not yet implemented)."""
    return IndicatorResult(
        key="news_based_vwap", fires=False, direction=direction,
        value=None, reason="Not implemented — MarketAUX integration pending"
    )


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def evaluate_indicator(
    key: str,
    direction: str,
    bars_1m: list[Bar] | None = None,
    bars_15m: list[Bar] | None = None,
    option_calls: list | None = None,
    option_puts: list | None = None,
) -> IndicatorResult:
    """
    Evaluate a single indicator by its registry key.
    Returns IndicatorResult(fires=False) for unknown keys.
    """
    b1 = bars_1m or []
    b15 = bars_15m or []

    match key:
        case "trend_15min":
            return eval_trend_15min(b15, direction)
        case "price_vs_vwap":
            return eval_price_vs_vwap(b1, direction)
        case "pullback_to_vwap":
            return eval_pullback_to_vwap(b1, direction)
        case "volume_spike":
            return eval_volume_spike(b1, direction)
        case "rsi":
            return eval_rsi(b1, direction)
        case "pcr":
            return eval_pcr(option_calls or [], option_puts or [], direction)
        case "news_based_vwap":
            return eval_news_based_vwap(direction)
        case _:
            logger.warning("Unknown indicator key: %s", key)
            return IndicatorResult(
                key=key, fires=False, direction=direction,
                value=None, reason="Unknown indicator"
            )


async def evaluate_all_active(
    db,
    direction: str,
    bars_1m: list[Bar],
    bars_15m: list[Bar],
    option_calls: list | None = None,
    option_puts: list | None = None,
) -> tuple[bool, list[IndicatorResult]]:
    """
    Evaluate all active indicators for a given direction.
    Returns (all_pass, results_list).

    Currently uses AND logic across all active indicators
    (group-level AND/OR is handled separately by the strategy engine).
    """
    from sqlalchemy import select
    from app.models import Indicator

    result = await db.execute(select(Indicator).where(Indicator.active == True))  # noqa: E712
    active_indicators = result.scalars().all()

    results: list[IndicatorResult] = []
    for ind in active_indicators:
        r = evaluate_indicator(
            key=ind.key,
            direction=direction,
            bars_1m=bars_1m,
            bars_15m=bars_15m,
            option_calls=option_calls,
            option_puts=option_puts,
        )
        results.append(r)

    all_pass = all(r.fires for r in results) if results else False
    return all_pass, results
