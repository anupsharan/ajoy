"""ReplayEngine — re-run recorded JSONL sessions through the full pipeline.

Uses a SimBroker (instant, optimistic fills at the limit price) so replays
never touch OpenD.  Intended for signal/threshold tuning:

    python -m app.services.s3.replay_engine s3_data/s3_20260711.jsonl
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path

from app.config import settings
from app.services.s3.bars import BarAggregator
from app.services.s3.book_analyzer import OrderBookAnalyzer
from app.services.s3.normalizer import MarketDataNormalizer
from app.services.s3.order_manager import OrderManager
from app.services.s3.position_manager import PositionManager
from app.services.s3.strategy_engine import StrategyEngine
from app.services.s3.tape_analyzer import TapeAnalyzer
from app.services.s3.types import Bar, BookLevel, BookSnapshot, FillEvent, Tick

logger = logging.getLogger(__name__)


class SimBroker:
    """Optimistic paper broker for replay: every limit fills immediately."""

    def __init__(self) -> None:
        self.oms: OrderManager | None = None  # wired after construction
        self.orders_placed: int = 0

    def place_limit(self, symbol: str, side: str, qty: int, price: float) -> str | None:
        self.orders_placed += 1
        order_id = f"SIM-{uuid.uuid4().hex[:8]}"
        if self.oms is not None:
            # Deliver the fill on the next call stack turn (same thread).
            self._pending = FillEvent(
                order_id=order_id, deal_id=f"D-{order_id}", symbol=symbol,
                ts=0.0, side=side, price=price, qty=qty,
            )
        return order_id

    def flush_fill(self) -> FillEvent | None:
        fill, self._pending = getattr(self, "_pending", None), None
        return fill

    def cancel_order(self, order_id: str) -> bool:
        return True

    def modify_order_supported(self) -> bool:
        return True


def build_pipeline() -> tuple:
    """Construct the full analytics stack (shared with tests)."""
    normalizer = MarketDataNormalizer(settings.s3_stale_data_max_ms)
    tape = TapeAnalyzer(
        settings.s3_flow_short_window_sec, settings.s3_flow_long_window_sec,
        settings.s3_flow_accel_mult, settings.s3_flow_min_imbalance,
    )
    bars = BarAggregator(settings.s3_ema_exit_period)
    book = OrderBookAnalyzer(
        tape,
        baseline_window_min=settings.s3_baseline_window_min,
        tod_bucket_min=settings.s3_baseline_tod_bucket_min,
        min_samples=settings.s3_baseline_min_samples,
        abs_min_shares=settings.s3_wall_abs_min_shares,
        rel_mult=settings.s3_wall_rel_mult,
        max_level=settings.s3_wall_max_level,
        min_persist_sec=settings.s3_wall_min_persist_sec,
        min_updates=settings.s3_wall_min_updates,
        match_window_ms=settings.s3_match_window_ms,
        min_consumption_ratio=settings.s3_min_consumption_ratio,
        max_pull_ratio=settings.s3_max_pull_ratio,
        confirm_ticks=settings.s3_confirm_ticks,
        require_ask_advance=settings.s3_require_ask_advance,
        reload_veto_frac=settings.s3_reload_veto_frac,
        reload_cooldown_sec=settings.s3_reload_cooldown_sec,
    )
    strategy = StrategyEngine(tape, bars)
    return normalizer, tape, bars, book, strategy


class ReplayEngine:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.normalizer, self.tape, self.bars, self.book, self.strategy = build_pipeline()
        self.broker = SimBroker()
        self.signals: list[dict] = []
        self.trades: list[dict] = []

        self.oms = OrderManager(self.broker, self._on_fill, self._on_terminal)
        self.broker.oms = self.oms
        self.positions = PositionManager(
            self.oms, self.tape, self.bars,
            on_position_opened=lambda p: None,
            on_position_closed=lambda p: self.trades.append(
                {"symbol": p.symbol, "pnl": p.realized_pnl, "reason": p.exit_reason}
            ),
        )

    def _on_fill(self, order, fill) -> None:
        self.positions.on_fill(order, fill)

    def _on_terminal(self, order) -> None:
        self.positions.on_order_terminal(order)

    def _pump_sim_fills(self) -> None:
        fill = self.broker.flush_fill()
        if fill is not None:
            self.oms.on_fill(fill)

    # ── Event dispatch (mirrors S3Engine._dispatch) ──────────────
    def run(self) -> dict:
        n = 0
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(evt["kind"], evt["data"])
                self._pump_sim_fills()
                n += 1
        summary = {
            "events": n,
            "signals": len(self.signals),
            "orders": self.broker.orders_placed,
            "trades": self.trades,
            "pnl": round(sum(t["pnl"] for t in self.trades), 2),
        }
        logger.info("[S3][REPLAY] %s", summary)
        return summary

    def _dispatch(self, kind: str, d: dict) -> None:
        if kind == "tick":
            tick = Tick(
                symbol=d["symbol"], ts=d["ts"], recv_ts=d["recv_ts"],
                price=d["price"], volume=d["volume"], seq=d.get("seq", 0),
            )
            if self.normalizer.normalize_tick(tick) is None:
                return
            self.tape.on_tick(tick)
            self.book.note_print(tick.symbol, tick.price, tick.recv_ts)
            self.bars.on_tick(tick)
            self.positions.on_tick(tick)
        elif kind == "book":
            book = BookSnapshot(
                symbol=d["symbol"], ts=d["ts"], recv_ts=d["recv_ts"],
                bids=[BookLevel(**l) for l in d.get("bids", [])],
                asks=[BookLevel(**l) for l in d.get("asks", [])],
                seq=d.get("seq", 0),
            )
            if self.normalizer.normalize_book(book) is None:
                return
            self.tape.on_book(book)
            self.positions.on_book(book)
            signal = self.book.on_book(book)
            if signal is not None:
                plan = self.strategy.evaluate(signal, book)
                if plan is not None:
                    self.signals.append({"symbol": plan.symbol,
                                         "ts": plan.signal_ts,
                                         "limit": plan.limit_price,
                                         "stop": plan.stop_price})
        elif kind == "bar":
            bar = Bar(
                symbol=d["symbol"], interval=d["interval"],
                start_ts=d["start_ts"], open=d["open"], high=d["high"],
                low=d["low"], close=d["close"], volume=d["volume"],
                turnover=d.get("turnover", 0.0),
                complete=d.get("complete", False),
            )
            completed = self.bars.on_bar(bar)
            if completed is not None and completed.interval == "1m":
                self.positions.on_minute_close(completed.symbol)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) != 2:
        print("usage: python -m app.services.s3.replay_engine <session.jsonl>")
        raise SystemExit(1)
    print(json.dumps(ReplayEngine(sys.argv[1]).run(), indent=2, default=str))
