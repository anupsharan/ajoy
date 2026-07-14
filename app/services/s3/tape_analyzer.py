"""TapeAnalyzer — trade-aggressor inference and rolling order-flow metrics.

Aggressor side comes from the trade price relative to the prevailing quote
held immediately BEFORE the print (never from display colors or the SDK's
direction field):

    price >= prevailing ask  → aggressive BUY
    price <= prevailing bid  → aggressive SELL
    inside the spread        → NEUTRAL (leans excluded from signed volume)
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.services.s3.types import Aggressor, BookSnapshot, Tick

logger = logging.getLogger(__name__)

_EPS = 1e-9


@dataclass
class FlowMetrics:
    buy_rate_short: float = 0.0     # aggressive-buy shares/sec, short window
    buy_rate_long: float = 0.0      # …long window
    imbalance: float = 0.0          # (buys − sells) / (buys + sells), short window
    accelerating: bool = False
    last_trade_ts: float = 0.0


@dataclass
class _SymbolTape:
    prevailing_bid: float = 0.0
    prevailing_ask: float = 0.0
    quote_ts: float = 0.0
    trades: deque[Tick] = field(default_factory=lambda: deque(maxlen=5000))
    # (price, ts, volume) of recent aggressive buys — consumed by the
    # OrderBookAnalyzer to match wall reductions against real prints.
    aggr_buys: deque[tuple[float, float, int]] = field(
        default_factory=lambda: deque(maxlen=2000)
    )


class TapeAnalyzer:
    def __init__(
        self,
        short_window_sec: float,
        long_window_sec: float,
        accel_mult: float,
        min_imbalance: float,
    ) -> None:
        self._short = short_window_sec
        self._long = long_window_sec
        self._accel_mult = accel_mult
        self._min_imbalance = min_imbalance
        self._state: dict[str, _SymbolTape] = defaultdict(_SymbolTape)

    # ── Ingest ───────────────────────────────────────────────────
    def on_book(self, book: BookSnapshot) -> None:
        st = self._state[book.symbol]
        if book.best_bid and book.best_ask:
            st.prevailing_bid = book.best_bid.price
            st.prevailing_ask = book.best_ask.price
            st.quote_ts = book.recv_ts

    def on_tick(self, tick: Tick) -> Tick:
        st = self._state[tick.symbol]
        if st.prevailing_ask > 0 and tick.price >= st.prevailing_ask - _EPS:
            tick.aggressor = Aggressor.BUY
            st.aggr_buys.append((tick.price, tick.recv_ts, tick.volume))
        elif st.prevailing_bid > 0 and tick.price <= st.prevailing_bid + _EPS:
            tick.aggressor = Aggressor.SELL
        else:
            tick.aggressor = Aggressor.NEUTRAL
        st.trades.append(tick)
        return tick

    # ── Wall-consumption matching ────────────────────────────────
    def matched_buy_volume(
        self, symbol: str, price: float, since_ts: float, window_s: float
    ) -> int:
        """Aggressive-buy shares printed AT (or through) `price` within
        [since_ts − window, now].  Used to split wall reductions into
        consumed vs withdrawn."""
        st = self._state[symbol]
        lo = since_ts - window_s
        return sum(
            v for p, ts, v in st.aggr_buys
            if ts >= lo and p >= price - _EPS
        )

    # ── Rolling metrics ──────────────────────────────────────────
    def metrics(self, symbol: str, now_ts: float) -> FlowMetrics:
        st = self._state[symbol]
        m = FlowMetrics()
        buys_s = sells_s = buys_l = 0
        for t in reversed(st.trades):
            age = now_ts - t.recv_ts
            if age > self._long:
                break
            if t.aggressor == Aggressor.BUY:
                buys_l += t.volume
                if age <= self._short:
                    buys_s += t.volume
            elif t.aggressor == Aggressor.SELL and age <= self._short:
                sells_s += t.volume
        m.buy_rate_short = buys_s / max(self._short, _EPS)
        m.buy_rate_long = buys_l / max(self._long, _EPS)
        total = buys_s + sells_s
        m.imbalance = (buys_s - sells_s) / total if total else 0.0
        m.accelerating = (
            m.buy_rate_short > self._accel_mult * m.buy_rate_long
            and m.imbalance >= self._min_imbalance
            and buys_s > 0
        )
        m.last_trade_ts = st.trades[-1].recv_ts if st.trades else 0.0
        return m

    def flow_supportive(self, symbol: str, now_ts: float) -> bool:
        return self.metrics(symbol, now_ts).accelerating
