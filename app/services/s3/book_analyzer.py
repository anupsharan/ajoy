"""OrderBookAnalyzer — robust depth baseline + ask-wall lifecycle tracking.

Baseline
--------
Rolling robust statistics of displayed size keyed by (symbol, side,
level-band, time-of-day bucket).  Location/scale = median/MAD; when MAD
degenerates to 0 (discrete size ladders) the 75th percentile is used as the
threshold scale instead.  A level is a WALL candidate when its size exceeds
BOTH an absolute floor and `rel_mult ×` the robust baseline.

Wall lifecycle
--------------
Candidate → persistent (age + update-count) → resolved.  Every reduction in
the wall's displayed size is split into:

    consumed  — matched against aggressive-buy prints at/through the wall
                price within the correlation window (TapeAnalyzer)
    withdrawn — the unmatched remainder (liquidity withdrawal; no intent
                is ever attributed to the withdrawal)

    consumption_ratio = consumed  / initial wall size
    pull_ratio        = withdrawn / initial wall size

A breakout is only tradeable when the wall was genuinely consumed
(consumption_ratio high, pull_ratio low), the best ask advanced beyond the
former wall price, and a print confirmed above it.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum

from app.services.s3.tape_analyzer import TapeAnalyzer
from app.services.s3.types import BookSnapshot

logger = logging.getLogger(__name__)

_EPS = 1e-9


def tick_size(price: float) -> float:
    """US-equity minimum increment (Reg NMS): $0.01 at/above $1."""
    return 0.01 if price >= 1.0 else 0.0001


class WallState(str, Enum):
    CANDIDATE = "CANDIDATE"
    PERSISTENT = "PERSISTENT"
    CONSUMED = "CONSUMED"       # broke via genuine consumption
    WITHDRAWN = "WITHDRAWN"     # vanished mostly unmatched — not tradeable
    RELOADED = "RELOADED"       # refreshed after consumption (iceberg) — vetoed
    EXPIRED = "EXPIRED"


@dataclass
class Wall:
    symbol: str
    price: float
    initial_size: int
    current_size: int
    first_seen: float
    last_update: float
    level_at_detection: int
    updates: int = 1
    consumed: int = 0
    withdrawn: int = 0
    state: WallState = WallState.CANDIDATE

    @property
    def consumption_ratio(self) -> float:
        return self.consumed / max(self.initial_size, 1)

    @property
    def pull_ratio(self) -> float:
        return self.withdrawn / max(self.initial_size, 1)


@dataclass
class BreakoutSignal:
    symbol: str
    wall: Wall
    confirm_price: float
    ts: float


class _Baseline:
    """Rolling (ts, size) samples with cached robust stats."""

    __slots__ = ("samples", "_cache", "_cache_n")

    def __init__(self) -> None:
        self.samples: deque[tuple[float, int]] = deque(maxlen=4000)
        self._cache: tuple[float, float] | None = None
        self._cache_n = 0

    def add(self, ts: float, size: int, window_s: float) -> None:
        self.samples.append((ts, size))
        while self.samples and ts - self.samples[0][0] > window_s:
            self.samples.popleft()
        if abs(len(self.samples) - self._cache_n) > 20:
            self._cache = None  # invalidate lazily

    def threshold(self, rel_mult: float) -> float | None:
        """rel_mult × robust scale of the level size; None if too few samples."""
        n = len(self.samples)
        if n < 10:
            return None
        if self._cache is None:
            sizes = sorted(s for _, s in self.samples)
            med = statistics.median(sizes)
            mad = statistics.median(abs(s - med) for s in sizes)
            if mad < _EPS:  # discrete ladder — fall back to P75
                p75 = sizes[int(0.75 * (n - 1))]
                base = max(p75, med, 1.0)
            else:
                base = med + 1.4826 * mad  # MAD → sigma-equivalent
            self._cache = (med, base)
            self._cache_n = n
        return rel_mult * self._cache[1]

    def __len__(self) -> int:
        return len(self.samples)


class OrderBookAnalyzer:
    def __init__(
        self,
        tape: TapeAnalyzer,
        *,
        baseline_window_min: int,
        tod_bucket_min: int,
        min_samples: int,
        abs_min_shares: int,
        rel_mult: float,
        max_level: int,
        min_persist_sec: float,
        min_updates: int,
        match_window_ms: int,
        min_consumption_ratio: float,
        max_pull_ratio: float,
        confirm_ticks: int,
        require_ask_advance: bool,
        reload_veto_frac: float = 0.25,
        reload_cooldown_sec: float = 120.0,
    ) -> None:
        self._tape = tape
        self._window_s = baseline_window_min * 60.0
        self._tod_bucket_s = tod_bucket_min * 60.0
        self._min_samples = min_samples
        self._abs_min = abs_min_shares
        self._rel_mult = rel_mult
        self._max_level = max_level
        self._min_persist = min_persist_sec
        self._min_updates = min_updates
        self._match_window_s = match_window_ms / 1000.0
        self._min_consumption = min_consumption_ratio
        self._max_pull = max_pull_ratio
        self._confirm_ticks = confirm_ticks
        self._require_ask_advance = require_ask_advance
        self._reload_veto_frac = reload_veto_frac
        self._reload_cooldown_s = reload_cooldown_sec

        # (symbol, side, level_band, tod_bucket) → _Baseline
        self._baselines: dict[tuple, _Baseline] = defaultdict(_Baseline)
        self._walls: dict[str, Wall] = {}          # one active ask wall / symbol
        self._sample_count: dict[str, int] = defaultdict(int)
        self._last_print: dict[str, tuple[float, float]] = {}  # symbol → (price, ts)
        # symbol → (price, ts) of the last iceberg-reload veto: that price
        # level may not be re-detected as a wall until the cooldown expires.
        self._reload_block: dict[str, tuple[float, float]] = {}

    # ── Helpers ──────────────────────────────────────────────────
    @staticmethod
    def _band(level_idx: int) -> int:
        return 0 if level_idx < 3 else 1

    def _tod_bucket(self, ts: float) -> int:
        lt = time.localtime(ts)
        return int((lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) // self._tod_bucket_s)

    def note_print(self, symbol: str, price: float, ts: float) -> None:
        self._last_print[symbol] = (price, ts)

    def active_wall(self, symbol: str) -> Wall | None:
        return self._walls.get(symbol)

    # ── Main update ──────────────────────────────────────────────
    def on_book(self, book: BookSnapshot) -> BreakoutSignal | None:
        sym = book.symbol
        ts = book.recv_ts

        # 1. Feed baselines (both sides — the spec keys baselines by side).
        for side, levels in (("bid", book.bids), ("ask", book.asks)):
            for i, lvl in enumerate(levels):
                key = (sym, side, self._band(i), self._tod_bucket(ts))
                self._baselines[key].add(ts, lvl.size, self._window_s)
        self._sample_count[sym] += 1

        # 2. Update the tracked wall (if any) BEFORE detecting new ones.
        signal = self._update_wall(book)
        if signal is not None:
            return signal

        # 3. Detect a new candidate wall if none is active.
        if sym not in self._walls and self._sample_count[sym] >= self._min_samples:
            self._detect_wall(book)
        return None

    # ── Detection ────────────────────────────────────────────────
    def _detect_wall(self, book: BookSnapshot) -> None:
        sym, ts = book.symbol, book.recv_ts
        blocked = self._reload_block.get(sym)
        for i, lvl in enumerate(book.asks[: self._max_level]):
            if lvl.size < self._abs_min:
                continue
            # Iceberg-reload cooldown: this price level recently refreshed
            # after consumption — do not re-arm on it.
            if (
                blocked is not None
                and ts - blocked[1] < self._reload_cooldown_s
                and abs(lvl.price - blocked[0]) <= 1.5 * tick_size(lvl.price)
            ):
                continue
            key = (sym, "ask", self._band(i), self._tod_bucket(ts))
            thr = self._baselines[key].threshold(self._rel_mult)
            if thr is None or lvl.size < thr:
                continue
            self._walls[sym] = Wall(
                symbol=sym, price=lvl.price, initial_size=lvl.size,
                current_size=lvl.size, first_seen=ts, last_update=ts,
                level_at_detection=i,
            )
            logger.info(
                "[S3][WALL] %s candidate ask wall %.2f × %d (%.1f× baseline, L%d)",
                sym, lvl.price, lvl.size, lvl.size / max(thr / self._rel_mult, 1.0), i + 1,
            )
            return

    # ── Lifecycle ────────────────────────────────────────────────
    def _update_wall(self, book: BookSnapshot) -> BreakoutSignal | None:
        sym, ts = book.symbol, book.recv_ts
        wall = self._walls.get(sym)
        if wall is None:
            return None

        # Locate the wall price in the current ask stack.
        size_now = 0
        for lvl in book.asks:
            if abs(lvl.price - wall.price) < _EPS:
                size_now = lvl.size
                break

        reduction = wall.current_size - size_now
        if reduction > 0:
            matched = min(
                reduction,
                self._tape.matched_buy_volume(sym, wall.price, ts, self._match_window_s),
            )
            wall.consumed += matched
            wall.withdrawn += reduction - matched
        elif size_now > wall.current_size:
            # Liquidity ADDED back at the wall price.  A meaningful refresh
            # after meaningful consumption is iceberg/reload behaviour — a
            # seller with more behind the displayed size.  Veto the wall and
            # block this price level from re-detection for the cooldown.
            refresh = size_now - wall.current_size
            if (
                self._reload_veto_frac > 0
                and wall.consumed >= 0.10 * wall.initial_size
                and refresh >= self._reload_veto_frac * wall.initial_size
            ):
                wall.state = WallState.RELOADED
                self._reload_block[sym] = (wall.price, ts)
                logger.info(
                    "[S3][WALL] %s wall %.2f RELOADED +%d after %.0f%% consumed "
                    "— vetoed, level blocked %.0fs",
                    sym, wall.price, refresh, wall.consumption_ratio * 100,
                    self._reload_cooldown_s,
                )
                del self._walls[sym]
                return None
        wall.current_size = size_now
        wall.updates += 1
        wall.last_update = ts

        age = ts - wall.first_seen
        if wall.state == WallState.CANDIDATE:
            if age >= self._min_persist and wall.updates >= self._min_updates:
                wall.state = WallState.PERSISTENT
            elif size_now == 0:
                # Vanished before proving persistence — discard quietly.
                del self._walls[sym]
                return None

        if wall.state != WallState.PERSISTENT:
            return None

        # Withdrawal dominates → not tradeable; drop and log neutrally.
        if wall.pull_ratio > self._max_pull:
            wall.state = WallState.WITHDRAWN
            logger.info(
                "[S3][WALL] %s wall %.2f resolved by liquidity withdrawal "
                "(pull %.0f%%, consumed %.0f%%) — no trade",
                sym, wall.price, wall.pull_ratio * 100, wall.consumption_ratio * 100,
            )
            del self._walls[sym]
            return None

        # Breakout check: consumed + ask advanced + print confirmation.
        if wall.consumption_ratio < self._min_consumption:
            return None
        best_ask = book.best_ask
        if self._require_ask_advance and (
            best_ask is None or best_ask.price <= wall.price + _EPS
        ):
            return None
        last = self._last_print.get(sym)
        confirm_level = wall.price + self._confirm_ticks * tick_size(wall.price)
        if last is None or last[0] < confirm_level - _EPS or ts - last[1] > 3.0:
            return None

        wall.state = WallState.CONSUMED
        logger.info(
            "[S3][BREAKOUT] %s wall %.2f consumed %.0f%% / pulled %.0f%% — "
            "ask %.2f, confirm print %.2f",
            sym, wall.price, wall.consumption_ratio * 100, wall.pull_ratio * 100,
            best_ask.price if best_ask else 0.0, last[0],
        )
        del self._walls[sym]
        return BreakoutSignal(symbol=sym, wall=wall, confirm_price=last[0], ts=ts)
