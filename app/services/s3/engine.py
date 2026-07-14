"""S3Engine — event-driven orchestrator thread.

SDK callbacks (MoomooClient) enqueue plain events onto a bounded queue; this
thread is the single consumer and the only place strategy state mutates, so
no analytics code needs locks.

Connection-loss protocol (spec):
  1. block new entries               (state → DISCONNECTED)
  2. reconnect + resubscribe         (exponential backoff)
  3. reconcile broker orders/fills/positions with local state
  4. resume only after reconciliation succeeds (state → RUNNING)

Trades persist into the existing `trades` table (strategy_name="S3") via a
synchronous SQLAlchemy session owned by this thread.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Direction, ExitReason, Symbol, Trade, TradeStatus
from app.services.s3.event_recorder import EventRecorder
from app.services.s3.moomoo_client import MoomooClient
from app.services.s3.order_manager import ManagedOrder, OrderManager
from app.services.s3.position_manager import Position, PositionManager
from app.services.s3.replay_engine import build_pipeline
from app.services.s3.tradier_broker import TradierEquityBroker
from app.services.s3.types import (
    Bar,
    BookSnapshot,
    EngineState,
    Event,
    FillEvent,
    OrderUpdate,
    Tick,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_EXIT_REASON_MAP = {
    "STOP": ExitReason.STOP,
    "EMA9_EXIT": ExitReason.EMA9_EXIT,
    "R1": ExitReason.R1,
    "R2": ExitReason.R2,
    "STAGNATION": ExitReason.STAGNATION,
    "RECLAIM_FAIL": ExitReason.RECLAIM_FAIL,
    "CUTOFF": ExitReason.CUTOFF,
    "KILL_SWITCH": ExitReason.MANUAL,
    "SHUTDOWN": ExitReason.MANUAL,
}


class S3Engine:
    def __init__(self) -> None:
        self.state = EngineState.STOPPED
        self.q: "queue.Queue[Event]" = queue.Queue(maxsize=settings.s3_queue_max)

        # Execution split: Moomoo OpenD is the DATA source; orders route to
        # the broker selected by S3_BROKER (default: Tradier, reusing the
        # app's existing credentials + USE_SANDBOX switch).
        self._moomoo_executes = settings.s3_broker.lower() == "moomoo"
        self.client = MoomooClient(self.q, data_only=not self._moomoo_executes)
        self.broker = self.client if self._moomoo_executes else TradierEquityBroker()
        self.recorder = EventRecorder(settings.s3_record_dir, settings.s3_record_events)

        (self.normalizer, self.tape, self.bars,
         self.book_analyzer, self.strategy) = build_pipeline()

        from app.services.s3.risk_manager import RiskManager  # avoid cycle at import
        self.risk = RiskManager(self.bars, self.normalizer)
        self.oms = OrderManager(self.broker, self._on_fill, self._on_terminal)
        self.positions = PositionManager(
            self.oms, self.tape, self.bars,
            on_position_opened=self._persist_open,
            on_position_closed=self._persist_close,
        )

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._symbols: list[str] = []
        self._trade_ids: dict[str, int] = {}       # symbol → trades.id
        self._last_book_fp: dict[str, tuple] = {}  # halt detection
        self._last_book_change: dict[str, float] = {}
        self._suspect_halt: set[str] = set()
        self._last_housekeep = 0.0
        self._last_bp_refresh = 0.0

        sync_url = settings.database_url.replace("+aiosqlite", "")
        self._db_engine = create_engine(sync_url, future=True)
        self._session_factory = sessionmaker(self._db_engine, expire_on_commit=False)

    # ── Lifecycle ────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="s3-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # ── Main loop ────────────────────────────────────────────────
    def _run(self) -> None:
        self.state = EngineState.STARTING
        logger.info("[S3] engine starting (env=%s)", settings.s3_trd_env)

        if not self.client.connect():
            logger.error("[S3] startup failed — engine idle. %s", self.client.caps.report())
            self.state = EngineState.STOPPED
            return

        self._symbols = self._load_symbols()
        if not self._symbols:
            logger.warning("[S3] no active S1 symbols to trade — engine idle")
            self.state = EngineState.STOPPED
            self.client.close()
            return
        if not self.client.subscribe(self._symbols):
            self.state = EngineState.STOPPED
            self.client.close()
            return

        self._reconcile()
        self.state = EngineState.RUNNING
        logger.info("[S3] RUNNING — %d symbols: %s | data=moomoo/OpenD exec=%s",
                    len(self._symbols), self._symbols, self._broker_desc())
        if not self._exec_available():
            logger.error("[S3] execution broker unavailable (%s) — SIGNAL-ONLY, "
                         "no orders will be placed", self._broker_desc())

        while not self._stop.is_set():
            try:
                event = self.q.get(timeout=0.25)
            except queue.Empty:
                event = None
            try:
                if event is not None:
                    self._dispatch(event)
                self._housekeeping()
            except Exception:  # noqa: BLE001 — engine must never die silently
                logger.exception("[S3] event-loop error")

        self._shutdown()

    def _shutdown(self) -> None:
        logger.info("[S3] graceful shutdown — cancelling orders, flattening")
        try:
            for order in self.oms.live_orders():
                self.oms.cancel(order.local_id)
            if self.positions.open_count() > 0:
                self.positions.flatten_all("SHUTDOWN")
                deadline = time.time() + 8
                while self.positions.open_count() > 0 and time.time() < deadline:
                    try:
                        self._dispatch(self.q.get(timeout=0.25))
                    except queue.Empty:
                        pass
        finally:
            self.recorder.close()
            self.client.close()
            if self.broker is not self.client:
                self.broker.close()
            self.state = EngineState.STOPPED
            logger.info("[S3] stopped")

    # ── Execution-broker helpers ─────────────────────────────────
    def _exec_available(self) -> bool:
        if self._moomoo_executes:
            return self.client.can_trade
        return self.broker.available

    def _broker_desc(self) -> str:
        if self._moomoo_executes:
            return f"moomoo/{settings.s3_trd_env}"
        return self.broker.describe()

    def _poll_broker_orders(self) -> None:
        """Tradier has no order push — poll live orders and feed snapshots
        into the OMS (fills synthesized from exec_quantity deltas)."""
        if self._moomoo_executes:
            return
        for order in self.oms.live_orders():
            if order.broker_id is None:
                continue
            upd = self.broker.poll_order(order.broker_id)
            if upd is not None:
                self.recorder.record("order", upd)
                self.oms.on_order_snapshot(upd)

    # ── Dispatch ─────────────────────────────────────────────────
    def _dispatch(self, event: Event) -> None:
        kind, payload = event.kind, event.payload

        if kind == "tick" and isinstance(payload, Tick):
            tick = self.normalizer.normalize_tick(payload)
            if tick is None:
                return
            self.recorder.record("tick", tick)
            self.tape.on_tick(tick)
            self.book_analyzer.note_print(tick.symbol, tick.price, tick.recv_ts)
            self.bars.on_tick(tick)
            self.positions.on_tick(tick)
            self._suspect_halt.discard(tick.symbol)

        elif kind == "book" and isinstance(payload, BookSnapshot):
            book = self.normalizer.normalize_book(payload)
            if book is None:
                return
            self.recorder.record("book", book)
            self._track_halt(book)
            self.tape.on_book(book)
            self.positions.on_book(book)
            signal = self.book_analyzer.on_book(book)
            if signal is not None and self.state == EngineState.RUNNING:
                self._try_enter(signal, book)

        elif kind == "bar" and isinstance(payload, Bar):
            self.recorder.record("bar", payload)
            completed = self.bars.on_bar(payload)
            if completed is not None and completed.interval == "1m":
                self.positions.on_minute_close(completed.symbol)

        elif kind == "order" and isinstance(payload, OrderUpdate):
            self.recorder.record("order", payload)
            self.oms.on_order_update(payload)

        elif kind == "fill" and isinstance(payload, FillEvent):
            self.recorder.record("fill", payload)
            self.oms.on_fill(payload)

        elif kind == "control" and isinstance(payload, dict):
            if payload.get("type") == "DISCONNECTED":
                self._handle_disconnect()

    # ── Entry path ───────────────────────────────────────────────
    def _try_enter(self, signal, book: BookSnapshot) -> None:
        sym = signal.symbol
        if self.positions.has_position(sym):
            return
        if sym in self._suspect_halt:
            logger.info("[S3][SKIP] %s suspected halt — entry blocked", sym)
            return
        blocked = self.risk.entry_blocked(
            sym, book,
            open_positions=self.positions.open_count(),
            portfolio_notional=self.positions.total_notional(),
            now_ts=book.recv_ts,
        )
        if blocked:
            logger.info("[S3][SKIP] %s entry blocked: %s", sym, blocked)
            return
        plan = self.strategy.evaluate(signal, book)
        if plan is None:
            return
        invalid = self.risk.stop_valid(plan.limit_price, plan.stop_price)
        if invalid:
            logger.info("[S3][SKIP] %s %s (entry %.2f stop %.2f)",
                        sym, invalid, plan.limit_price, plan.stop_price)
            return
        decision = self.risk.size_position(sym, plan.limit_price, plan.stop_price)
        if not decision.approved:
            logger.info("[S3][SKIP] %s sizing rejected: %s", sym, decision.reason)
            return
        self.recorder.record("decision", {
            "symbol": sym, "limit": plan.limit_price, "stop": plan.stop_price,
            "shares": decision.shares, "wall": plan.wall_price,
        })
        pos = self.positions.open_position(plan, decision.shares)
        if pos is not None:
            self.risk.day.record_entry(sym)

    # ── OMS callbacks ────────────────────────────────────────────
    def _on_fill(self, order: ManagedOrder, fill: FillEvent) -> None:
        self.positions.on_fill(order, fill)

    def _on_terminal(self, order: ManagedOrder) -> None:
        self.positions.on_order_terminal(order)

    # ── Housekeeping (≤1 Hz) ─────────────────────────────────────
    def _housekeeping(self) -> None:
        now_ts = time.time()
        if now_ts - self._last_housekeep < 1.0:
            return
        self._last_housekeep = now_ts

        self.oms.sweep_timeouts(now_ts)
        self._poll_broker_orders()
        self.positions.check_time_exits(now_ts)

        # Kill switch (hot-reloadable from the Settings page).
        if settings.s3_kill_switch and self.state == EngineState.RUNNING:
            logger.warning("[S3] KILL SWITCH — flattening all, halting")
            self.positions.flatten_all("KILL_SWITCH")
            self.state = EngineState.HALTED

        # Daily-loss / consecutive-loss halts.
        if self.state == EngineState.RUNNING and self.risk.check_halts():
            logger.warning("[S3] HALT: %s — flattening", self.risk.halted_reason)
            self.positions.flatten_all("KILL_SWITCH")
            self.state = EngineState.HALTED

        # End-of-window flatten.
        if self.risk.past_flatten_time() and self.positions.open_count() > 0:
            logger.info("[S3] flatten time reached — closing all")
            self.positions.flatten_all("CUTOFF")

        # Buying-power refresh (30 s) — from the EXECUTION broker.
        if now_ts - self._last_bp_refresh > 30 and self._exec_available():
            self.risk.account.buying_power = self.broker.fetch_buying_power()
            self.risk.account.ts = now_ts
            self._last_bp_refresh = now_ts

        # Connection liveness (5 s cadence via modulo of housekeeping tick).
        if int(now_ts) % 5 == 0 and self.state in (
            EngineState.RUNNING, EngineState.HALTED
        ):
            if not self.client.ping():
                self._handle_disconnect()

    def _track_halt(self, book: BookSnapshot) -> None:
        fp = (
            tuple((l.price, l.size) for l in book.bids[:3]),
            tuple((l.price, l.size) for l in book.asks[:3]),
        )
        if self._last_book_fp.get(book.symbol) != fp:
            self._last_book_fp[book.symbol] = fp
            self._last_book_change[book.symbol] = book.recv_ts
            self._suspect_halt.discard(book.symbol)
            return
        quiet = book.recv_ts - self._last_book_change.get(book.symbol, book.recv_ts)
        m = self.tape.metrics(book.symbol, book.recv_ts)
        if (
            quiet > settings.s3_halt_quiet_sec
            and book.recv_ts - m.last_trade_ts > settings.s3_halt_quiet_sec
            and book.symbol not in self._suspect_halt
        ):
            self._suspect_halt.add(book.symbol)
            logger.warning("[S3] %s static book + silent tape %.0fs — suspected halt",
                           book.symbol, quiet)

    # ── Disconnect / reconcile ───────────────────────────────────
    def _handle_disconnect(self) -> None:
        if self.state == EngineState.DISCONNECTED:
            return
        logger.error("[S3] connection lost — blocking entries, reconnecting")
        self.state = EngineState.DISCONNECTED
        backoff = settings.s3_reconnect_backoff_sec
        while not self._stop.is_set():
            self.client.close()
            time.sleep(backoff)
            if self.client.connect() and self.client.resubscribe():
                self.state = EngineState.RECONCILING
                if self._reconcile():
                    self.state = EngineState.RUNNING
                    logger.info("[S3] reconnected + reconciled — resuming")
                    return
            backoff = min(backoff * 2, 60.0)

    def _reconcile(self) -> bool:
        """Replay broker truth into the OMS and cross-check positions."""
        try:
            if self._moomoo_executes:
                self.oms.reconcile(self.client.fetch_open_orders(),
                                   self.client.fetch_fills())
                broker_pos = self.client.fetch_positions()
            else:
                # Tradier: order rows carry fill progress — snapshot replay.
                self.oms.reconcile(self.broker.fetch_open_orders(), [],
                                   snapshots=True)
                broker_pos = self.broker.fetch_positions()
            for sym, pos in self.positions.positions.items():
                if pos.closed:
                    continue
                broker_qty = broker_pos.get(sym, 0)
                if broker_qty != pos.qty:
                    logger.error(
                        "[S3][RECONCILE] %s local qty %d != broker %d — adopting broker",
                        sym, pos.qty, broker_qty,
                    )
                    pos.qty = max(broker_qty, 0)
                    if pos.qty == 0:
                        pos.state = pos.state.__class__.CLOSED
                        pos.exit_reason = pos.exit_reason or "STOP"
                        self._persist_close(pos)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("[S3] reconciliation failed")
            return False

    # ── DB / symbols ─────────────────────────────────────────────
    def _load_symbols(self) -> list[str]:
        """S3 trades watchlist rows with the per-symbol S3 flag enabled
        (Symbols page — mirrors the S1/S2 pills)."""
        with self._session_factory() as db:
            rows = db.query(Symbol).filter(
                Symbol.active == True,          # noqa: E712
                Symbol.s3_enabled == True,      # noqa: E712
            ).all()
            return [r.ticker for r in rows]

    def _persist_open(self, pos: Position) -> None:
        try:
            with self._session_factory() as db:
                trade = Trade(
                    symbol=pos.symbol,
                    option_symbol=pos.symbol,          # stock — no contract
                    direction=Direction.CALL,          # long-only
                    strategy_name="S3",
                    quantity=pos.qty,
                    remaining_qty=pos.qty,
                    entry_price=pos.entry_vwap,
                    entry_time=datetime.now(ET),
                    stop_price=pos.stop_price,
                    tp1_price=pos.tp1_price or None,
                    tp2_price=pos.tp2_price or None,
                    status=TradeStatus.OPEN,
                )
                db.add(trade)
                db.commit()
                self._trade_ids[pos.symbol] = trade.id
        except Exception:  # noqa: BLE001
            logger.exception("[S3] failed to persist open for %s", pos.symbol)

    def _persist_close(self, pos: Position) -> None:
        trade_id = self._trade_ids.pop(pos.symbol, None)
        if trade_id is None:
            return
        try:
            with self._session_factory() as db:
                trade = db.get(Trade, trade_id)
                if trade is None:
                    return
                sold = max(pos.sold_qty, 1)
                trade.quantity = pos.sold_qty or trade.quantity
                trade.remaining_qty = 0
                trade.exit_price = pos.entry_vwap + pos.realized_pnl / sold
                trade.exit_time = datetime.now(ET)
                trade.exit_reason = _EXIT_REASON_MAP.get(
                    pos.exit_reason or "", ExitReason.MANUAL
                )
                trade.pnl = round(pos.realized_pnl, 2)
                trade.status = TradeStatus.CLOSED
                trade.tp1_hit = pos.tp1_filled
                trade.be_stop_set = pos.breakeven_set
                db.commit()
            self.risk.day.record_exit(pos.symbol, pos.realized_pnl, time.time())
        except Exception:  # noqa: BLE001
            logger.exception("[S3] failed to persist close for %s", pos.symbol)

    # ── Status (for /api/s3/status) ──────────────────────────────
    def status(self) -> dict:
        return {
            "state": self.state.value,
            "data_source": "moomoo/OpenD",
            "execution_broker": self._broker_desc(),
            "can_trade": self._exec_available(),
            "capabilities": self.client.caps.report(),
            "symbols": self._symbols,
            "open_positions": self.positions.open_count(),
            "realized_pnl": round(self.risk.day.realized_pnl, 2),
            "consecutive_losses": self.risk.day.consecutive_losses,
            "halted_reason": self.risk.halted_reason,
            "suspected_halts": sorted(self._suspect_halt),
            "dropped_events": self.client.dropped_events,
            "data_quality": dict(self.normalizer.quality.rejected),
        }


# ── Module-level singleton for the FastAPI app ───────────────────
_engine: S3Engine | None = None


def start_s3_engine() -> None:
    global _engine
    if not settings.s3_enabled:
        logger.info("[S3] disabled (S3_ENABLED=0) — engine not started")
        return
    if _engine is None:
        _engine = S3Engine()
        _engine.start()


def stop_s3_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.stop()
        _engine = None


def s3_status() -> dict:
    if _engine is None:
        return {"state": "STOPPED", "enabled": settings.s3_enabled}
    return _engine.status()
