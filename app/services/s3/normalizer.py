"""MarketDataNormalizer — validate, dedupe and sequence raw events.

All raw ticks / book snapshots pass through here before any analytics.
Detects: stale, duplicate, out-of-sequence, crossed, locked and empty data.
Rejected events are counted (never silently dropped) so data-quality issues
surface in the status endpoint and the log.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from app.services.s3.types import BookIssue, BookSnapshot, Tick, now

logger = logging.getLogger(__name__)


@dataclass
class _SymbolState:
    last_book_seq: int = 0
    last_book_ts: float = 0.0
    last_book_fingerprint: tuple | None = None
    last_tick_key: tuple | None = None
    last_tick_ts: float = 0.0


@dataclass
class QualityCounters:
    accepted_ticks: int = 0
    accepted_books: int = 0
    rejected: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def reject(self, reason: str) -> None:
        self.rejected[reason] += 1


class MarketDataNormalizer:
    """Stateless-ish gatekeeper; one instance per engine."""

    def __init__(self, stale_max_ms: int) -> None:
        self._stale_max_s = stale_max_ms / 1000.0
        self._state: dict[str, _SymbolState] = defaultdict(_SymbolState)
        self.quality = QualityCounters()

    # ── Ticks ────────────────────────────────────────────────────
    def normalize_tick(self, tick: Tick) -> Tick | None:
        st = self._state[tick.symbol]
        if tick.price <= 0 or tick.volume <= 0:
            self.quality.reject("tick_invalid")
            return None
        key = (tick.seq, tick.ts, tick.price, tick.volume)
        if tick.seq and st.last_tick_key and key == st.last_tick_key:
            self.quality.reject("tick_duplicate")
            return None
        # Out-of-order ticks are kept (prints can legitimately arrive with
        # equal timestamps) but grossly stale ones are rejected.
        if tick.recv_ts - tick.ts > max(self._stale_max_s * 4, 5.0) and tick.ts > 0:
            self.quality.reject("tick_stale")
            return None
        st.last_tick_key = key
        st.last_tick_ts = tick.ts
        self.quality.accepted_ticks += 1
        return tick

    # ── Order book ───────────────────────────────────────────────
    def normalize_book(self, book: BookSnapshot) -> BookSnapshot | None:
        st = self._state[book.symbol]

        if not book.bids and not book.asks:
            book.issue = BookIssue.EMPTY
            self.quality.reject("book_empty")
            return None

        if book.seq and book.seq < st.last_book_seq:
            book.issue = BookIssue.OUT_OF_SEQUENCE
            self.quality.reject("book_out_of_sequence")
            return None

        fingerprint = (
            tuple((l.price, l.size) for l in book.bids[:3]),
            tuple((l.price, l.size) for l in book.asks[:3]),
        )
        if book.seq and book.seq == st.last_book_seq and fingerprint == st.last_book_fingerprint:
            book.issue = BookIssue.DUPLICATE
            self.quality.reject("book_duplicate")
            return None

        if now() - book.recv_ts > self._stale_max_s:
            book.issue = BookIssue.STALE
            self.quality.reject("book_stale")
            return None

        # Crossed / locked markets: keep out of analytics, they poison the
        # baseline and the aggressor inference.
        if book.bids and book.asks:
            bb, ba = book.bids[0].price, book.asks[0].price
            if bb > ba:
                book.issue = BookIssue.CROSSED
                self.quality.reject("book_crossed")
                return None
            if bb == ba:
                book.issue = BookIssue.LOCKED
                self.quality.reject("book_locked")
                return None

        # Monotonic level sanity (bids strictly descending, asks ascending).
        for levels, sign in ((book.bids, -1), (book.asks, 1)):
            for a, b in zip(levels, levels[1:]):
                if (b.price - a.price) * sign <= 0:
                    self.quality.reject("book_malformed")
                    return None

        st.last_book_seq = book.seq
        st.last_book_ts = book.ts
        st.last_book_fingerprint = fingerprint
        self.quality.accepted_books += 1
        book.issue = BookIssue.OK
        return book

    # ── Staleness probe (used by RiskManager pre-trade) ──────────
    def data_age_ms(self, symbol: str) -> float:
        st = self._state[symbol]
        newest = max(st.last_book_ts, st.last_tick_ts)
        if newest == 0.0:
            return float("inf")
        return (now() - newest) * 1000.0
