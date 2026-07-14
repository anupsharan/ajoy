"""StrategyEngine — turns a wall-consumption breakout into an entry plan.

Entry requires ALL of (evaluated on the event that produced the signal):
  1. BreakoutSignal from OrderBookAnalyzer (wall consumed, ask advanced,
     print confirmation above the former wall)
  2. Order flow accelerating: short-window aggressive-buy rate above
     `accel_mult ×` the long-window rate AND signed-volume imbalance
     above the floor (TapeAnalyzer)
  3. 5-min context OK: price above session VWAP, 5-min EMA not against us
  4. Spread acceptable, data fresh, all RiskManager gates green

The stop derives from structural invalidation: min(former wall price,
recent 1-min micro-swing low) minus a pad of (spread + pad_ticks·tick +
vol_mult·short-horizon sigma), snapped to tick.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from app.config import settings
from app.services.s3.bars import BarAggregator
from app.services.s3.book_analyzer import BreakoutSignal, tick_size
from app.services.s3.tape_analyzer import TapeAnalyzer
from app.services.s3.types import BookSnapshot

logger = logging.getLogger(__name__)


@dataclass
class EntryPlan:
    symbol: str
    limit_price: float        # marketable limit: ask + slippage cap
    stop_price: float
    wall_price: float
    signal_ts: float


class StrategyEngine:
    def __init__(self, tape: TapeAnalyzer, bars: BarAggregator) -> None:
        self._tape = tape
        self._bars = bars

    def evaluate(self, signal: BreakoutSignal, book: BookSnapshot) -> EntryPlan | None:
        sym = signal.symbol
        ts = signal.ts

        # Gate 2 — order flow must be accelerating.
        m = self._tape.metrics(sym, ts)
        if not m.accelerating:
            logger.info(
                "[S3][SKIP] %s breakout without flow acceleration "
                "(short=%.0f/s long=%.0f/s imb=%.2f)",
                sym, m.buy_rate_short, m.buy_rate_long, m.imbalance,
            )
            return None

        # Gate 3 — 5-min trend / VWAP context.
        if not self._bars.trend_ok(sym):
            logger.info("[S3][SKIP] %s breakout against 5-min VWAP/EMA context", sym)
            return None

        best_ask = book.best_ask
        if best_ask is None:
            return None
        tick = tick_size(best_ask.price)

        # Marketable limit with a strict slippage cap — never an open-ended
        # market order, never a passive bid parked below the wall.
        limit_price = round(
            best_ask.price + settings.s3_entry_slippage_ticks * tick, 4
        )

        # Structural stop.
        swing_low = self._bars.micro_swing_low(sym, lookback=3)
        invalidation = signal.wall.price
        if swing_low is not None:
            invalidation = min(invalidation, swing_low) if swing_low < invalidation \
                else invalidation
            # A micro-swing low ABOVE the wall means the wall itself is the
            # structure being reclaimed — keep the wall as invalidation.
        sigma = self._bars.short_horizon_sigma(sym) or 0.0
        spread = book.spread or tick
        pad = (
            spread
            + settings.s3_stop_spread_pad_ticks * tick
            + settings.s3_stop_vol_mult * sigma
        )
        stop_price = math.floor((invalidation - pad) / tick) * tick

        return EntryPlan(
            symbol=sym,
            limit_price=limit_price,
            stop_price=round(stop_price, 4),
            wall_price=signal.wall.price,
            signal_ts=ts,
        )
