"""S3 end-to-end integration test — full lifecycle, no OpenD/Tradier needed.

Drives the REAL S3Engine dispatch path with synthetic market data and a fake
instant-fill broker, against a throwaway SQLite DB:

  warmup baselines → wall detected → consumed by aggressive buys →
  breakout signal → risk-sized ENTRY1 → fill → scale-in ENTRY2 →
  TP1 fill (stop→breakeven) → TP2 fill → runner EMA9 exit →
  trade persisted CLOSED → recorded JSONL replayed through ReplayEngine.

Run:  uv run python scripts/s3_e2e_test.py
Exits 0 on success; prints FAIL + assertion detail otherwise.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Isolate side effects BEFORE importing the app ────────────────
_tmp = Path(tempfile.mkdtemp(prefix="s3_e2e_"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/e2e.db"

from app.config import settings  # noqa: E402

settings_overrides = {
    "database_url": f"sqlite+aiosqlite:///{_tmp}/e2e.db",
    "s3_record_dir": str(_tmp / "rec"),
    "s3_record_events": True,
    # keep the session gates open regardless of wall-clock
    "s3_trading_start_time": "00:00",
    "s3_last_entry_time": "23:59",
    "s3_flatten_time": "23:59",
    "s3_stale_data_max_ms": 10_000_000,
    # compress timing so the test runs in milliseconds
    "s3_baseline_min_samples": 30,
    "s3_wall_min_persist_sec": 0.5,
    "s3_wall_min_updates": 3,
    "s3_scale_requires_flow": False,
    "s3_stagnation_exit_sec": 3600.0,
    "s3_halt_quiet_sec": 3600.0,
    "s3_max_risk_dollars": 50.0,
    "s3_max_participation": 0.05,
    "s3_max_notional": 1e9,          # let the participation cap be the binding one
    "s3_kill_switch": False,
    "s3_max_daily_loss": 0.0,
    "s3_max_consecutive_losses": 0,
}
for k, v in settings_overrides.items():
    object.__setattr__(settings, k, v)

from sqlalchemy import create_engine, select  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Trade  # noqa: E402
from app.services.s3.engine import S3Engine  # noqa: E402
from app.services.s3.replay_engine import ReplayEngine  # noqa: E402
from app.services.s3.types import (  # noqa: E402
    Bar, BookLevel, BookSnapshot, EngineState, Event, FillEvent, Tick,
)

SYM = "TEST"
PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1


class InstantBroker:
    """Fake execution venue: every limit order fills instantly at its price."""

    def __init__(self) -> None:
        self.engine: S3Engine | None = None
        self.n = 0
        self.available = True

    def place_limit(self, symbol: str, side: str, qty: int, price: float) -> str:
        self.n += 1
        oid = f"E2E-{self.n}"
        self._pending = (oid, symbol, side, qty, price)
        return oid

    def deliver_fill(self, price: float | None = None) -> None:
        oid, sym, side, qty, px = self._pending
        self.engine.oms.on_fill(FillEvent(
            order_id=oid, deal_id=f"D-{oid}", symbol=sym, ts=time.time(),
            side=side, price=price if price is not None else px, qty=qty,
        ))

    def cancel_order(self, order_id: str) -> bool:
        return True

    def modify_order_supported(self) -> bool:
        return True

    def describe(self) -> str:
        return "e2e/instant"

    def fetch_buying_power(self) -> float:
        return 100_000.0

    def fetch_open_orders(self):
        return []

    def fetch_positions(self):
        return {}

    def poll_order(self, order_id: str):
        return None

    def close(self) -> None:
        pass


def book(ts: float, bid=100.00, ask=100.02, ask_sizes=None, bid_jitter=0, seq=0):
    ask_sizes = ask_sizes or [500, 400, 300]
    return BookSnapshot(
        symbol=SYM, ts=ts, recv_ts=ts,
        bids=[BookLevel(round(bid - i * 0.01, 2), 500 + bid_jitter + i) for i in range(3)],
        asks=[BookLevel(round(ask + i * 0.01, 2), s) for i, s in enumerate(ask_sizes)],
        seq=seq,
    )


def main() -> int:
    engine = S3Engine()
    broker = InstantBroker()
    broker.engine = engine
    engine.broker = broker            # execution side swapped for the fake
    engine.oms._broker = broker
    engine.state = EngineState.RUNNING
    engine.risk.account.buying_power = 100_000.0

    # Fresh tables in the throwaway DB.
    sync = create_engine(f"sqlite:///{_tmp}/e2e.db", future=True)
    Base.metadata.create_all(sync)
    engine._db_engine = sync
    from sqlalchemy.orm import sessionmaker
    engine._session_factory = sessionmaker(sync, expire_on_commit=False)

    D = engine._dispatch
    now = time.time()

    print("── Stage 1: bars (VWAP / EMA / 5-min context) ──")
    for i in range(12):
        D(Event("bar", Bar(SYM, "1m", i * 60, 100.0, 100.05, 99.95, 100.0,
                           volume=2000, turnover=200_000.0)))
    D(Event("bar", Bar(SYM, "5m", 0, 100.0, 100.15, 99.90, 100.10, volume=9000,
                       turnover=901_000.0)))
    D(Event("bar", Bar(SYM, "5m", 300, 100.1, 100.12, 100.0, 100.05, volume=100,
                       turnover=10_005.0)))
    vwap = engine.bars.vwap(SYM)
    check("session VWAP computed", vwap is not None and 99.5 < vwap < 100.5)
    check("5-min trend context OK", engine.bars.trend_ok(SYM))

    print("── Stage 2: baseline warmup (35 books + ticks) ──")
    seq = 0
    for i in range(35):
        seq += 1
        t = now - 90 + i
        D(Event("book", book(t, ask_sizes=[500 + i % 7, 400, 300],
                             bid_jitter=i % 5, seq=seq)))
        if i % 5 == 0:
            D(Event("tick", Tick(SYM, t, t, price=100.01, volume=50, seq=seq)))
    check("no wall during warmup", engine.book_analyzer.active_wall(SYM) is None)
    q = engine.normalizer.quality
    check("books accepted by normalizer", q.accepted_books >= 30,
          f"accepted={q.accepted_books} rejected={dict(q.rejected)}")

    print("── Stage 3: wall appears, persists, gets consumed ──")
    tw = now - 8
    for i in range(4):
        seq += 1
        D(Event("book", book(tw + i * 0.4, ask_sizes=[20000, 400, 300], seq=seq)))
    wall = engine.book_analyzer.active_wall(SYM)
    check("wall detected", wall is not None and wall.initial_size == 20000)

    t1 = tw + 2.0
    for i in range(4):
        ts = t1 + i * 0.4
        seq += 1
        D(Event("tick", Tick(SYM, ts, ts, price=100.02, volume=5000, seq=seq)))
        remaining = 20000 - (i + 1) * 5000
        seq += 1
        D(Event("book", book(ts + 0.05, ask_sizes=[max(remaining, 1), 400, 300],
                             seq=seq)))
    wall = engine.book_analyzer.active_wall(SYM)
    check("wall mostly consumed", wall is not None and wall.consumption_ratio >= 0.6,
          f"wall={wall}")

    print("── Stage 4: breakout confirmation → ENTRY1 ──")
    tf = time.time()
    # burst of aggressive buying for the flow-acceleration gate
    for j in range(6):
        seq += 1
        D(Event("tick", Tick(SYM, tf, tf, price=100.03, volume=800, seq=seq)))
    seq += 1
    adv = BookSnapshot(symbol=SYM, ts=tf, recv_ts=tf,
                       bids=[BookLevel(100.02, 600)],
                       asks=[BookLevel(100.03, 400)], seq=seq)
    D(Event("book", adv))

    pos = engine.positions.positions.get(SYM)
    check("position created on breakout", pos is not None)
    if pos is None:
        return finish()
    entry1 = engine.oms.order(pos.entry_order_ids[0])
    check("ENTRY1 submitted ~50% of size", entry1 is not None
          and entry1.qty == pos.tranche1_shares
          and abs(entry1.qty / pos.full_shares - 0.5) < 0.11,
          f"qty={getattr(entry1, 'qty', '?')} full={pos.full_shares}")
    check("participation cap bound sizing", pos.full_shares == 100,
          f"full_shares={pos.full_shares}")
    check("stop below entry, structurally derived",
          0 < pos.stop_price < entry1.limit_price)

    broker.deliver_fill(100.04)
    check("position OPEN after fill",
          pos.state.value == "OPEN" and pos.qty == pos.tranche1_shares)

    print("── Stage 5: scale-in on micro-high break ──")
    ts2 = time.time()
    seq += 1
    D(Event("tick", Tick(SYM, ts2, ts2, price=100.06, volume=500, seq=seq)))
    check("ENTRY2 submitted", len(pos.entry_order_ids) == 2)
    broker.deliver_fill(100.05)
    check("full size after scale-in", pos.qty == pos.full_shares, f"qty={pos.qty}")
    check("targets placed (TP1+TP2)", pos.tp1_order_id and pos.tp2_order_id)
    check("R computed", pos.r_value > 0, f"R={pos.r_value}")
    third = pos.qty // 3
    check("thirds allocation, remainder→runner",
          pos.tp1_qty == third and pos.tp2_qty == third
          and pos.runner_qty == pos.qty - 2 * third
          and pos.runner_qty >= third)
    check("oversell guard: sells ≤ position",
          engine.oms.open_sell_qty(SYM) <= pos.qty)

    print("── Stage 6: TP1 → breakeven, TP2 ──")
    stop_before = pos.stop_price
    tp1 = engine.oms.order(pos.tp1_order_id)
    engine.oms.on_fill(FillEvent(tp1.broker_id, "D-TP1", SYM, time.time(),
                                 "SELL", pos.tp1_price, pos.tp1_qty))
    check("TP1 filled", pos.tp1_filled and pos.qty == pos.tp2_qty + pos.runner_qty)
    check("stop moved to breakeven (up only)",
          pos.breakeven_set and pos.stop_price > stop_before,
          f"{stop_before} → {pos.stop_price}")
    tp2 = engine.oms.order(pos.tp2_order_id)
    engine.oms.on_fill(FillEvent(tp2.broker_id, "D-TP2", SYM, time.time(),
                                 "SELL", pos.tp2_price, pos.tp2_qty))
    check("TP2 filled, runner remains", pos.tp2_filled and pos.qty == pos.runner_qty)

    print("── Stage 7: runner exits on 1-min close < EMA9 ──")
    D(Event("bar", Bar(SYM, "1m", 2000, 100.0, 100.1, 99.4, 99.50, volume=1500,
                       turnover=149_250.0)))
    D(Event("bar", Bar(SYM, "1m", 2060, 99.5, 99.6, 99.4, 99.55, volume=100,
                       turnover=9_955.0)))  # completes the 99.50 bar
    check("runner flattening on EMA9 exit",
          pos.state.value in ("FLATTENING", "CLOSED") and pos.exit_reason == "EMA9_EXIT",
          f"state={pos.state.value} reason={pos.exit_reason}")
    runner_px = broker._pending[4]          # urgent-sell limit price
    runner_qty = pos.runner_qty
    broker.deliver_fill()                   # urgent sell fills at that limit
    check("position CLOSED, all shares out", pos.closed and pos.qty == 0)
    expected_pnl = ((pos.tp1_price - pos.entry_vwap) * pos.tp1_qty
                    + (pos.tp2_price - pos.entry_vwap) * pos.tp2_qty
                    + (runner_px - pos.entry_vwap) * runner_qty)
    check("realized P&L matches leg-by-leg math",
          abs(pos.realized_pnl - expected_pnl) < 0.01,
          f"realized={pos.realized_pnl:.2f} expected={expected_pnl:.2f}")

    print("── Stage 8: DB persistence ──")
    with engine._session_factory() as db:
        trade = db.execute(select(Trade).where(Trade.symbol == SYM)).scalar_one_or_none()
    check("trade row persisted", trade is not None)
    if trade is not None:
        check("strategy_name=S3, status=closed",
              trade.strategy_name == "S3" and trade.status.value == "closed")
        check("exit_reason=EMA9_EXIT", trade.exit_reason.value == "EMA9_EXIT",
              f"got {trade.exit_reason}")
        check("pnl persisted matches", abs((trade.pnl or 0) - round(pos.realized_pnl, 2)) < 0.01)

    print("── Stage 9: recorded session replays cleanly ──")
    engine.recorder.close()
    rec_files = sorted(Path(settings.s3_record_dir).glob("*.jsonl"))
    check("JSONL recording exists", bool(rec_files) and rec_files[0].stat().st_size > 0)
    if rec_files:
        summary = ReplayEngine(rec_files[0]).run()
        check("replay reproduces ≥1 signal", summary["signals"] >= 1,
              f"summary={summary}")

    print("── Stage 10: kill switch flattens & halts ──")
    # fresh mini-position via direct open on a second symbol not needed —
    # verify halt path flags only
    object.__setattr__(settings, "s3_kill_switch", True)
    engine._last_housekeep = 0
    engine._housekeeping()
    check("engine HALTED on kill switch", engine.state == EngineState.HALTED)
    object.__setattr__(settings, "s3_kill_switch", False)

    return finish()


def finish() -> int:
    print(f"\n{'='*50}\nE2E RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
