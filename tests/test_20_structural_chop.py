"""
Tests for the July 2026 strategy redesign:
  • Structural (chart-based) stop/target levels + R/R gate
  • Chop-day regime filter (session range vs daily ATR)
  • S2 structure exit (1-min closes through 5-min EMA9)
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.strategy import (
    calculate_atr,
    check_chop_regime,
    compute_structural_levels,
    get_structural_stop_target,
    should_activate_runner,
)
from app.services.strategy_ema import check_s2_structure_exit
from tests.conftest import make_bar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bars_from_ohlc(rows, start=None, minutes=1):
    """rows: list of (open, high, low, close) → list[Bar] with sequential times."""
    # Past date so completed_bars() always treats every bar as closed,
    # regardless of when the test suite runs.
    start = start or datetime(2025, 1, 6, 10, 0)
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(make_bar(c, open_=o, high=h, low=l, ts=start + timedelta(minutes=i * minutes)))
    return out


# ---------------------------------------------------------------------------
# compute_structural_levels
# ---------------------------------------------------------------------------

class TestComputeStructuralLevels:
    def test_call_normal(self):
        # entry 100, stop 99 (1 below), target 103 (3 above) → R/R 3.0
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=100.0, stop_underlying=99.0, target_underlying=103.0,
            min_stop_pct=0.08, max_stop_pct=0.30, min_reward_risk=1.2,
        )
        assert s.ok
        assert s.reward_risk == pytest.approx(3.0)
        # option stop: 2.00 − 0.5×1.0 = 1.50 → −25%, inside [8%, 30%]
        assert s.stop_price == pytest.approx(1.50)
        # option tp: 2.00 + 0.5×3.0 = 3.50
        assert s.tp_price == pytest.approx(3.50)
        assert s.risk_pct == pytest.approx(0.25)

    def test_put_normal(self):
        s = compute_structural_levels(
            direction="PUT", option_entry=2.00, delta=-0.50,
            underlying_entry=100.0, stop_underlying=101.0, target_underlying=97.0,
            min_stop_pct=0.08, max_stop_pct=0.30, min_reward_risk=1.2,
        )
        assert s.ok
        assert s.reward_risk == pytest.approx(3.0)
        assert s.stop_price == pytest.approx(1.50)
        assert s.tp_price == pytest.approx(3.50)

    def test_rr_gate_skips(self):
        # stop 1 below, target only 1 above → R/R 1.0 < 1.2 → skip
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=100.0, stop_underlying=99.0, target_underlying=101.0,
            min_reward_risk=1.2,
        )
        assert not s.ok
        assert "reward/risk" in s.skip_reason

    def test_no_room_to_target_skips(self):
        # price already at the session high → tp_dist ≤ 0 → skip
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=100.0, stop_underlying=99.0, target_underlying=100.0,
        )
        assert not s.ok
        assert "no room" in s.skip_reason

    def test_price_beyond_invalidation_skips(self):
        # CALL but price already below the stop level → broken setup
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=98.0, stop_underlying=99.0, target_underlying=103.0,
        )
        assert not s.ok
        assert "invalidation" in s.skip_reason

    def test_missing_delta_falls_back(self):
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=None,
            underlying_entry=100.0, stop_underlying=99.0, target_underlying=103.0,
        )
        assert not s.ok
        assert s.skip_reason == "fallback"

    def test_stop_clamped_to_max(self):
        # huge stop distance: 2.00 − 0.5×4.0 = 0 → clamp to max 30%
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=100.0, stop_underlying=96.0, target_underlying=110.0,
            min_stop_pct=0.08, max_stop_pct=0.30, min_reward_risk=1.2,
        )
        assert s.ok
        assert s.risk_pct == pytest.approx(0.30)
        assert s.stop_price == pytest.approx(1.40)  # 2.00 × 0.70

    def test_stop_clamped_to_min(self):
        # tiny stop distance: 0.5×0.10/2.00 = 2.5% → widen to min 8%
        s = compute_structural_levels(
            direction="CALL", option_entry=2.00, delta=0.50,
            underlying_entry=100.0, stop_underlying=99.90, target_underlying=103.0,
            min_stop_pct=0.08, max_stop_pct=0.30, min_reward_risk=1.2,
        )
        assert s.ok
        assert s.risk_pct == pytest.approx(0.08)
        assert s.stop_price == pytest.approx(1.84)  # 2.00 × 0.92


# ---------------------------------------------------------------------------
# get_structural_stop_target
# ---------------------------------------------------------------------------

class TestStructuralStopTarget:
    def test_call_uses_pullback_low_and_session_high(self):
        rows = [
            (100.0, 101.5, 99.8, 101.0),   # session high 101.5
            (101.0, 101.2, 100.2, 100.4),  # pullback
            (100.4, 100.6, 99.9, 100.1),   # pullback low 99.9
            (100.1, 100.8, 100.0, 100.7),  # bounce
            (100.7, 100.9, 100.5, 100.8),  # in-progress bar (excluded from lows)
        ]
        bars = bars_from_ohlc(rows)
        stop_u, target_u = get_structural_stop_target(
            bars, "CALL", anchor=100.0, buffer_pct=0.001, pullback_lookback=10,
        )
        # stop below min(pullback low 99.8 across completed bars, anchor 100.0)
        assert stop_u == pytest.approx(99.8 * 0.999)
        assert target_u == pytest.approx(101.5)

    def test_put_mirrored(self):
        rows = [
            (100.0, 100.2, 98.5, 99.0),    # session low 98.5
            (99.0, 99.8, 98.9, 99.6),      # pullback high 99.8
            (99.6, 99.9, 99.2, 99.3),      # 99.9 pullback high
            (99.3, 99.4, 99.0, 99.1),
            (99.1, 99.2, 98.9, 99.0),      # in-progress
        ]
        bars = bars_from_ohlc(rows)
        stop_u, target_u = get_structural_stop_target(
            bars, "PUT", anchor=99.5, buffer_pct=0.001, pullback_lookback=10,
        )
        # max completed-bar high is 100.2 (bar 0) > anchor 99.5
        assert stop_u == pytest.approx(100.2 * 1.001)
        assert target_u == pytest.approx(98.5)

    def test_insufficient_data(self):
        assert get_structural_stop_target([], "CALL", anchor=100.0) == (0.0, 0.0)
        bars = bars_from_ohlc([(100, 100.5, 99.5, 100.2)])
        assert get_structural_stop_target(bars, "CALL", anchor=100.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# ATR + chop regime
# ---------------------------------------------------------------------------

class TestAtrAndChop:
    def _daily_bars(self, n=20, rng=5.0, base=500.0):
        rows = []
        for i in range(n):
            rows.append((base, base + rng, base, base + rng / 2))
        return bars_from_ohlc(rows, start=datetime(2026, 6, 1), minutes=60 * 24)

    def test_atr_constant_range(self):
        bars = self._daily_bars(n=20, rng=5.0)
        assert calculate_atr(bars, period=14) == pytest.approx(5.0)

    def test_atr_insufficient_data(self):
        assert calculate_atr(self._daily_bars(n=5), period=14) == 0.0

    def test_chop_day_detected(self):
        # session range 1.0 vs ATR 5.0 → ratio 0.2 < 0.5 → chop
        session = bars_from_ohlc(
            [(500.0, 500.5, 499.8, 500.2), (500.2, 500.8, 499.9, 500.3)],
            start=datetime(2026, 7, 7, 10, 0),
        )
        is_chop, ratio = check_chop_regime(session, daily_atr=5.0, min_ratio=0.5)
        assert is_chop
        assert ratio == pytest.approx(1.0 / 5.0)

    def test_trend_day_passes(self):
        # session range 4.0 vs ATR 5.0 → ratio 0.8 ≥ 0.5 → not chop
        session = bars_from_ohlc(
            [(500.0, 501.0, 499.0, 500.8), (500.8, 503.0, 500.5, 502.5)],
            start=datetime(2026, 7, 7, 10, 0),
        )
        is_chop, ratio = check_chop_regime(session, daily_atr=5.0, min_ratio=0.5)
        assert not is_chop
        assert ratio == pytest.approx(4.0 / 5.0)

    def test_missing_data_never_blocks(self):
        assert check_chop_regime([], daily_atr=5.0) == (False, 0.0)
        session = bars_from_ohlc([(500, 501, 499, 500.5)])
        assert check_chop_regime(session, daily_atr=0.0) == (False, 0.0)


# ---------------------------------------------------------------------------
# S2 structure exit
# ---------------------------------------------------------------------------

class TestS2StructureExit:
    def _bars(self, closes, start=None):
        rows = [(c + 0.05, c + 0.1, c - 0.1, c) for c in closes]
        # Times in the past so all bars count as completed
        return bars_from_ohlc(rows, start=start or datetime(2025, 1, 6, 10, 0))

    def test_call_exit_on_two_closes_below_ema9(self):
        # EMA9 at 100.0; last two completed closes well below
        bars = self._bars([100.5, 100.2, 99.80, 99.70])
        assert check_s2_structure_exit(bars, "CALL", ema9_5m=100.0,
                                       confirm_bars=2, margin_pct=0.0005)

    def test_call_no_exit_single_wick(self):
        # only ONE close below EMA9 — needs two consecutive
        bars = self._bars([100.5, 100.2, 99.70, 100.10])
        assert not check_s2_structure_exit(bars, "CALL", ema9_5m=100.0,
                                           confirm_bars=2, margin_pct=0.0005)

    def test_call_no_exit_close_on_ema(self):
        # closes below EMA9 but inside the margin → not broken
        bars = self._bars([100.5, 100.2, 99.97, 99.96])
        assert not check_s2_structure_exit(bars, "CALL", ema9_5m=100.0,
                                           confirm_bars=2, margin_pct=0.001)

    def test_put_exit_on_two_closes_above_ema9(self):
        bars = self._bars([99.5, 99.8, 100.30, 100.40])
        assert check_s2_structure_exit(bars, "PUT", ema9_5m=100.0,
                                       confirm_bars=2, margin_pct=0.0005)

    def test_no_exit_insufficient_data(self):
        assert not check_s2_structure_exit([], "CALL", ema9_5m=100.0)
        assert not check_s2_structure_exit(self._bars([99.0]), "CALL", ema9_5m=0.0)


# ---------------------------------------------------------------------------
# Runner mode activation
# ---------------------------------------------------------------------------

class TestRunnerActivation:
    def _momentum_bars(self, direction="CALL"):
        """bars[-2] is the momentum candle (bars[-1] is treated as in-progress)."""
        if direction == "CALL":
            closes = [(100.0, 100.1), (100.1, 100.4), (100.4, 100.5)]  # (open, close)
        else:
            closes = [(100.4, 100.3), (100.3, 100.0), (100.0, 99.9)]
        rows = [(o, max(o, c) + 0.05, min(o, c) - 0.05, c) for o, c in closes]
        return bars_from_ohlc(rows)

    def _fading_bars(self):
        """bars[-2] is a red candle — momentum faded for a CALL."""
        rows = [
            (100.0, 100.5, 99.95, 100.4),
            (100.4, 100.45, 100.0, 100.1),   # red, falling — no momentum
            (100.1, 100.2, 100.0, 100.15),   # in-progress
        ]
        return bars_from_ohlc(rows)

    def test_activates_at_target_with_momentum(self):
        bars = self._momentum_bars("CALL")
        # TP 4.00, bid 3.85 → within 5% zone; momentum candle present
        assert should_activate_runner(3.85, 4.00, bars, "CALL", proximity_pct=0.05)

    def test_put_direction(self):
        bars = self._momentum_bars("PUT")
        assert should_activate_runner(3.85, 4.00, bars, "PUT", proximity_pct=0.05)

    def test_no_activation_far_from_target(self):
        bars = self._momentum_bars("CALL")
        # bid 3.50 is more than 5% below TP 4.00 → not in the zone
        assert not should_activate_runner(3.50, 4.00, bars, "CALL", proximity_pct=0.05)

    def test_no_activation_when_momentum_faded(self):
        bars = self._fading_bars()
        # in the zone, but last completed candle is red → TP should fire normally
        assert not should_activate_runner(3.90, 4.00, bars, "CALL", proximity_pct=0.05)

    def test_no_activation_without_tp(self):
        bars = self._momentum_bars("CALL")
        assert not should_activate_runner(3.90, None, bars, "CALL")
        assert not should_activate_runner(3.90, 0.0, bars, "CALL")
