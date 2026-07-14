"""PositionManager — tranche entries, R-based scale-outs, hard-stop OCO.

Lifecycle
---------
ENTRY1 (~50% of risk-approved size, marketable limit) → optional ENTRY2 iff
the post-entry micro-high breaks within the scale window, flow stays
supportive and worst-case risk stays inside the ORIGINAL limit → R-based
management:

    R  = VWAP entry − initial stop
    ⅓ off at +1R  → after the fill CONFIRMS, stop → breakeven + costs
    ⅓ off at +2R
    runner exits on a completed 1-min close below the EMA(9)

The hard stop is software-monitored on every tick/quote, protects every
partial fill immediately, and only ever ratchets UP.  Because Moomoo has no
native OCO (see MoomooClient.capabilities), stop-fire cancels the resting
target sells and market-ably sells freed quantity as each cancel confirms —
the OMS oversell guard makes the race safe.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app.config import settings
from app.services.s3.bars import BarAggregator
from app.services.s3.book_analyzer import tick_size
from app.services.s3.order_manager import ManagedOrder, OrderManager, OrderState
from app.services.s3.strategy_engine import EntryPlan
from app.services.s3.tape_analyzer import TapeAnalyzer
from app.services.s3.types import BookSnapshot, FillEvent, Tick

logger = logging.getLogger(__name__)


class PosState(str, Enum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    STOPPING = "STOPPING"
    FLATTENING = "FLATTENING"
    CLOSED = "CLOSED"


@dataclass
class Position:
    symbol: str
    plan: EntryPlan
    full_shares: int                 # risk-approved total
    tranche1_shares: int
    state: PosState = PosState.PENDING_ENTRY
    opened_ts: float = field(default_factory=time.time)

    qty: int = 0                     # live shares held
    entry_vwap: float = 0.0          # volume-weighted entry across all fills
    entry_cost: float = 0.0
    stop_price: float = 0.0
    r_value: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_qty: int = 0
    tp2_qty: int = 0
    runner_qty: int = 0
    tp1_filled: bool = False
    tp2_filled: bool = False
    breakeven_set: bool = False
    scale_done: bool = False
    micro_high: float = 0.0          # post-entry high, for the add-on trigger
    last_new_high_ts: float = 0.0    # when micro_high last advanced (stagnation clock)
    entry1_fill_ts: float = 0.0
    realized_pnl: float = 0.0
    sold_qty: int = 0
    exit_reason: str | None = None

    entry_order_ids: list[str] = field(default_factory=list)
    tp1_order_id: str | None = None
    tp2_order_id: str | None = None

    @property
    def closed(self) -> bool:
        return self.state == PosState.CLOSED

    @property
    def notional(self) -> float:
        return self.qty * self.entry_vwap


class PositionManager:
    def __init__(
        self,
        oms: OrderManager,
        tape: TapeAnalyzer,
        bars: BarAggregator,
        on_position_opened: Callable[[Position], None],
        on_position_closed: Callable[[Position], None],
    ) -> None:
        self._oms = oms
        self._tape = tape
        self._bars = bars
        self._on_opened = on_position_opened
        self._on_closed = on_position_closed
        self.positions: dict[str, Position] = {}
        self._last_book: dict[str, BookSnapshot] = {}

    # ── Queries ──────────────────────────────────────────────────
    def open_count(self) -> int:
        return sum(1 for p in self.positions.values() if not p.closed)

    def total_notional(self) -> float:
        return sum(p.notional for p in self.positions.values() if not p.closed)

    def has_position(self, symbol: str) -> bool:
        p = self.positions.get(symbol)
        return p is not None and not p.closed

    # ── Entry ────────────────────────────────────────────────────
    def open_position(self, plan: EntryPlan, full_shares: int) -> Position | None:
        if self.has_position(plan.symbol):
            return None
        tranche1 = max(int(round(full_shares * settings.s3_initial_tranche_pct)), 1)
        pos = Position(
            symbol=plan.symbol, plan=plan, full_shares=full_shares,
            tranche1_shares=tranche1, stop_price=plan.stop_price,
        )
        self.positions[plan.symbol] = pos
        order = self._oms.submit(
            plan.symbol, "BUY", tranche1, plan.limit_price, "ENTRY1",
            timeout_sec=settings.s3_entry_timeout_sec,
        )
        if order is None or order.state == OrderState.FAILED:
            pos.state = PosState.CLOSED
            pos.exit_reason = "ENTRY_FAILED"
            return None
        pos.entry_order_ids.append(order.local_id)
        return pos

    # ── Fill / terminal callbacks (wired by the engine) ──────────
    def on_fill(self, order: ManagedOrder, fill: FillEvent) -> None:
        pos = self.positions.get(order.symbol)
        if pos is None or pos.closed:
            return
        if order.side == "BUY":
            prev_qty = pos.qty
            pos.qty += fill.qty
            pos.entry_cost += fill.qty * fill.price
            pos.entry_vwap = pos.entry_cost / max(pos.qty, 1)
            if prev_qty == 0:
                pos.state = PosState.OPEN
                pos.entry1_fill_ts = time.time()
                pos.micro_high = fill.price
                pos.last_new_high_ts = pos.entry1_fill_ts
                # Every partial fill is protected immediately: the software
                # stop below covers pos.qty from this moment on.
                logger.info("[S3][POS] %s OPEN %d @ %.2f stop %.2f",
                            pos.symbol, pos.qty, pos.entry_vwap, pos.stop_price)
                self._on_opened(pos)
            self._recompute_levels(pos)
        else:
            pos.qty = max(pos.qty - fill.qty, 0)
            pos.sold_qty += fill.qty
            pos.realized_pnl += (fill.price - pos.entry_vwap) * fill.qty
            if order.local_id == pos.tp1_order_id and order.state == OrderState.FILLED:
                pos.tp1_filled = True
                self._move_stop_to_breakeven(pos)
            if order.local_id == pos.tp2_order_id and order.state == OrderState.FILLED:
                pos.tp2_filled = True
            if pos.qty <= 0:
                self._finalize(pos)

    def on_order_terminal(self, order: ManagedOrder) -> None:
        pos = self.positions.get(order.symbol)
        if pos is None or pos.closed:
            return

        if order.purpose in ("ENTRY1", "ENTRY2"):
            if order.state != OrderState.FILLED and order.filled_qty == 0 \
                    and pos.qty == 0 and order.purpose == "ENTRY1":
                pos.state = PosState.CLOSED
                pos.exit_reason = "ENTRY_UNFILLED"
                logger.info("[S3][POS] %s entry unfilled — no position", pos.symbol)
            if order.purpose == "ENTRY2":
                pos.scale_done = True
            self._recompute_levels(pos)
            self._place_targets(pos)
            return

        if order.purpose in ("STOP", "FLATTEN") and order.state != OrderState.FILLED:
            # Urgent sell cancelled/expired with shares still open →
            # resubmit one tick deeper (never give up on an exit).
            remaining = pos.qty - self._oms.open_sell_qty(pos.symbol)
            if remaining > 0 and pos.state in (PosState.STOPPING, PosState.FLATTENING):
                self._urgent_sell(pos, remaining, order.purpose, deeper=True)
            return

        if order.purpose in ("TP1", "TP2") and order.state in (
            OrderState.CANCELLED, OrderState.FAILED, OrderState.REJECTED
        ) and pos.state in (PosState.STOPPING, PosState.FLATTENING):
            # A target cancel just confirmed during stop/flatten — sell the
            # freed quantity urgently.
            freed = pos.qty - self._oms.open_sell_qty(pos.symbol)
            if freed > 0:
                self._urgent_sell(pos, freed, "STOP" if pos.state == PosState.STOPPING else "FLATTEN")

    # ── Market-data driven management ────────────────────────────
    def on_book(self, book: BookSnapshot) -> None:
        self._last_book[book.symbol] = book
        pos = self.positions.get(book.symbol)
        if pos is None or pos.closed or pos.state != PosState.OPEN:
            return
        bid = book.best_bid.price if book.best_bid else None
        if bid is not None and pos.qty > 0 and bid <= pos.stop_price:
            self._trigger_stop(pos)

    def on_tick(self, tick: Tick) -> None:
        pos = self.positions.get(tick.symbol)
        if pos is None or pos.closed:
            return
        if pos.state == PosState.OPEN:
            # Capture the PRE-update high: the scale-in trigger is "price
            # broke the previous micro-high", so the comparison must run
            # against the high as it stood before this tick.
            prev_high = pos.micro_high
            if tick.price > pos.micro_high:
                pos.micro_high = tick.price
                pos.last_new_high_ts = tick.recv_ts or time.time()
            self._maybe_scale_in(pos, tick, prev_high)
            if tick.price <= pos.stop_price and pos.qty > 0:
                self._trigger_stop(pos)
                return
            # Thesis invalidation: before TP1, a print a full tick below the
            # former wall means the reclaim FAILED — exit now, don't ride it
            # down to the (lower) hard stop.
            if (
                settings.s3_reclaim_fail_exit
                and not pos.tp1_filled
                and pos.qty > 0
                # Half-tick threshold: robust to float noise — fires for any
                # print a full tick below the wall, never for a wall retest.
                and tick.price < pos.plan.wall_price - 0.5 * tick_size(pos.plan.wall_price)
            ):
                logger.info("[S3][POS] %s reclaim failed: %.2f back below wall %.2f",
                            pos.symbol, tick.price, pos.plan.wall_price)
                self._flatten(pos, reason="RECLAIM_FAIL")

    def check_time_exits(self, now_ts: float) -> None:
        """Stagnation exit (engine housekeeping, ~1 Hz, tick-independent):
        before TP1, a scalp that stops making new highs is dead weight —
        exit instead of holding full size until the stop or the flatten."""
        limit = settings.s3_stagnation_exit_sec
        if limit <= 0:
            return
        for pos in self.positions.values():
            if (
                pos.state == PosState.OPEN
                and not pos.tp1_filled
                and pos.qty > 0
                and pos.last_new_high_ts > 0
                and now_ts - pos.last_new_high_ts > limit
            ):
                logger.info("[S3][POS] %s stagnant %.0fs without a new high — exiting",
                            pos.symbol, now_ts - pos.last_new_high_ts)
                self._flatten(pos, reason="STAGNATION")

    def on_minute_close(self, symbol: str) -> None:
        """Completed 1-min candle → runner management (close < EMA9 exits)."""
        pos = self.positions.get(symbol)
        if pos is None or pos.closed or pos.state != PosState.OPEN:
            return
        if not (pos.tp1_filled and pos.tp2_filled):
            return
        bar = self._bars.last_completed(symbol, "1m")
        ema = self._bars.ema(symbol, "1m", settings.s3_ema_exit_period)
        if bar is not None and ema is not None and bar.close < ema:
            logger.info("[S3][POS] %s runner exit: 1-min close %.2f < EMA%d %.2f",
                        symbol, bar.close, settings.s3_ema_exit_period, ema)
            self._flatten(pos, reason="EMA9_EXIT")

    # ── Scale-in (tranche 2) ─────────────────────────────────────
    def _maybe_scale_in(self, pos: Position, tick: Tick, prev_high: float) -> None:
        if pos.scale_done or pos.entry1_fill_ts == 0.0:
            return
        elapsed = time.time() - pos.entry1_fill_ts
        if elapsed > settings.s3_scale_window_sec:
            pos.scale_done = True  # window closed
            self._place_targets(pos)
            return
        remaining = pos.full_shares - pos.qty
        if remaining <= 0 or tick.price <= prev_high:
            return
        if settings.s3_scale_requires_flow and not self._tape.flow_supportive(
            pos.symbol, tick.recv_ts
        ):
            return
        # Worst-case risk of the COMBINED position must stay within the
        # original limit (never average down — adds only above micro-high).
        book = self._last_book.get(pos.symbol)
        ask = book.best_ask.price if book and book.best_ask else tick.price
        tick_sz = tick_size(ask)
        limit = round(ask + settings.s3_entry_slippage_ticks * tick_sz, 4)
        worst = (pos.entry_cost + remaining * limit) / (pos.qty + remaining) - pos.stop_price
        if worst * (pos.qty + remaining) > settings.s3_max_risk_dollars * 1.001:
            pos.scale_done = True
            self._place_targets(pos)
            return
        pos.scale_done = True
        order = self._oms.submit(
            pos.symbol, "BUY", remaining, limit, "ENTRY2",
            timeout_sec=settings.s3_entry_timeout_sec,
        )
        if order is not None:
            pos.entry_order_ids.append(order.local_id)

    # ── Levels / targets ─────────────────────────────────────────
    def _recompute_levels(self, pos: Position) -> None:
        if pos.qty <= 0 or pos.entry_vwap <= 0:
            return
        pos.r_value = pos.entry_vwap - pos.stop_price
        if pos.r_value <= 0:
            return
        tick_sz = tick_size(pos.entry_vwap)
        pos.tp1_price = round(pos.entry_vwap + settings.s3_tp1_r_mult * pos.r_value, 4)
        pos.tp2_price = round(pos.entry_vwap + settings.s3_tp2_r_mult * pos.r_value, 4)
        # Integer allocation, remainder → runner.
        third = pos.qty // 3
        pos.tp1_qty = third
        pos.tp2_qty = third
        pos.runner_qty = pos.qty - 2 * third
        # Snap to tick.
        pos.tp1_price = round(round(pos.tp1_price / tick_sz) * tick_sz, 4)
        pos.tp2_price = round(round(pos.tp2_price / tick_sz) * tick_sz, 4)

    def _place_targets(self, pos: Position) -> None:
        """Rest the +1R / +2R limit sells once entries are done."""
        if pos.state != PosState.OPEN or pos.qty <= 0:
            return
        if not pos.scale_done and time.time() - pos.entry1_fill_ts < settings.s3_scale_window_sec:
            return  # wait for the add-on window to resolve
        self._recompute_levels(pos)
        if pos.tp1_order_id is None and pos.tp1_qty > 0:
            o = self._oms.submit(pos.symbol, "SELL", pos.tp1_qty, pos.tp1_price,
                                 "TP1", position_qty=pos.qty)
            pos.tp1_order_id = o.local_id if o else None
        if pos.tp2_order_id is None and pos.tp2_qty > 0:
            o = self._oms.submit(pos.symbol, "SELL", pos.tp2_qty, pos.tp2_price,
                                 "TP2", position_qty=pos.qty)
            pos.tp2_order_id = o.local_id if o else None

    def _move_stop_to_breakeven(self, pos: Position) -> None:
        if pos.breakeven_set:
            return
        tick_sz = tick_size(pos.entry_vwap)
        be = round(pos.entry_vwap + settings.s3_breakeven_cost_ticks * tick_sz, 4)
        if be > pos.stop_price:  # ratchet up only — never widen
            pos.stop_price = be
            pos.breakeven_set = True
            logger.info("[S3][POS] %s stop → breakeven %.2f (TP1 confirmed)",
                        pos.symbol, be)

    # ── Exits ────────────────────────────────────────────────────
    def _trigger_stop(self, pos: Position) -> None:
        if pos.state != PosState.OPEN:
            return
        pos.state = PosState.STOPPING
        pos.exit_reason = pos.exit_reason or "STOP"
        logger.info("[S3][POS] %s HARD STOP @ %.2f (qty %d)",
                    pos.symbol, pos.stop_price, pos.qty)
        self._cancel_targets(pos)
        free = pos.qty - self._oms.open_sell_qty(pos.symbol)
        if free > 0:
            self._urgent_sell(pos, free, "STOP")

    def flatten(self, symbol: str, reason: str = "CUTOFF") -> None:
        pos = self.positions.get(symbol)
        if pos is not None and not pos.closed:
            self._flatten(pos, reason)

    def flatten_all(self, reason: str = "CUTOFF") -> None:
        for pos in list(self.positions.values()):
            if not pos.closed:
                self._flatten(pos, reason)

    def _flatten(self, pos: Position, reason: str) -> None:
        if pos.state in (PosState.STOPPING, PosState.FLATTENING, PosState.CLOSED):
            return
        if pos.state == PosState.PENDING_ENTRY:
            for oid in pos.entry_order_ids:
                self._oms.cancel(oid)
            if pos.qty == 0:
                pos.state = PosState.CLOSED
                pos.exit_reason = reason
                return
        pos.state = PosState.FLATTENING
        pos.exit_reason = reason
        self._cancel_targets(pos)
        free = pos.qty - self._oms.open_sell_qty(pos.symbol)
        if free > 0:
            self._urgent_sell(pos, free, "FLATTEN")

    def _cancel_targets(self, pos: Position) -> None:
        for oid in (pos.tp1_order_id, pos.tp2_order_id):
            if oid:
                self._oms.cancel(oid)
        # Also kill any live entry add-on.
        for oid in pos.entry_order_ids:
            self._oms.cancel(oid)

    def _urgent_sell(self, pos: Position, qty: int, purpose: str, deeper: bool = False) -> None:
        book = self._last_book.get(pos.symbol)
        bid = book.best_bid.price if book and book.best_bid else pos.stop_price
        tick_sz = tick_size(bid)
        ticks = settings.s3_exit_slippage_ticks * (2 if deeper else 1)
        limit = round(max(bid - ticks * tick_sz, tick_sz), 4)
        self._oms.submit(pos.symbol, "SELL", qty, limit, purpose,
                         position_qty=pos.qty, timeout_sec=settings.s3_exit_timeout_sec)

    def _finalize(self, pos: Position) -> None:
        pos.state = PosState.CLOSED
        logger.info("[S3][POS] %s CLOSED (%s) realized %.2f",
                    pos.symbol, pos.exit_reason, pos.realized_pnl)
        self._on_closed(pos)
