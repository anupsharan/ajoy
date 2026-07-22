"""
Tests for the July 2026 strategy redesign:
  • Structural (chart-based) stop/target levels + R/R gate
  • Chop-day regime filter (session range vs daily ATR)
  • S2 structure exit (1-min closes through 5-min EMA9)
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from unittest.mock import patch

from app.services.strategy import (
    calculate_atr,
    check_chop_regime,
    check_ema_slope_15m,
    check_entry_signal,
    compute_structural_levels,
    get_regime_from_vwap,
    get_structural_stop_target,
    session_bars,
    should_activate_runner,
)
from app.services.strategy_ema import check_ema_cross_freshness, check_s2_structure_exit
from tests.conftest import make_bar, falling_bars


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

    def test_gap_up_day_counts_gap_in_true_range(self):
        """
        Gap-and-go regression (Jul 9 2026): QQQ gapped +$9 overnight, then
        held a tight $2 intraday range while trending above VWAP.  Plain
        range read 40% of ATR → wrongly blocked; TRUE range vs yesterday's
        close reads (511−500)/5 = 220% → trend day, entries allowed.
        """
        session = bars_from_ohlc(
            [(509.0, 509.5, 509.0, 509.3), (509.3, 511.0, 509.2, 510.8)],
            start=datetime(2026, 7, 9, 10, 0),
        )
        # Without prev_close: intraday range 2.0 / ATR 5.0 = 0.4 → chop
        is_chop, ratio = check_chop_regime(session, daily_atr=5.0, min_ratio=0.5)
        assert is_chop and ratio == pytest.approx(0.4)
        # With prev_close 500 (gap included): (511−500)/5 = 2.2 → NOT chop
        is_chop, ratio = check_chop_regime(
            session, daily_atr=5.0, min_ratio=0.5, prev_close=500.0
        )
        assert not is_chop
        assert ratio == pytest.approx(2.2)

    def test_gap_down_day_counts_gap_too(self):
        """Mirror case: gap DOWN with a tight intraday range is also a move."""
        session = bars_from_ohlc(
            [(491.0, 491.5, 490.0, 490.5), (490.5, 491.2, 490.2, 491.0)],
            start=datetime(2026, 7, 9, 10, 0),
        )
        is_chop, ratio = check_chop_regime(
            session, daily_atr=5.0, min_ratio=0.5, prev_close=500.0
        )
        assert not is_chop
        assert ratio == pytest.approx((500.0 - 490.0) / 5.0)


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
# Session VWAP — regression for the multi-day VWAP bug (NFLX #134, Jul 8 2026)
# ---------------------------------------------------------------------------

class TestSessionVwap:
    def _two_day_1m_bars(self, yday_px=76.6, today_px=75.5, n_each=40, today_rise=0.01):
        """Yesterday trades high; today trades lower but RISING (recovery day)."""
        bars = []
        for i in range(n_each):
            bars.append(make_bar(yday_px, open_=yday_px - 0.02,
                                 ts=datetime(2025, 1, 6, 10, 0) + timedelta(minutes=i)))
        for i in range(n_each):
            c = today_px + i * today_rise
            bars.append(make_bar(c, open_=c - 0.02,
                                 ts=datetime(2025, 1, 7, 10, 0) + timedelta(minutes=i)))
        return bars

    def test_session_bars_filters_to_last_day(self):
        bars = self._two_day_1m_bars()
        sess = session_bars(bars)
        assert len(sess) == 40
        assert all(b.time.date() == bars[-1].time.date() for b in sess)

    def test_recovery_day_put_rejected_wrong_side(self):
        """
        NFLX #134 pattern: yesterday ~76.6, today recovering ~75.5→75.9.
        Blended multi-day VWAP (~76.1) made price look 'below VWAP' → PUT.
        True session VWAP (~75.7) has price ABOVE → wrong side → no signal.
        """
        bars_1m = self._two_day_1m_bars()
        bars_15m = falling_bars(base=78.0, n=40, step=0.05)   # bearish 15-min trend
        from unittest.mock import patch as _patch
        with _patch.object(settings, "ema_slope_filter_enabled", False):
            sig = check_entry_signal(bars_1m, bars_15m)
        assert sig is None   # with the blended VWAP this produced a PUT signal

    def test_regime_uses_session_vwap(self):
        """QQQ recovering above today's VWAP must read BULLISH, not bearish."""
        bars = self._two_day_1m_bars(yday_px=500.0, today_px=490.0, today_rise=0.1)
        # last close 493.9 vs session VWAP ≈ 491.9 → ~+0.4% above → bullish
        # (blended with yesterday's 500s, the old code read this as BEARISH)
        from unittest.mock import patch as _patch
        with _patch.object(settings, "regime_gate_enabled", True), \
             _patch.object(settings, "regime_vwap_threshold", 0.002):
            assert get_regime_from_vwap(bars) == "bullish"


# ---------------------------------------------------------------------------
# S2 volume filter — configurable threshold
# ---------------------------------------------------------------------------

class TestVolumeThreshold:
    def _bars(self, last_vol: int, avg_vol: int = 1000, n: int = 25):
        bars = [make_bar(100.0, open_=99.98, volume=avg_vol,
                         ts=datetime(2025, 1, 6, 10, 0) + timedelta(minutes=i))
                for i in range(n)]
        bars[-1] = make_bar(100.0, open_=99.98, volume=last_vol,
                            ts=datetime(2025, 1, 6, 10, 0) + timedelta(minutes=n - 1))
        return bars

    def test_bar_at_85pct_passes_with_08_ratio(self):
        from app.services.strategy_ema import check_volume_filter
        assert check_volume_filter(self._bars(850), min_ratio=0.8)

    def test_bar_at_70pct_blocked_with_08_ratio(self):
        from app.services.strategy_ema import check_volume_filter
        assert not check_volume_filter(self._bars(700), min_ratio=0.8)

    def test_default_10_ratio_keeps_original_behavior(self):
        from app.services.strategy_ema import check_volume_filter
        from unittest.mock import patch as _patch
        with _patch.object(settings, "s2_volume_min_ratio", 1.0):
            assert not check_volume_filter(self._bars(950))
            assert check_volume_filter(self._bars(1050))


# ---------------------------------------------------------------------------
# Structure exit — min-hold breathing room (NVDA #161 / PLTR #155 regression)
# ---------------------------------------------------------------------------

class TestStructExitMinHold:
    def _broken_setup(self):
        """5-min bars + 1-min bars whose closes sit far below the EMA9."""
        bars_5m = bars_from_ohlc([(100, 100.1, 99.9, 100.0)] * 30, minutes=5)
        bars_1m = bars_from_ohlc([(99.0, 99.1, 98.9, 99.0)] * 10)   # « EMA9=100
        return bars_5m, bars_1m

    def _cond(self, minutes_held):
        from app.services.strategy_ema import check_s2_exit_conditions
        from datetime import timezone as _tz
        bars_5m, bars_1m = self._broken_setup()
        entry = datetime.now(tz=_tz.utc) - timedelta(minutes=minutes_held)
        return check_s2_exit_conditions(
            bars=bars_5m, direction="CALL",
            entry_price=2.00, current_price=1.95, stop_price=1.60,
            be_stop_set=False, bid_price=1.94, entry_time=entry,
            bars_1m=bars_1m, ema9_5m=100.0,
        )

    def test_no_struct_exit_inside_min_hold(self):
        with patch.object(settings, "s2_structure_exit_min_hold_minutes", 10), \
             patch.object(settings, "s2_quick_loss_pct", 0.0):
            cond = self._cond(minutes_held=5)
        assert cond is None                      # breathing room — no exit yet

    def test_struct_exit_fires_after_min_hold(self):
        with patch.object(settings, "s2_structure_exit_min_hold_minutes", 10), \
             patch.object(settings, "s2_quick_loss_pct", 0.0):
            cond = self._cond(minutes_held=15)
        assert cond is not None and cond.reason == "STRUCT_EXIT"

    def test_margin_widened_ignores_shallow_break(self):
        """Closes only 0.1% below EMA9 with a 0.15% margin → not broken."""
        from app.services.strategy_ema import check_s2_structure_exit
        bars = bars_from_ohlc([(99.92, 99.95, 99.88, 99.90)] * 5)   # −0.10%
        assert not check_s2_structure_exit(
            bars, "CALL", ema9_5m=100.0, confirm_bars=2, margin_pct=0.0015)


# ---------------------------------------------------------------------------
# Honest exit labels — STOP vs TRAILING_STOP against the ENTRY-TIME stop
# ---------------------------------------------------------------------------

class TestOriginalStopLabeling:
    def _bars(self):
        # enough completed 5-min bars for the exit engine's EMA needs
        return bars_from_ohlc([(100, 100.1, 99.9, 100.0)] * 30, minutes=5)

    def test_structural_stop_labels_as_stop(self):
        """F #146 regression: structural stop 0.28 > pct-stop 0.24 used to
        mislabel as TRAILING_STOP.  With original_stop stored, it's STOP."""
        from app.services.strategy_ema import check_s2_exit_conditions
        cond = check_s2_exit_conditions(
            bars=self._bars(), direction="PUT",
            entry_price=0.30, current_price=0.27, stop_price=0.28,
            be_stop_set=False, bid_price=0.27,
            original_stop=0.28,               # entry-time snapshot
        )
        assert cond is not None and cond.reason == "STOP"

    def test_raised_stop_labels_as_trailing(self):
        from app.services.strategy_ema import check_s2_exit_conditions
        cond = check_s2_exit_conditions(
            bars=self._bars(), direction="PUT",
            entry_price=0.30, current_price=0.31, stop_price=0.32,
            be_stop_set=True, bid_price=0.31,
            original_stop=0.28,               # stop was genuinely raised
        )
        assert cond is not None and cond.reason == "TRAILING_STOP"

    def test_legacy_rows_fall_back_to_pct(self):
        """Pre-migration trades (original_stop=None) keep the old behavior."""
        from app.services.strategy_ema import check_s2_exit_conditions
        from unittest.mock import patch as _patch
        with _patch.object(settings, "s2_stop_loss_pct", 0.19):
            cond = check_s2_exit_conditions(
                bars=self._bars(), direction="PUT",
                entry_price=0.30, current_price=0.27, stop_price=0.28,
                be_stop_set=False, bid_price=0.27,
                original_stop=None,
            )
        assert cond is not None and cond.reason == "TRAILING_STOP"  # old (wrong) label


# ---------------------------------------------------------------------------
# S2 cross freshness — session-aware
# ---------------------------------------------------------------------------

class TestCrossFreshnessSessionAware:
    def _two_day_bars(self, cross_today: bool):
        """
        60 five-min bars across two dates.  Downward EMA9/21 cross placement:
          cross_today=False → prices fall early on DAY 1 (cross yesterday),
                              then keep drifting down (no new cross today)
          cross_today=True  → prices flat both days, then fall sharply in
                              today's bars (cross today)
        """
        rows = []
        n = 60
        for i in range(n):
            if cross_today:
                px = 100.0 if i < 45 else 100.0 - (i - 44) * 0.8
            else:
                px = 100.0 if i < 5 else 100.0 - (i - 4) * 0.4
            rows.append((px + 0.02, px + 0.05, px - 0.05, px))
        bars = bars_from_ohlc(rows, start=datetime(2025, 1, 6, 9, 30), minutes=5)
        # First 30 bars = Jan 6 (yesterday), last 30 = Jan 7 (today)
        for i, b in enumerate(bars):
            if i >= 30:
                bars[i] = make_bar(b.close, open_=b.open, high=b.high, low=b.low,
                                   ts=datetime(2025, 1, 7, 9, 30) + timedelta(minutes=(i - 30) * 5))
        return bars

    def test_prior_day_cross_blocked_even_within_bar_cap(self):
        bars = self._two_day_bars(cross_today=False)
        with patch.object(settings, "s2_cross_max_bars_old", 99):
            assert not check_ema_cross_freshness(bars, "PUT", ticker="TST")

    def test_same_day_cross_passes(self):
        bars = self._two_day_bars(cross_today=True)
        with patch.object(settings, "s2_cross_max_bars_old", 40):
            assert check_ema_cross_freshness(bars, "PUT", ticker="TST")


# ---------------------------------------------------------------------------
# L1 EMA slope filter
# ---------------------------------------------------------------------------

class TestEmaSlopeFilter:
    def _bars(self, closes):
        return bars_from_ohlc([(c, c + 0.05, c - 0.05, c) for c in closes])

    def test_rising_ema_passes_bullish(self):
        closes = [100 + i * 0.5 for i in range(40)]
        with patch.object(settings, "ema_slope_filter_enabled", True):
            assert check_ema_slope_15m(self._bars(closes), "bullish", period=21, lookback=2)

    def test_rolling_over_ema_blocks_bullish(self):
        # rise then decline — the EMA rolls over even while price may sit above it.
        # (A pure plateau keeps the EMA asymptotically rising, which correctly
        # passes — the filter targets rollovers, not slow convergence.)
        closes = [100 + i * 0.5 for i in range(30)] + [115 - i * 0.5 for i in range(30)]
        with patch.object(settings, "ema_slope_filter_enabled", True):
            assert not check_ema_slope_15m(self._bars(closes), "bullish", period=21, lookback=2)

    def test_falling_ema_passes_bearish(self):
        closes = [130 - i * 0.5 for i in range(40)]
        with patch.object(settings, "ema_slope_filter_enabled", True):
            assert check_ema_slope_15m(self._bars(closes), "bearish", period=21, lookback=2)

    def test_rising_ema_blocks_bearish(self):
        closes = [100 + i * 0.5 for i in range(40)]
        with patch.object(settings, "ema_slope_filter_enabled", True):
            assert not check_ema_slope_15m(self._bars(closes), "bearish", period=21, lookback=2)

    def test_disabled_always_passes(self):
        closes = [100 + i * 0.5 for i in range(30)] + [115.0] * 30
        with patch.object(settings, "ema_slope_filter_enabled", False):
            assert check_ema_slope_15m(self._bars(closes), "bullish", period=21, lookback=2)

    def test_insufficient_data_passes(self):
        with patch.object(settings, "ema_slope_filter_enabled", True):
            assert check_ema_slope_15m(self._bars([100.0] * 5), "bullish", period=21, lookback=2)


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


# ---------------------------------------------------------------------------
# S1 PUT kill switch (review lever)
# ---------------------------------------------------------------------------

def test_s1_put_kill_switch_blocks_puts():
    from tests.conftest import falling_bars as _fb
    from app.services.strategy import calculate_vwap
    bars_15m = _fb(base=200.0, n=40, step=0.5)
    vwap = calculate_vwap(bars_15m)
    bars_1m = [make_bar(vwap * 0.994, open_=vwap * 0.9945) for _ in range(30)]
    from unittest.mock import patch as _p
    with _p.object(settings, "s1_puts_enabled", False), \
         _p.object(settings, "ema_slope_filter_enabled", False), \
         _p.object(settings, "vwap_min_clearance_pct", 0.0):
        assert check_entry_signal(bars_1m, bars_15m) is None
    with _p.object(settings, "s1_puts_enabled", True), \
         _p.object(settings, "ema_slope_filter_enabled", False), \
         _p.object(settings, "vwap_min_clearance_pct", 0.0):
        sig = check_entry_signal(bars_1m, bars_15m)
    assert sig is not None and sig.direction == "PUT"
