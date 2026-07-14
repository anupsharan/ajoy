"""Unit tests for the S3 ask-wall breakout pipeline (no OpenD required)."""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.services.s3.bars import BarAggregator
from app.services.s3.book_analyzer import OrderBookAnalyzer, tick_size
from app.services.s3.normalizer import MarketDataNormalizer
from app.services.s3.order_manager import OrderManager, OrderState
from app.services.s3.risk_manager import RiskManager
from app.services.s3.tape_analyzer import TapeAnalyzer
from app.services.s3.types import (
    Bar,
    BookLevel,
    BookSnapshot,
    FillEvent,
    OrderUpdate,
    Tick,
)

SYM = "TEST"


# ── Helpers ──────────────────────────────────────────────────────
def make_book(ts: float, bid=100.00, ask=100.02, ask_sizes=None, seq=0) -> BookSnapshot:
    ask_sizes = ask_sizes or [500, 400, 300]
    bids = [BookLevel(price=round(bid - i * 0.01, 2), size=500) for i in range(3)]
    asks = [BookLevel(price=round(ask + i * 0.01, 2), size=s)
            for i, s in enumerate(ask_sizes)]
    return BookSnapshot(symbol=SYM, ts=ts, recv_ts=ts, bids=bids, asks=asks, seq=seq)


def make_tape() -> TapeAnalyzer:
    return TapeAnalyzer(5.0, 30.0, 1.5, 0.25)


def make_analyzer(tape: TapeAnalyzer) -> OrderBookAnalyzer:
    return OrderBookAnalyzer(
        tape,
        baseline_window_min=20, tod_bucket_min=600,  # one giant TOD bucket
        min_samples=15, abs_min_shares=5000, rel_mult=5.0, max_level=3,
        min_persist_sec=0.5, min_updates=3, match_window_ms=750,
        min_consumption_ratio=0.6, max_pull_ratio=0.35,
        confirm_ticks=1, require_ask_advance=True,
    )


# ── Normalizer ───────────────────────────────────────────────────
class TestNormalizer:
    def test_crossed_book_rejected(self):
        n = MarketDataNormalizer(1500)
        t = time.time()
        book = BookSnapshot(symbol=SYM, ts=t, recv_ts=t,
                            bids=[BookLevel(100.05, 100)],
                            asks=[BookLevel(100.00, 100)])
        assert n.normalize_book(book) is None
        assert n.quality.rejected["book_crossed"] == 1

    def test_locked_book_rejected(self):
        n = MarketDataNormalizer(1500)
        t = time.time()
        book = BookSnapshot(symbol=SYM, ts=t, recv_ts=t,
                            bids=[BookLevel(100.00, 100)],
                            asks=[BookLevel(100.00, 100)])
        assert n.normalize_book(book) is None

    def test_duplicate_and_out_of_sequence(self):
        n = MarketDataNormalizer(1500)
        t = time.time()
        assert n.normalize_book(make_book(t, seq=5)) is not None
        assert n.normalize_book(make_book(t, seq=5)) is None    # duplicate
        assert n.normalize_book(make_book(t, seq=3)) is None    # out of sequence

    def test_bad_tick_rejected(self):
        n = MarketDataNormalizer(1500)
        t = time.time()
        assert n.normalize_tick(Tick(SYM, t, t, price=0.0, volume=10)) is None
        assert n.normalize_tick(Tick(SYM, t, t, price=10.0, volume=100)) is not None


# ── Tape / aggressor inference ───────────────────────────────────
class TestTape:
    def test_aggressor_from_quote_not_colors(self):
        tape = make_tape()
        t = time.time()
        tape.on_book(make_book(t))
        buy = tape.on_tick(Tick(SYM, t, t, price=100.02, volume=100))
        sell = tape.on_tick(Tick(SYM, t, t, price=100.00, volume=100))
        mid = tape.on_tick(Tick(SYM, t, t, price=100.01, volume=100))
        assert buy.aggressor.value == "BUY"
        assert sell.aggressor.value == "SELL"
        assert mid.aggressor.value == "NEUTRAL"

    def test_flow_acceleration(self):
        tape = make_tape()
        t = time.time()
        tape.on_book(make_book(t))
        # Sparse old buying, burst of recent buying.
        for i in range(3):
            tape.on_tick(Tick(SYM, t - 25 + i, t - 25 + i, price=100.02, volume=50))
        for i in range(10):
            tape.on_tick(Tick(SYM, t - 1, t - 1, price=100.02, volume=400))
        m = tape.metrics(SYM, t)
        assert m.accelerating
        assert m.imbalance > 0.9


# ── Wall detection & consumption ─────────────────────────────────
class TestWall:
    def _warm_up(self, ana, t0, n=20):
        for i in range(n):
            ana.on_book(make_book(t0 + i * 0.1, seq=i + 1))

    def test_consumed_wall_fires_breakout(self):
        tape = make_tape()
        ana = make_analyzer(tape)
        t0 = time.time() - 60
        self._warm_up(ana, t0)
        seq = 100
        tw = t0 + 10
        # Wall appears: 20k on the best ask vs ~500 baseline.
        for i in range(4):
            b = make_book(tw + i * 0.3, ask_sizes=[20000, 400, 300], seq=seq + i)
            tape.on_book(b)
            ana.on_book(b)
        # Aggressive buys chew through it; book reflects the reduction.
        t1 = tw + 1.5
        for i in range(4):
            ts = t1 + i * 0.2
            tape.on_tick(Tick(SYM, ts, ts, price=100.02, volume=5000))
            ana.note_print(SYM, 100.02, ts)
            remaining = 20000 - (i + 1) * 5000
            b = make_book(ts + 0.05, ask_sizes=[max(remaining, 0) or 1, 400, 300],
                          seq=seq + 10 + i)
            tape.on_book(b)
            sig = ana.on_book(b)
        # Wall gone; ask advances; print confirms above former wall.
        tf = t1 + 1.2
        tape.on_tick(Tick(SYM, tf, tf, price=100.03, volume=800))
        ana.note_print(SYM, 100.03, tf)
        adv = BookSnapshot(
            symbol=SYM, ts=tf + 0.05, recv_ts=tf + 0.05,
            bids=[BookLevel(100.02, 500)], asks=[BookLevel(100.03, 400)],
            seq=seq + 50,
        )
        tape.on_book(adv)
        sig = ana.on_book(adv)
        assert sig is not None
        assert sig.wall.consumption_ratio >= 0.6
        assert sig.wall.pull_ratio <= 0.35

    def test_withdrawn_wall_never_fires(self):
        tape = make_tape()
        ana = make_analyzer(tape)
        t0 = time.time() - 60
        self._warm_up(ana, t0)
        tw = t0 + 10
        seq = 200
        for i in range(4):
            b = make_book(tw + i * 0.3, ask_sizes=[20000, 400, 300], seq=seq + i)
            tape.on_book(b)
            ana.on_book(b)
        # Wall vanishes with NO aggressive-buy prints → pure withdrawal.
        gone = make_book(tw + 2.0, ask_sizes=[1, 400, 300], seq=seq + 10)
        tape.on_book(gone)
        assert ana.on_book(gone) is None
        assert ana.active_wall(SYM) is None  # resolved as WITHDRAWN

    def test_iceberg_reload_vetoes_wall_and_blocks_level(self):
        """Wall refreshes upward after real consumption → RELOADED veto,
        and the price level cannot be re-detected during the cooldown."""
        tape = make_tape()
        ana = make_analyzer(tape)
        t0 = time.time() - 60
        self._warm_up(ana, t0)
        tw, seq = t0 + 10, 400
        for i in range(4):
            b = make_book(tw + i * 0.3, ask_sizes=[20000, 400, 300], seq=seq + i)
            tape.on_book(b)
            ana.on_book(b)
        # 30% consumed via matched aggressive buys…
        ts = tw + 1.5
        tape.on_tick(Tick(SYM, ts, ts, price=100.02, volume=6000))
        b = make_book(ts + 0.05, ask_sizes=[14000, 400, 300], seq=seq + 10)
        tape.on_book(b)
        ana.on_book(b)
        assert ana.active_wall(SYM) is not None
        # …then the wall RELOADS by +8000 (≥25% of initial) → veto.
        b = make_book(ts + 0.4, ask_sizes=[22000, 400, 300], seq=seq + 11)
        tape.on_book(b)
        assert ana.on_book(b) is None
        assert ana.active_wall(SYM) is None
        # Same price level cannot become a wall again inside the cooldown.
        for i in range(6):
            b = make_book(ts + 1.0 + i * 0.2, ask_sizes=[25000, 400, 300],
                          seq=seq + 20 + i)
            tape.on_book(b)
            ana.on_book(b)
        assert ana.active_wall(SYM) is None

    def test_small_level_not_a_wall(self):
        tape = make_tape()
        ana = make_analyzer(tape)
        t0 = time.time() - 60
        self._warm_up(ana, t0)
        b = make_book(t0 + 10, ask_sizes=[4000, 400, 300], seq=300)  # < abs floor
        ana.on_book(b)
        assert ana.active_wall(SYM) is None


# ── Risk sizing ──────────────────────────────────────────────────
class TestRisk:
    def _rm(self):
        bars = BarAggregator(9)
        return RiskManager(bars, MarketDataNormalizer(1500)), bars

    def test_shares_formula(self, monkeypatch):
        rm, _ = self._rm()
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 100.0)
        monkeypatch.setattr(settings, "s3_max_notional", 1e9)
        d = rm.size_position(SYM, entry_px=50.00, stop_px=49.80)
        assert d.shares == 500  # 100 / 0.20

    def test_notional_cap_binds(self, monkeypatch):
        rm, _ = self._rm()
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 1000.0)
        monkeypatch.setattr(settings, "s3_max_notional", 5000.0)
        d = rm.size_position(SYM, entry_px=100.0, stop_px=99.9)
        assert d.shares == 50
        assert d.reason == "NOTIONAL"

    def test_participation_cap(self, monkeypatch):
        rm, bars = self._rm()
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 1000.0)
        monkeypatch.setattr(settings, "s3_max_notional", 1e9)
        monkeypatch.setattr(settings, "s3_max_participation", 0.05)
        bar = Bar(SYM, "1m", 0, 100, 101, 99, 100, volume=2000, complete=True)
        bars.on_bar(bar)
        bars.on_bar(Bar(SYM, "1m", 60, 100, 101, 99, 100, volume=1, complete=False))
        d = rm.size_position(SYM, entry_px=100.0, stop_px=99.9)
        assert d.shares == 100  # 5% of 2000
        assert d.reason == "PARTICIPATION"

    def test_min_shares_viability_floor(self, monkeypatch):
        """A 3-share position on a $385 stock must be skipped, not taken."""
        rm, _ = self._rm()
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 50.0)
        monkeypatch.setattr(settings, "s3_max_notional", 1400.0)
        monkeypatch.setattr(settings, "s3_min_shares", 20)
        d = rm.size_position(SYM, entry_px=385.0, stop_px=384.80)  # notional → 3 sh
        assert not d.approved
        assert "BELOW_MIN_SHARES" in d.reason
        # Cheap symbol passes: $12 stock, 5¢ stop → risk 1000sh, notional 116sh
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 50.0)
        d2 = rm.size_position(SYM, entry_px=12.0, stop_px=11.95)
        assert d2.approved and d2.shares >= 20

    def test_stop_sanity(self):
        rm, _ = self._rm()
        assert rm.stop_valid(100.0, 99.99) == "STOP_TOO_TIGHT"
        assert rm.stop_valid(100.0, 97.0) == "STOP_TOO_WIDE"
        assert rm.stop_valid(100.0, 99.5) is None

    def test_daily_loss_halt(self, monkeypatch):
        rm, _ = self._rm()
        monkeypatch.setattr(settings, "s3_max_daily_loss", 300.0)
        rm.day.record_exit(SYM, -301.0, time.time())
        assert rm.check_halts() == "MAX_DAILY_LOSS"

    def test_consecutive_losses(self, monkeypatch):
        rm, _ = self._rm()
        monkeypatch.setattr(settings, "s3_max_daily_loss", 0.0)
        monkeypatch.setattr(settings, "s3_max_consecutive_losses", 3)
        for _ in range(3):
            rm.day.record_exit(SYM, -1.0, time.time())
        assert rm.check_halts() == "CONSECUTIVE_LOSSES"


# ── OMS state machine ────────────────────────────────────────────
class _FakeBroker:
    def __init__(self):
        self.next_id = 0
        self.cancels: list[str] = []

    def place_limit(self, symbol, side, qty, price):
        self.next_id += 1
        return str(self.next_id)

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return True

    def modify_order_supported(self):
        return True


class TestOMS:
    def _oms(self):
        fills, terminals = [], []
        oms = OrderManager(_FakeBroker(),
                           on_fill=lambda o, f: fills.append((o, f)),
                           on_terminal=lambda o: terminals.append(o))
        return oms, fills, terminals

    def test_duplicate_deal_ignored(self):
        oms, fills, _ = self._oms()
        o = oms.submit(SYM, "BUY", 100, 10.0, "ENTRY1")
        f = FillEvent(order_id=o.broker_id, deal_id="D1", symbol=SYM,
                      ts=0, side="BUY", price=10.0, qty=50)
        oms.on_fill(f)
        oms.on_fill(f)  # duplicate push
        assert o.filled_qty == 50
        assert len(fills) == 1

    def test_oversell_blocked(self):
        oms, _, _ = self._oms()
        assert oms.submit(SYM, "SELL", 60, 10.0, "TP1", position_qty=100) is not None
        assert oms.submit(SYM, "SELL", 60, 11.0, "TP2", position_qty=100) is None

    def test_stale_cancel_after_fill(self):
        oms, _, _ = self._oms()
        o = oms.submit(SYM, "BUY", 100, 10.0, "ENTRY1")
        oms.on_fill(FillEvent(o.broker_id, "D1", SYM, 0, "BUY", 10.0, 100))
        assert o.state == OrderState.FILLED
        # Late CANCELLED push must not un-fill the order.
        oms.on_order_update(OrderUpdate(o.broker_id, SYM, 0, "CANCELLED_ALL",
                                        "BUY", 10.0, 100, 100, 10.0))
        assert o.state == OrderState.FILLED

    def test_poll_snapshot_synthesizes_incremental_fills(self):
        """Tradier path: order rows carry exec_quantity/avg_fill_price —
        snapshots must produce idempotent incremental fills."""
        oms, fills, _ = self._oms()
        o = oms.submit(SYM, "BUY", 100, 10.0, "ENTRY1")
        snap1 = OrderUpdate(o.broker_id, SYM, 0, "FILLED_PART",
                            "BUY", 10.0, 100, 40, 10.00)
        oms.on_order_snapshot(snap1)
        oms.on_order_snapshot(snap1)          # duplicate poll — no double count
        assert o.filled_qty == 40
        assert len(fills) == 1
        snap2 = OrderUpdate(o.broker_id, SYM, 0, "FILLED_ALL",
                            "BUY", 10.0, 100, 100, 10.06)
        oms.on_order_snapshot(snap2)
        assert o.state == OrderState.FILLED
        assert o.filled_qty == 100
        # Incremental price of the last 60 shares: (10.06·100 − 10.00·40)/60 = 10.10
        assert fills[-1][1].price == pytest.approx(10.10)

    def test_partial_then_filled(self):
        oms, _, terminals = self._oms()
        o = oms.submit(SYM, "BUY", 100, 10.0, "ENTRY1")
        oms.on_fill(FillEvent(o.broker_id, "D1", SYM, 0, "BUY", 10.0, 40))
        assert o.state == OrderState.PARTIAL
        oms.on_fill(FillEvent(o.broker_id, "D2", SYM, 0, "BUY", 10.1, 60))
        assert o.state == OrderState.FILLED
        assert o.filled_avg == pytest.approx(10.06)
        assert terminals and terminals[-1] is o


# ── PositionManager exits ────────────────────────────────────────
class TestPositionExits:
    def _pm(self, monkeypatch):
        from app.services.s3.position_manager import PositionManager
        from app.services.s3.strategy_engine import EntryPlan
        monkeypatch.setattr(settings, "s3_initial_tranche_pct", 0.50)
        monkeypatch.setattr(settings, "s3_entry_timeout_sec", 3.0)
        closed = []
        tape, bars = make_tape(), BarAggregator(9)
        oms = OrderManager(_FakeBroker(), on_fill=lambda o, f: pm.on_fill(o, f),
                           on_terminal=lambda o: pm.on_order_terminal(o))
        pm = PositionManager(oms, tape, bars,
                             on_position_opened=lambda p: None,
                             on_position_closed=closed.append)
        plan = EntryPlan(symbol=SYM, limit_price=100.05, stop_price=99.90,
                         wall_price=100.02, signal_ts=time.time())
        pos = pm.open_position(plan, 100)          # tranche1 = 50
        entry = oms.order(pos.entry_order_ids[0])
        oms.on_fill(FillEvent(entry.broker_id, "D1", SYM, 0, "BUY", 100.04, 50))
        pm.on_book(make_book(time.time(), bid=100.03, ask=100.05))
        assert pos.state.value == "OPEN" and pos.qty == 50
        return pm, oms, pos

    def test_reclaim_fail_exit(self, monkeypatch):
        pm, oms, pos = self._pm(monkeypatch)
        monkeypatch.setattr(settings, "s3_reclaim_fail_exit", True)
        t = time.time()
        # One tick below the former wall (100.02 − 0.01) → immediate exit.
        pm.on_tick(Tick(SYM, t, t, price=100.01, volume=100))
        assert pos.state.value == "FLATTENING"
        assert pos.exit_reason == "RECLAIM_FAIL"
        assert oms.open_sell_qty(SYM) == 50       # urgent sell covers all shares

    def test_no_reclaim_exit_at_wall_price(self, monkeypatch):
        pm, oms, pos = self._pm(monkeypatch)
        monkeypatch.setattr(settings, "s3_reclaim_fail_exit", True)
        t = time.time()
        pm.on_tick(Tick(SYM, t, t, price=100.02, volume=100))  # retest, not failure
        assert pos.state.value == "OPEN"

    def test_scale_in_fires_on_micro_high_break(self, monkeypatch):
        """Regression: on_tick used to update micro_high BEFORE the scale-in
        comparison, so 'price > previous high' was never true and ENTRY2
        could never fire (caught by the E2E harness)."""
        pm, oms, pos = self._pm(monkeypatch)
        monkeypatch.setattr(settings, "s3_scale_window_sec", 30.0)
        monkeypatch.setattr(settings, "s3_scale_requires_flow", False)
        monkeypatch.setattr(settings, "s3_max_risk_dollars", 1000.0)
        t = time.time()
        pm.on_tick(Tick(SYM, t, t, price=100.06, volume=100))  # breaks 100.04 high
        assert len(pos.entry_order_ids) == 2, "ENTRY2 must fire on micro-high break"
        entry2 = oms.order(pos.entry_order_ids[1])
        assert entry2.qty == pos.full_shares - 50

    def test_stagnation_exit(self, monkeypatch):
        pm, oms, pos = self._pm(monkeypatch)
        monkeypatch.setattr(settings, "s3_stagnation_exit_sec", 60.0)
        pos.last_new_high_ts = time.time() - 61
        pm.check_time_exits(time.time())
        assert pos.state.value == "FLATTENING"
        assert pos.exit_reason == "STAGNATION"

    def test_stagnation_clock_resets_on_new_high(self, monkeypatch):
        pm, oms, pos = self._pm(monkeypatch)
        monkeypatch.setattr(settings, "s3_stagnation_exit_sec", 60.0)
        pos.last_new_high_ts = time.time() - 61
        t = time.time()
        pm.on_tick(Tick(SYM, t, t, price=100.10, volume=100))  # new high resets clock
        pm.check_time_exits(time.time())
        assert pos.state.value == "OPEN"


# ── R allocation / misc ──────────────────────────────────────────
class TestMisc:
    def test_tick_size(self):
        assert tick_size(25.0) == 0.01
        assert tick_size(0.50) == 0.0001

    def test_integer_thirds_remainder_to_runner(self):
        # Mirrors PositionManager._recompute_levels allocation.
        for qty in (3, 7, 100, 101, 1, 2):
            third = qty // 3
            runner = qty - 2 * third
            assert third * 2 + runner == qty
            assert runner >= third  # remainder always favors the runner

    def test_vwap_from_bars(self):
        bars = BarAggregator(9)
        bars.on_bar(Bar(SYM, "1m", 0, 10, 11, 9, 10, volume=100, turnover=1000.0, complete=True))
        bars.on_bar(Bar(SYM, "1m", 60, 10, 12, 10, 12, volume=100, turnover=1200.0, complete=True))
        bars.on_bar(Bar(SYM, "1m", 120, 12, 12, 12, 12, volume=1, complete=False))
        assert bars.vwap(SYM) == pytest.approx(11.0)

    def test_ema_needs_enough_bars(self):
        bars = BarAggregator(9)
        for i in range(5):
            bars.on_bar(Bar(SYM, "1m", i * 60, 10, 10, 10, 10 + i, volume=1, complete=True))
        bars.on_bar(Bar(SYM, "1m", 999, 10, 10, 10, 10, volume=1, complete=False))
        assert bars.ema(SYM, "1m", 9) is None
        for i in range(6, 15):
            bars.on_bar(Bar(SYM, "1m", i * 60, 10, 10, 10, 15, volume=1, complete=True))
        bars.on_bar(Bar(SYM, "1m", 9999, 10, 10, 10, 15, volume=1, complete=False))
        assert bars.ema(SYM, "1m", 9) is not None
