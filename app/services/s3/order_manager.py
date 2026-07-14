"""OrderManager — race-safe order-management state machine.

Moomoo's OpenAPI exposes no native bracket/OCO for US equities (verified at
startup by MoomooClient.capabilities), so exits are managed in software with
these invariants:

  * one state machine per local order; broker events are idempotent
    (deduped by deal_id, and by monotonic filled_qty per order)
  * outstanding SELL qty may never exceed live position qty (oversell guard)
  * at most one in-flight cancel/replace per order
  * duplicate / out-of-order pushes are ignored, not re-applied
  * on reconnect, `reconcile()` replays broker order/deal state into the
    local machines before any new order is accepted
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from app.services.s3.types import FillEvent, OrderUpdate

logger = logging.getLogger(__name__)


class Broker(Protocol):
    """Order-entry surface the OMS needs (implemented by MoomooClient
    and by ReplayEngine's SimBroker)."""

    def place_limit(self, symbol: str, side: str, qty: int, price: float) -> str | None: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def modify_order_supported(self) -> bool: ...


class OrderState(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED,
                        OrderState.REJECTED, OrderState.FAILED)


# Broker status → local state (unknown statuses keep the current state).
_STATUS_MAP = {
    "SUBMITTED": OrderState.SUBMITTED,
    "SUBMITTING": OrderState.SUBMITTED,
    "WAITING_SUBMIT": OrderState.SUBMITTED,
    "FILLED_PART": OrderState.PARTIAL,
    "FILLED_ALL": OrderState.FILLED,
    "CANCELLED_PART": OrderState.CANCELLED,
    "CANCELLED_ALL": OrderState.CANCELLED,
    "FAILED": OrderState.FAILED,
    "DISABLED": OrderState.FAILED,
    "DELETED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
}


@dataclass
class ManagedOrder:
    local_id: str
    symbol: str
    side: str                  # "BUY" | "SELL"
    qty: int
    limit_price: float
    purpose: str               # "ENTRY1" | "ENTRY2" | "TP1" | "TP2" | "STOP" | "FLATTEN"
    created_ts: float = field(default_factory=time.time)
    broker_id: str | None = None
    state: OrderState = OrderState.PENDING_SUBMIT
    filled_qty: int = 0
    filled_avg: float = 0.0
    timeout_sec: float | None = None
    cancel_requested: bool = False
    seen_deals: set[str] = field(default_factory=set)

    @property
    def open_qty(self) -> int:
        return max(self.qty - self.filled_qty, 0)


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        on_fill: Callable[[ManagedOrder, FillEvent], None],
        on_terminal: Callable[[ManagedOrder], None],
    ) -> None:
        self._broker = broker
        self._on_fill = on_fill
        self._on_terminal = on_terminal
        self._orders: dict[str, ManagedOrder] = {}       # local_id → order
        self._by_broker_id: dict[str, str] = {}          # broker_id → local_id

    # ── Queries ──────────────────────────────────────────────────
    def order(self, local_id: str) -> ManagedOrder | None:
        return self._orders.get(local_id)

    def open_sell_qty(self, symbol: str) -> int:
        return sum(
            o.open_qty for o in self._orders.values()
            if o.symbol == symbol and o.side == "SELL" and not o.state.terminal
        )

    def live_orders(self, symbol: str | None = None) -> list[ManagedOrder]:
        return [
            o for o in self._orders.values()
            if not o.state.terminal and (symbol is None or o.symbol == symbol)
        ]

    # ── Submission ───────────────────────────────────────────────
    def submit(
        self,
        symbol: str,
        side: str,
        qty: int,
        limit_price: float,
        purpose: str,
        *,
        position_qty: int = 0,
        timeout_sec: float | None = None,
    ) -> ManagedOrder | None:
        if qty <= 0:
            return None
        # Oversell guard: total outstanding sells (incl. this one) ≤ position.
        if side == "SELL" and self.open_sell_qty(symbol) + qty > position_qty:
            logger.error(
                "[S3][OMS] OVERSELL BLOCKED %s: want %d, outstanding %d, position %d",
                symbol, qty, self.open_sell_qty(symbol), position_qty,
            )
            return None

        order = ManagedOrder(
            local_id=uuid.uuid4().hex[:12], symbol=symbol, side=side,
            qty=qty, limit_price=limit_price, purpose=purpose,
            timeout_sec=timeout_sec,
        )
        self._orders[order.local_id] = order
        broker_id = self._broker.place_limit(symbol, side, qty, limit_price)
        if broker_id is None:
            order.state = OrderState.FAILED
            logger.error("[S3][OMS] submit FAILED %s %s %d @ %.2f (%s)",
                         side, symbol, qty, limit_price, purpose)
            self._on_terminal(order)
            return order
        order.broker_id = str(broker_id)
        order.state = OrderState.SUBMITTED
        self._by_broker_id[order.broker_id] = order.local_id
        logger.info("[S3][OMS] %s %s %s %d @ %.2f → broker #%s",
                    purpose, side, symbol, qty, limit_price, broker_id)
        return order

    # ── Cancel (at most one in-flight per order) ─────────────────
    def cancel(self, local_id: str) -> None:
        order = self._orders.get(local_id)
        if order is None or order.state.terminal or order.cancel_requested:
            return
        order.cancel_requested = True
        order.state = OrderState.CANCELLING
        if order.broker_id and not self._broker.cancel_order(order.broker_id):
            # Cancel rejected — usually already filled; the pending push
            # will resolve the true state.
            order.cancel_requested = False
            logger.warning("[S3][OMS] cancel rejected for %s (%s)",
                           order.broker_id, order.purpose)

    # ── Timeouts ─────────────────────────────────────────────────
    def sweep_timeouts(self, now_ts: float) -> None:
        for order in list(self._orders.values()):
            if (
                not order.state.terminal
                and order.timeout_sec is not None
                and not order.cancel_requested
                and now_ts - order.created_ts > order.timeout_sec
            ):
                logger.info("[S3][OMS] timeout → cancel %s %s (%s)",
                            order.symbol, order.broker_id, order.purpose)
                self.cancel(order.local_id)

    # ── Broker pushes (idempotent) ───────────────────────────────
    def on_order_update(self, upd: OrderUpdate) -> None:
        local_id = self._by_broker_id.get(str(upd.order_id))
        if local_id is None:
            logger.warning("[S3][OMS] update for unknown order %s (external?)",
                           upd.order_id)
            return
        order = self._orders[local_id]
        if order.state.terminal:
            return  # duplicate / late push
        new_state = _STATUS_MAP.get(upd.status.upper())

        # Monotonic fill quantity — regressions are stale pushes.
        if upd.filled_qty > order.filled_qty:
            order.filled_qty = min(upd.filled_qty, order.qty)
            if upd.filled_avg_price > 0:
                order.filled_avg = upd.filled_avg_price
        if new_state is not None:
            # A CANCELLED push on a fully filled order is stale ordering.
            if new_state == OrderState.CANCELLED and order.filled_qty >= order.qty:
                new_state = OrderState.FILLED
            order.state = new_state
        if order.state.terminal:
            self._on_terminal(order)

    def on_order_snapshot(self, upd: OrderUpdate) -> None:
        """Poll-based brokers (Tradier): one snapshot carries both fill
        progress and status.  Synthesizes an idempotent incremental
        FillEvent from the monotonic filled_qty delta, then applies the
        status transition."""
        local_id = self._by_broker_id.get(str(upd.order_id))
        if local_id is None:
            return
        order = self._orders[local_id]
        if order.state.terminal:
            return
        if upd.filled_qty > order.filled_qty:
            delta = upd.filled_qty - order.filled_qty
            prev_qty, prev_avg = order.filled_qty, order.filled_avg
            # Incremental price from the average change; falls back to the
            # snapshot average, then the limit price.
            if upd.filled_avg_price > 0 and delta > 0:
                inc = (upd.filled_avg_price * upd.filled_qty - prev_avg * prev_qty) / delta
                price = inc if inc > 0 else upd.filled_avg_price
            else:
                price = upd.filled_avg_price or order.limit_price
            self.on_fill(FillEvent(
                order_id=str(upd.order_id),
                deal_id=f"POLL-{upd.order_id}-{upd.filled_qty}",
                symbol=upd.symbol or order.symbol,
                ts=upd.ts, side=order.side, price=round(price, 4), qty=delta,
            ))
        if not order.state.terminal:
            self.on_order_update(upd)

    def on_fill(self, fill: FillEvent) -> None:
        local_id = self._by_broker_id.get(str(fill.order_id))
        if local_id is None:
            logger.warning("[S3][OMS] fill for unknown order %s", fill.order_id)
            return
        order = self._orders[local_id]
        if fill.deal_id in order.seen_deals:
            return  # duplicate deal push
        order.seen_deals.add(fill.deal_id)
        prev = order.filled_qty
        add = min(fill.qty, order.qty - prev)
        if add <= 0:
            return
        order.filled_avg = (
            (order.filled_avg * prev + fill.price * add) / (prev + add)
            if prev else fill.price
        )
        order.filled_qty = prev + add
        if order.filled_qty >= order.qty and not order.state.terminal:
            order.state = OrderState.FILLED
        elif not order.state.terminal:
            order.state = OrderState.PARTIAL
        self._on_fill(order, fill)
        if order.state.terminal:
            self._on_terminal(order)

    # ── Reconnect reconciliation ─────────────────────────────────
    def reconcile(
        self,
        broker_orders: list[OrderUpdate],
        broker_fills: list[FillEvent],
        *,
        snapshots: bool = False,
    ) -> None:
        """Replay broker truth into local machines after a reconnect.
        `snapshots=True` for poll-based brokers whose order rows carry fill
        progress (fills are synthesized from deltas)."""
        for upd in broker_orders:
            if snapshots:
                self.on_order_snapshot(upd)
            else:
                self.on_order_update(upd)
        for f in broker_fills:
            self.on_fill(f)
        # Any local non-terminal order the broker no longer reports is dead.
        broker_ids = {str(u.order_id) for u in broker_orders}
        for order in self._orders.values():
            if (
                not order.state.terminal
                and order.broker_id is not None
                and order.broker_id not in broker_ids
            ):
                logger.warning("[S3][OMS] reconcile: broker lost order %s → FAILED",
                               order.broker_id)
                order.state = OrderState.FAILED
                self._on_terminal(order)
