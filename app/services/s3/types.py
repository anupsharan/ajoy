"""Shared event types for the S3 pipeline.

Every SDK callback is converted into one of these plain dataclasses at the
adapter boundary so that the rest of the pipeline never touches Moomoo types
(and the ReplayEngine can synthesize them from JSONL).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Aggressor(str, Enum):
    BUY = "BUY"        # trade printed at/above the prevailing ask
    SELL = "SELL"      # trade printed at/below the prevailing bid
    NEUTRAL = "NEUTRAL"  # inside the spread / quote unknown


class BookIssue(str, Enum):
    OK = "OK"
    STALE = "STALE"
    DUPLICATE = "DUPLICATE"
    OUT_OF_SEQUENCE = "OUT_OF_SEQUENCE"
    CROSSED = "CROSSED"
    LOCKED = "LOCKED"
    EMPTY = "EMPTY"


class EngineState(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DISCONNECTED = "DISCONNECTED"
    RECONCILING = "RECONCILING"
    HALTED = "HALTED"          # daily loss / consecutive losses / kill switch
    STOPPED = "STOPPED"


@dataclass(slots=True)
class Tick:
    """One trade print."""
    symbol: str                # bare ticker, e.g. "AAPL"
    ts: float                  # exchange/SDK timestamp (epoch seconds)
    recv_ts: float             # local receive timestamp
    price: float
    volume: int
    seq: int = 0               # SDK sequence when available
    aggressor: Aggressor = Aggressor.NEUTRAL  # filled in by TapeAnalyzer


@dataclass(slots=True)
class BookLevel:
    price: float
    size: int
    orders: int = 0


@dataclass(slots=True)
class BookSnapshot:
    """Ten-level order book snapshot."""
    symbol: str
    ts: float
    recv_ts: float
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    seq: int = 0
    issue: BookIssue = BookIssue.OK

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.asks[0].price + self.bids[0].price) / 2.0
        return None


@dataclass(slots=True)
class Bar:
    """One completed (or forming) K-line bar."""
    symbol: str
    interval: str              # "1m" | "5m"
    start_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float = 0.0
    complete: bool = False


@dataclass(slots=True)
class OrderUpdate:
    """Normalized order-status push from the broker."""
    order_id: str
    symbol: str
    ts: float
    status: str                # broker-side status string, normalized upper-case
    side: str                  # "BUY" | "SELL"
    price: float
    qty: int
    filled_qty: int
    filled_avg_price: float


@dataclass(slots=True)
class FillEvent:
    """Normalized execution/deal push."""
    order_id: str
    deal_id: str
    symbol: str
    ts: float
    side: str
    price: float
    qty: int


@dataclass(slots=True)
class Event:
    """Envelope placed on the engine queue."""
    kind: str                  # "tick" | "book" | "bar" | "order" | "fill" | "control"
    payload: object
    recv_ts: float = field(default_factory=time.time)


def now() -> float:
    return time.time()
