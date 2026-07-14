"""BarAggregator — 1-min / 5-min bar state, EMA, session VWAP, HOD.

Bars arrive from Moomoo K-line pushes (authoritative); ticks are used only
to keep the forming bar fresh between pushes.  Session VWAP is computed
cumulatively from 1-min turnover/volume because the SDK exposes no session
VWAP field.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.services.s3.types import Bar, Tick

logger = logging.getLogger(__name__)

_MAX_BARS = 500


@dataclass
class _SymbolBars:
    one_min: deque[Bar] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    five_min: deque[Bar] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    cum_turnover: float = 0.0
    cum_volume: int = 0
    session_high: float = 0.0
    accumulated_1m: set[float] = field(default_factory=set)  # start_ts already in VWAP


class BarAggregator:
    def __init__(self, ema_period: int) -> None:
        self._ema_period = ema_period
        self._state: dict[str, _SymbolBars] = defaultdict(_SymbolBars)

    def reset_session(self) -> None:
        self._state.clear()

    # ── Ingest ───────────────────────────────────────────────────
    def on_bar(self, bar: Bar) -> Bar | None:
        """Store a pushed bar.  Returns the bar iff it just COMPLETED."""
        st = self._state[bar.symbol]
        series = st.one_min if bar.interval == "1m" else st.five_min
        completed: Bar | None = None

        if series and series[-1].start_ts == bar.start_ts:
            # Same bar updated in place; completion is detected when a NEWER
            # bar arrives, so simply replace.
            series[-1] = bar
        else:
            if series:
                prev = series[-1]
                if not prev.complete:
                    prev.complete = True
                    completed = prev
                if prev.interval == "1m" and prev.start_ts not in st.accumulated_1m:
                    st.accumulated_1m.add(prev.start_ts)
                    st.cum_turnover += prev.turnover or prev.close * prev.volume
                    st.cum_volume += prev.volume
                if prev.interval == "5m":
                    st.session_high = max(st.session_high, prev.high)
            series.append(bar)

        if bar.interval == "5m":
            st.session_high = max(st.session_high, bar.high)
        return completed

    def on_tick(self, tick: Tick) -> None:
        st = self._state[tick.symbol]
        for series in (st.one_min, st.five_min):
            if series and not series[-1].complete:
                b = series[-1]
                b.close = tick.price
                b.high = max(b.high, tick.price)
                b.low = min(b.low, tick.price)

    # ── Derived values ───────────────────────────────────────────
    def ema(self, symbol: str, interval: str = "1m", period: int | None = None) -> float | None:
        """EMA over COMPLETED closes (classic seed = SMA of first `period`)."""
        period = period or self._ema_period
        st = self._state[symbol]
        series = st.one_min if interval == "1m" else st.five_min
        closes = [b.close for b in series if b.complete]
        if len(closes) < period:
            return None
        k = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        for c in closes[period:]:
            ema = c * k + ema * (1 - k)
        return ema

    def vwap(self, symbol: str) -> float | None:
        st = self._state[symbol]
        if st.cum_volume <= 0:
            return None
        return st.cum_turnover / st.cum_volume

    def high_of_day(self, symbol: str) -> float | None:
        st = self._state[symbol]
        return st.session_high or None

    def last_completed(self, symbol: str, interval: str = "1m") -> Bar | None:
        st = self._state[symbol]
        series = st.one_min if interval == "1m" else st.five_min
        for b in reversed(series):
            if b.complete:
                return b
        return None

    def recent_volume(self, symbol: str, minutes: int = 1) -> int:
        """Trailing completed 1-min volume — liquidity-participation cap."""
        st = self._state[symbol]
        done = [b for b in st.one_min if b.complete]
        return sum(b.volume for b in done[-minutes:]) if done else 0

    def micro_swing_low(self, symbol: str, lookback: int = 3) -> float | None:
        st = self._state[symbol]
        done = [b for b in st.one_min if b.complete]
        if not done:
            return None
        return min(b.low for b in done[-lookback:])

    def short_horizon_sigma(self, symbol: str, lookback: int = 5) -> float | None:
        """Per-bar close-to-close sigma over the last `lookback` 1-min bars."""
        st = self._state[symbol]
        closes = [b.close for b in st.one_min if b.complete][-(lookback + 1):]
        if len(closes) < 3:
            return None
        rets = [b - a for a, b in zip(closes, closes[1:])]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        return var ** 0.5

    def trend_ok(self, symbol: str) -> bool:
        """5-min context: price above VWAP and 5-min EMA9 rising or flat."""
        last5 = self.last_completed(symbol, "5m")
        vwap = self.vwap(symbol)
        if last5 is None or vwap is None:
            return False
        if last5.close < vwap:
            return False
        ema5 = self.ema(symbol, "5m", self._ema_period)
        return ema5 is None or last5.close >= ema5
