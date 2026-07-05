"""
Tests for Strategy 2 — EMA Pullback entry functions.

Covers:
  check_5min_trend_filter   — 5-min trend direction gate (EMA + VWAP + slope, cached)
  get_5min_ema9             — extract current EMA9 price level from 5-min bars
  check_1min_pullback       — bars[-2] (pullback bar) touched 5m-EMA9
  check_1min_confirmation   — bars[-1] (confirm bar) broke pullback-bar's range
  check_option_spread       — bid/ask spread ≤ max_spread_pct
  check_volume_filter       — current 1-min volume ≥ rolling lookback average
  check_s2_exit_conditions  — stop / trail / EMA-cross exit priority cascade

Design note on the two-bar sequential pattern:
  check_1min_pullback  checks bars[-2]  (the bar that touched EMA9)
  check_1min_confirmation checks bars[-1] vs bars[-2] (the break-out candle)
  The same bar list is passed to both functions in "sequence" integration tests.

Timestamp note:
  The test sandbox runs in UTC but completed_bars() interprets naive datetimes as ET
  (via `last.replace(tzinfo=ET)`).  At 16:17 UTC, _now_ET ≈ 12:17 ET, so any naive
  bar timestamped after ~12:12 ET looks "currently forming" and gets dropped.
  All non-VWAP test bars use a FIXED historical date (2024-01-15) to guarantee they
  are always treated as completed regardless of the current clock.
  VWAP tests need today's date and use 9 AM ET morning hours (well before any run time).
"""
import pytest
from datetime import datetime, timedelta, date as _date

from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy_ema import (
    _trend_cache,
    check_5min_trend_filter,
    check_1min_confirmation,
    check_1min_pullback,
    check_option_spread,
    check_s2_exit_conditions,
    check_volume_filter,
    get_5min_ema9,
)

# ── Fixed historical base timestamp ───────────────────────────────────────────
# 2024-01-15 10:00 AM.  Naive, historical.  `completed_bars()` treats it as ET,
# and 10:00 AM ET on a past date is always "in the past" → never dropped.
_BASE = datetime(2024, 1, 15, 10, 0)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the module-level trend cache before and after every test."""
    _trend_cache.clear()
    yield
    _trend_cache.clear()


# ── Bar helpers ───────────────────────────────────────────────────────────────

def _bar(close, open_=None, high=None, low=None, volume=500_000, offset_min=0):
    """
    Bar at _BASE + offset_min.  Safe for completed_bars() in any timezone:
    _BASE is a 2024 date, so _BASE+anything is clearly in the past even if
    interpreted as ET.
    """
    ts = _BASE + timedelta(minutes=offset_min)
    return make_bar(close, open_=open_, high=high, low=low, volume=volume, ts=ts)


def _1m_sequence(pullback_kwargs, confirm_kwargs, n_filler=5):
    """
    Build [filler×n_filler, pullback_bar, confirm_bar] with monotonically
    increasing timestamps from _BASE.

    Result layout:
      bars[-2] = pullback_bar  (minutes = n_filler)
      bars[-1] = confirm_bar   (minutes = n_filler + 1)
    """
    filler = [_bar(150.0, offset_min=i) for i in range(n_filler)]
    pb  = _bar(offset_min=n_filler,     **pullback_kwargs)
    cfm = _bar(offset_min=n_filler + 1, **confirm_kwargs)
    return filler + [pb, cfm]


def _rising_5m(base=150.0, n=35, step=0.05):
    """
    Rising 5-min bars on a historical date.
    EMA9 > EMA21, both slopes up.
    VWAP is bypassed (bars not from today) — only EMA conditions are tested.
    """
    bars = []
    for i in range(n):
        c = round(base + i * step, 4)
        o = round(c - 0.02, 4)
        ts = _BASE + timedelta(minutes=i * 5)
        bars.append(make_bar(c, open_=o, ts=ts))
    return bars


def _falling_5m(base=152.0, n=35, step=0.05):
    """Falling 5-min bars on a historical date. EMA9 < EMA21, both slopes down."""
    bars = []
    for i in range(n):
        c = round(base - i * step, 4)
        o = round(c + 0.02, 4)
        ts = _BASE + timedelta(minutes=i * 5)
        bars.append(make_bar(c, open_=o, ts=ts))
    return bars


def _flat_5m(price=150.0, n=35):
    """Flat doji bars. EMA9 == EMA21, slope == 0 — neither CALL nor PUT conditions hold."""
    return [make_bar(price, open_=price, ts=_BASE + timedelta(minutes=i * 5)) for i in range(n)]


def _today_rising_5m(base=150.0, n=35, step=0.05):
    """
    Rising 5-min bars timestamped TODAY at 9:00–11:55 AM.
    _today_bars() picks these up so VWAP is computed.
    For rising bars: final close > average close → close > VWAP → CALL VWAP gate passes.
    Timestamps are pre-market morning hours so they look "completed" to ET-based checks.
    """
    today = _date.today()
    bars = []
    for i in range(n):
        c = round(base + i * step, 4)
        o = round(c - 0.02, 4)
        ts = datetime(today.year, today.month, today.day, 9, 0) + timedelta(minutes=i * 5)
        bars.append(make_bar(c, open_=o, ts=ts))
    return bars


def _today_falling_5m(base=152.0, n=35, step=0.05):
    """
    Falling 5-min bars timestamped today at 9:00 AM.
    Final close < average close → close < VWAP → PUT VWAP gate passes.
    """
    today = _date.today()
    bars = []
    for i in range(n):
        c = round(base - i * step, 4)
        o = round(c + 0.02, 4)
        ts = datetime(today.year, today.month, today.day, 9, 0) + timedelta(minutes=i * 5)
        bars.append(make_bar(c, open_=o, ts=ts))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# check_5min_trend_filter
# ─────────────────────────────────────────────────────────────────────────────

class TestCheck5minTrendFilter:
    """
    Most tests use historical (_BASE) timestamps so VWAP is bypassed (vwap==0 → skip).
    Two dedicated tests use today's timestamps to verify the VWAP gate is wired up.
    """

    def test_call_passes_on_rising_trend(self):
        """Rising bars → EMA9 > EMA21, both slopes up → CALL passes."""
        assert check_5min_trend_filter(_rising_5m(), "CALL", ticker="R1") is True

    def test_put_blocked_on_rising_trend(self):
        """Rising trend does not satisfy PUT conditions."""
        assert check_5min_trend_filter(_rising_5m(), "PUT", ticker="R2") is False

    def test_put_passes_on_falling_trend(self):
        """Falling bars → EMA9 < EMA21, both slopes down → PUT passes."""
        assert check_5min_trend_filter(_falling_5m(), "PUT", ticker="F1") is True

    def test_call_blocked_on_falling_trend(self):
        """Falling trend does not satisfy CALL conditions."""
        assert check_5min_trend_filter(_falling_5m(), "CALL", ticker="F2") is False

    def test_flat_trend_blocks_both_directions(self):
        """
        Flat bars: EMA9 == EMA21, slope == 0.
        Neither EMA9 > EMA21 (CALL) nor EMA9 < EMA21 (PUT) holds → both blocked.
        """
        bars = _flat_5m()
        assert check_5min_trend_filter(bars, "CALL", ticker="FL") is False
        assert check_5min_trend_filter(bars, "PUT",  ticker="FL") is False

    def test_insufficient_bars_returns_true(self):
        """
        Fewer than ema_slow + 2 = 23 bars → not enough history.
        Falls through to True (don't block on data scarcity).
        """
        bars = _rising_5m(n=10)
        assert check_5min_trend_filter(bars, "CALL", ticker="FEW") is True
        assert check_5min_trend_filter(bars, "PUT",  ticker="FEW") is True

    def test_empty_bars_returns_true(self):
        assert check_5min_trend_filter([], "CALL", ticker="EMPTY") is True

    def test_result_cached_same_last_bar_time(self):
        """Second call with identical bars returns cached value and populates cache."""
        bars = _rising_5m()
        r1 = check_5min_trend_filter(bars, "CALL", ticker="CACHE")
        r2 = check_5min_trend_filter(bars, "CALL", ticker="CACHE")
        assert r1 == r2 is True
        assert "CACHE" in _trend_cache

    def test_cache_stores_both_directions(self):
        """After a call, cache stores both CALL and PUT values for the ticker."""
        bars = _rising_5m()
        check_5min_trend_filter(bars, "CALL", ticker="BOTH")
        entry = _trend_cache.get("BOTH", {})
        assert "CALL" in entry and "PUT" in entry

    def test_cache_isolated_per_ticker(self):
        """Different tickers have independent cache entries with independent results."""
        rising  = _rising_5m()
        falling = _falling_5m()
        r = check_5min_trend_filter(rising,  "CALL", ticker="AAPL")
        f = check_5min_trend_filter(falling, "CALL", ticker="TSLA")
        assert r is True
        assert f is False
        # Both tickers have their own cache entries
        assert "AAPL" in _trend_cache and "TSLA" in _trend_cache
        # AAPL's CALL is True (rising), TSLA's CALL is False (falling)
        assert _trend_cache["AAPL"]["CALL"] is True
        assert _trend_cache["TSLA"]["CALL"] is False

    def test_cache_refreshes_on_new_last_bar_time(self):
        """
        When the last completed bar's timestamp changes (a new candle closed),
        the cache is invalidated and the result is recomputed.
        """
        bars_v1 = _rising_5m(base=150.0, n=35)   # last bar at _BASE + 34*5min
        bars_v2 = _rising_5m(base=150.0, n=36)   # one more bar → different last_bar_time

        r1 = check_5min_trend_filter(bars_v1, "CALL", ticker="TICK")
        r2 = check_5min_trend_filter(bars_v2, "CALL", ticker="TICK")

        # Both are rising bars so both pass CALL
        assert r1 is True
        assert r2 is True
        # Cache was updated with the newer timestamp
        assert _trend_cache["TICK"]["last_bar_time"] == bars_v2[-1].time

    def test_vwap_gate_call_today_bars(self):
        """
        Today's rising bars: VWAP = avg(close), final close > avg → CALL VWAP passes.
        Entire 3-step filter (EMA + VWAP + slope) passes for CALL.
        """
        bars = _today_rising_5m()
        assert check_5min_trend_filter(bars, "CALL", ticker="TVWAP_C") is True

    def test_vwap_gate_put_today_bars(self):
        """
        Today's falling bars: final close < VWAP → PUT VWAP passes.
        """
        bars = _today_falling_5m()
        assert check_5min_trend_filter(bars, "PUT", ticker="TVWAP_P") is True

    def test_vwap_blocks_call_when_close_below_vwap(self):
        """
        Today's falling bars: final close < VWAP.
        CALL requires close > VWAP — should fail because of VWAP even if slope/EMA happened to allow.
        (With falling bars EMA slope is also wrong for CALL, so this is doubly blocked.)
        """
        bars = _today_falling_5m()
        assert check_5min_trend_filter(bars, "CALL", ticker="TVWAP_BLOCK") is False


# ─────────────────────────────────────────────────────────────────────────────
# get_5min_ema9
# ─────────────────────────────────────────────────────────────────────────────

class TestGet5minEma9:

    def test_returns_float_on_sufficient_bars(self):
        """35 bars is enough for EMA9 — returns a float within the price range."""
        bars = _rising_5m(base=150.0, n=35)
        result = get_5min_ema9(bars)
        assert result is not None
        assert 149.0 < result < 153.0

    def test_returns_none_on_empty_bars(self):
        assert get_5min_ema9([]) is None

    def test_returns_none_below_ema_period(self):
        """Fewer than 9 bars → can't compute EMA9 → None."""
        bars = _rising_5m(n=5)
        assert get_5min_ema9(bars) is None

    def test_ema9_tracks_price_direction(self):
        """EMA9 of rising bars > EMA9 of falling bars starting from the same base."""
        r_ema = get_5min_ema9(_rising_5m(base=150.0, n=35))
        f_ema = get_5min_ema9(_falling_5m(base=152.0, n=35))
        assert r_ema is not None and f_ema is not None
        assert r_ema > f_ema

    def test_ema9_is_within_bar_range(self):
        """EMA9 must be between the first and last close of the rising series."""
        bars = _rising_5m(base=150.0, n=35, step=0.05)
        ema = get_5min_ema9(bars)
        assert ema is not None
        first_close = bars[0].close
        last_close  = bars[-1].close
        assert first_close < ema <= last_close


# ─────────────────────────────────────────────────────────────────────────────
# check_1min_pullback  (checks bars[-2], the pullback bar)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheck1minPullback:
    """
    After the fix in a prior session:
      check_1min_pullback checks bars[-2]  (NOT bars[-1]).
      bars[-1] is the confirmation bar, evaluated separately.

    Each test builds a list ending with [pullback_bar, confirm_bar].
    pullback_bar = bars[-2]
    confirm_bar  = bars[-1]  (content irrelevant for this function)
    """

    # ── CALL ──────────────────────────────────────────────────────────────────

    def test_call_pullback_low_at_ema9(self):
        """CALL: pullback bar's low exactly touches EMA9 → confirmed."""
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.5, open_=150.3, high=150.5, low=150.0),
            confirm_kwargs =dict(close=150.8, open_=150.5),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is True

    def test_call_pullback_low_below_ema9(self):
        """CALL: pullback bar's low dips below EMA9 → confirmed."""
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.4, open_=150.2, high=150.4, low=149.8),
            confirm_kwargs =dict(close=150.9, open_=150.5),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is True

    def test_call_pullback_close_within_threshold(self):
        """CALL: close within 0.10% of EMA9 (even when low doesn't touch) → confirmed."""
        ema9 = 150.0
        close_near = round(ema9 * 1.0009, 4)   # 0.09% above — within 0.10% threshold
        bars = _1m_sequence(
            pullback_kwargs=dict(close=close_near, open_=close_near - 0.1, low=close_near - 0.05),
            confirm_kwargs =dict(close=close_near + 0.3, open_=close_near + 0.1),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is True

    def test_call_pullback_fails_when_low_above_ema9_and_close_far(self):
        """
        CALL: pullback bar's low is 0.5%+ above EMA9 and close is also far above → no pullback.
        0.5% gap is well outside the 0.10% threshold.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=151.0, open_=150.8, high=151.0, low=150.8),
            confirm_kwargs =dict(close=151.5, open_=151.0),
        )
        # pullback_bar.low = 150.8 > ema9 = 150.0 (gap = 0.53%)
        # abs(151.0 - 150.0) = 1.0 > threshold = 0.15  → False
        assert check_1min_pullback(bars, "CALL", ema9) is False

    def test_call_pullback_checks_second_to_last_bar_not_last(self):
        """
        Regression guard: bars[-2] must be checked, NOT bars[-1].
        Pullback bar (bars[-2]) is well above EMA9.
        Confirm bar (bars[-1]) has low below EMA9 — but it should NOT be evaluated here.
        Result must be False.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            # bars[-2]: low = 150.8 (above EMA9 — no pullback)
            pullback_kwargs=dict(close=151.0, open_=150.8, high=151.0, low=150.8),
            # bars[-1]: low = 149.0 (below EMA9 — irrelevant, should NOT be checked)
            confirm_kwargs =dict(close=149.5, open_=150.0, high=150.0, low=149.0),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is False

    # ── PUT ───────────────────────────────────────────────────────────────────

    def test_put_pullback_high_at_ema9(self):
        """PUT: pullback bar's high exactly touches EMA9 → confirmed."""
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.5, open_=149.7, high=150.0, low=149.5),
            confirm_kwargs =dict(close=149.2, open_=149.6),
        )
        assert check_1min_pullback(bars, "PUT", ema9) is True

    def test_put_pullback_high_above_ema9(self):
        """PUT: pullback bar's high rises above EMA9 → confirmed."""
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.5, open_=149.8, high=150.2, low=149.5),
            confirm_kwargs =dict(close=149.1, open_=149.5),
        )
        assert check_1min_pullback(bars, "PUT", ema9) is True

    def test_put_pullback_close_within_threshold(self):
        """PUT: close within 0.10% of EMA9 (even when high doesn't touch) → confirmed."""
        ema9 = 150.0
        close_near = round(ema9 * 0.9991, 4)    # 0.09% below — within threshold
        bars = _1m_sequence(
            pullback_kwargs=dict(close=close_near, open_=close_near + 0.05, high=close_near + 0.05),
            confirm_kwargs =dict(close=close_near - 0.3, open_=close_near - 0.1),
        )
        assert check_1min_pullback(bars, "PUT", ema9) is True

    def test_put_pullback_fails_when_high_below_ema9_and_close_far(self):
        """
        PUT: pullback bar's high is 0.5%+ below EMA9 and close is also far below → no pullback.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.0, open_=149.2, high=149.2, low=149.0),
            confirm_kwargs =dict(close=148.5, open_=149.0),
        )
        # pullback_bar.high = 149.2 < ema9 = 150.0 (gap = 0.53%)
        # abs(149.0 - 150.0) = 1.0 > threshold = 0.15  → False
        assert check_1min_pullback(bars, "PUT", ema9) is False

    def test_put_pullback_checks_second_to_last_bar_not_last(self):
        """
        Regression: bars[-1] high above EMA9 should not trigger a PUT pullback.
        Only bars[-2] is evaluated.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            # bars[-2]: high = 149.2 (below EMA9 — no pullback)
            pullback_kwargs=dict(close=149.0, open_=149.2, high=149.2, low=148.8),
            # bars[-1]: high = 150.5 (above EMA9 — irrelevant)
            confirm_kwargs =dict(close=150.3, open_=149.5, high=150.5, low=149.5),
        )
        assert check_1min_pullback(bars, "PUT", ema9) is False

    # ── Edge cases ─────────────────────────────────────────────────────────────

    def test_fewer_than_two_bars_returns_false(self):
        """Fewer than 2 bars → can't evaluate bars[-2] → False."""
        assert check_1min_pullback([_bar(149.5)], "CALL", 150.0) is False
        assert check_1min_pullback([],            "CALL", 150.0) is False

    def test_zero_ema9_returns_false(self):
        """ema9_5m = 0 is invalid — function returns False immediately."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.0),
            confirm_kwargs =dict(close=150.5),
        )
        assert check_1min_pullback(bars, "CALL", 0.0) is False
        assert check_1min_pullback(bars, "PUT",  0.0) is False

    def test_negative_ema9_returns_false(self):
        """Negative ema9_5m is also invalid."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.0),
            confirm_kwargs =dict(close=150.5),
        )
        assert check_1min_pullback(bars, "CALL", -1.0) is False


# ─────────────────────────────────────────────────────────────────────────────
# check_1min_confirmation  (checks bars[-1] vs bars[-2])
# ─────────────────────────────────────────────────────────────────────────────

class TestCheck1minConfirmation:
    """
    bars[-2] = pullback bar  (its high / low is the range to break)
    bars[-1] = confirmation bar (must close through bars[-2]'s high or low)
    """

    # ── CALL ──────────────────────────────────────────────────────────────────

    def test_call_confirmed_bullish_and_breaks_prior_high(self):
        """CALL: bars[-1] bullish AND close > bars[-2].high → confirmed."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.2, open_=150.0, high=150.3, low=149.9),
            confirm_kwargs =dict(close=150.5, open_=150.1, high=150.5, low=150.0),
        )
        # cfm: close=150.5 > pb.high=150.3, close > open → bullish → True
        assert check_1min_confirmation(bars, "CALL") is True

    def test_call_fails_bearish_candle(self):
        """CALL: confirm bar closes below open (bearish) → not confirmed."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.2, open_=150.0, high=150.3, low=149.9),
            confirm_kwargs =dict(close=150.0, open_=150.4, high=150.4, low=149.9),
        )
        # cfm: close=150.0 < open=150.4 (bearish) → False
        assert check_1min_confirmation(bars, "CALL") is False

    def test_call_fails_doji(self):
        """CALL: confirm bar is a doji (close == open) → not bullish → not confirmed."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.2, open_=150.0, high=150.3, low=149.9),
            confirm_kwargs =dict(close=150.5, open_=150.5, high=150.5, low=150.5),
        )
        # close == open → doji, not bullish → False
        assert check_1min_confirmation(bars, "CALL") is False

    def test_call_fails_bullish_but_no_range_break(self):
        """CALL: confirm bar is bullish but close ≤ bars[-2].high → not a break → False."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.2, open_=150.0, high=150.5, low=149.9),
            confirm_kwargs =dict(close=150.3, open_=150.1, high=150.3, low=150.0),
        )
        # cfm.close=150.3 < pb.high=150.5 → no range break → False
        assert check_1min_confirmation(bars, "CALL") is False

    # ── PUT ───────────────────────────────────────────────────────────────────

    def test_put_confirmed_bearish_and_breaks_prior_low(self):
        """PUT: bars[-1] bearish AND close < bars[-2].low → confirmed."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.8, open_=150.0, high=150.0, low=149.7),
            confirm_kwargs =dict(close=149.5, open_=149.9, high=149.9, low=149.4),
        )
        # cfm: close=149.5 < pb.low=149.7, close < open → bearish → True
        assert check_1min_confirmation(bars, "PUT") is True

    def test_put_fails_bullish_candle(self):
        """PUT: confirm bar closes above open → not confirmed."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.8, open_=150.0, high=150.0, low=149.7),
            confirm_kwargs =dict(close=150.1, open_=149.8, high=150.1, low=149.8),
        )
        # cfm: close=150.1 > open=149.8 (bullish) → False
        assert check_1min_confirmation(bars, "PUT") is False

    def test_put_fails_bearish_but_no_range_break(self):
        """PUT: confirm bar is bearish but close ≥ bars[-2].low → no break → False."""
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.8, open_=150.0, high=150.0, low=149.5),
            confirm_kwargs =dict(close=149.6, open_=149.9, high=149.9, low=149.5),
        )
        # cfm.close=149.6 > pb.low=149.5 → no range break → False
        assert check_1min_confirmation(bars, "PUT") is False

    # ── Edge cases ─────────────────────────────────────────────────────────────

    def test_fewer_than_two_bars_returns_false(self):
        """Fewer than 2 bars → False."""
        assert check_1min_confirmation([_bar(150.0)], "CALL") is False
        assert check_1min_confirmation([],            "PUT")  is False

    # ── Integration: two-bar sequence ────────────────────────────────────────

    def test_full_call_sequence_both_functions_pass(self):
        """
        Complete two-bar CALL entry:
          pullback bar (bars[-2]): low touches EMA9
          confirm bar  (bars[-1]): bullish, close > pb.high
        Both check_1min_pullback and check_1min_confirmation pass on the same bar list.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.1, open_=150.3, high=150.3, low=149.95),
            confirm_kwargs =dict(close=150.5, open_=150.1, high=150.5, low=150.0),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is True
        assert check_1min_confirmation(bars, "CALL")   is True

    def test_full_put_sequence_both_functions_pass(self):
        """
        Complete two-bar PUT entry:
          pullback bar (bars[-2]): high touches EMA9
          confirm bar  (bars[-1]): bearish, close < pb.low
        """
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=149.9, open_=149.7, high=150.05, low=149.7),
            confirm_kwargs =dict(close=149.5, open_=149.9, high=149.9,  low=149.4),
        )
        assert check_1min_pullback(bars, "PUT", ema9) is True
        assert check_1min_confirmation(bars, "PUT")   is True

    def test_pullback_yes_but_confirm_no(self):
        """Pullback bar touches EMA9 but confirm candle is bearish for a CALL → pullback=True, confirm=False."""
        ema9 = 150.0
        bars = _1m_sequence(
            pullback_kwargs=dict(close=150.1, open_=150.3, high=150.3, low=149.95),
            confirm_kwargs =dict(close=150.0, open_=150.4, high=150.4, low=149.9),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is True
        assert check_1min_confirmation(bars, "CALL")   is False

    def test_confirm_yes_but_pullback_no(self):
        """
        Confirm bar looks good but pullback bar didn't touch EMA9.
        check_1min_pullback = False (pb entirely above EMA9).
        check_1min_confirmation = True (cfm breaks pb.high while bullish).
        Both must pass for a valid entry — one failure blocks the trade.
        """
        ema9 = 150.0
        bars = _1m_sequence(
            # pullback bar well above EMA9 (no touch)
            pullback_kwargs=dict(close=151.0, open_=150.8, high=151.0, low=150.8),
            # confirm bar bullish and breaks pb.high
            confirm_kwargs =dict(close=151.4, open_=151.0, high=151.4, low=151.0),
        )
        assert check_1min_pullback(bars, "CALL", ema9) is False
        assert check_1min_confirmation(bars, "CALL")   is True


# ─────────────────────────────────────────────────────────────────────────────
# check_option_spread
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckOptionSpread:
    """
    Spread = (ask - bid) / mid.  Blocked when spread > max_spread_pct.
    Avoid testing "exactly at limit" due to floating-point rounding.
    """

    def test_narrow_spread_passes_10pct_threshold(self):
        """4% spread comfortably below 10% → allowed."""
        # bid=1.96, ask=2.04, mid=2.00, spread=(0.08)/2.00=4%
        assert check_option_spread(bid=1.96, ask=2.04, max_spread_pct=0.10) is True

    def test_wide_spread_blocked_at_10pct(self):
        """50% spread blocked at 10% threshold."""
        # bid=1.50, ask=2.50, mid=2.00, spread=1.00/2.00=50%
        assert check_option_spread(bid=1.50, ask=2.50, max_spread_pct=0.10) is False

    def test_spread_below_threshold_passes(self):
        """Spread clearly below the given threshold → allowed."""
        # bid=1.94, ask=2.06, mid=2.00, spread=0.12/2.00=6% < 10%
        assert check_option_spread(bid=1.94, ask=2.06, max_spread_pct=0.10) is True

    def test_spread_above_threshold_blocked(self):
        """Spread clearly above the given threshold → blocked."""
        # bid=1.50, ask=2.50, mid=2.00, spread=50% > 20%
        assert check_option_spread(bid=1.50, ask=2.50, max_spread_pct=0.20) is False

    def test_zero_bid_passes_through(self):
        """bid=0 is an invalid quote → pass-through True."""
        assert check_option_spread(bid=0, ask=2.00, max_spread_pct=0.10) is True

    def test_zero_ask_passes_through(self):
        """ask=0 is invalid → pass-through True."""
        assert check_option_spread(bid=1.90, ask=0, max_spread_pct=0.10) is True

    def test_default_threshold_is_10pct(self):
        """Default max_spread_pct=0.10: 2% spread passes, 50% fails."""
        assert check_option_spread(bid=1.98, ask=2.02) is True    # 2% → pass
        assert check_option_spread(bid=1.50, ask=2.50) is False   # 50% → blocked

    def test_custom_tight_threshold(self):
        """With max_spread_pct=0.05: 4% spread passes but 8% spread fails."""
        # 4%: bid=1.96, ask=2.04 → spread=4% < 5%
        assert check_option_spread(bid=1.96, ask=2.04, max_spread_pct=0.05) is True
        # 8%: bid=1.92, ask=2.08 → spread=8% > 5%
        assert check_option_spread(bid=1.92, ask=2.08, max_spread_pct=0.05) is False

    def test_higher_price_option(self):
        """Works correctly for options priced around $10."""
        # bid=9.60, ask=10.40, mid=10.00, spread=0.80/10.00=8% < 10% → pass
        assert check_option_spread(bid=9.60, ask=10.40, max_spread_pct=0.10) is True
        # bid=8.00, ask=12.00, mid=10.00, spread=4.00/10.00=40% > 10% → blocked
        assert check_option_spread(bid=8.00, ask=12.00, max_spread_pct=0.10) is False


# ─────────────────────────────────────────────────────────────────────────────
# check_volume_filter
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckVolumeFilter:
    """
    check_volume_filter does NOT call completed_bars — it reads bars_1m directly.
    avg_vol = mean of bars_1m[-(lookback+1):-1]
    current = bars_1m[-1].volume
    """

    def _bars(self, volumes):
        return [make_bar(150.0, volume=v) for v in volumes]

    def test_volume_above_average_passes(self):
        """Current bar volume above lookback avg → allowed."""
        bars = self._bars([100_000] * 20 + [200_000])   # avg=100k, current=200k
        assert check_volume_filter(bars, lookback=20) is True

    def test_volume_exactly_at_average_passes(self):
        """Volume exactly equal to avg → allowed (>=)."""
        bars = self._bars([100_000] * 21)
        assert check_volume_filter(bars, lookback=20) is True

    def test_volume_below_average_blocked(self):
        """Current bar volume below avg → blocked."""
        bars = self._bars([500_000] * 20 + [10_000])    # avg=500k, current=10k
        assert check_volume_filter(bars, lookback=20) is False

    def test_insufficient_history_passes_through(self):
        """Fewer than lookback+1 bars → pass-through True (data scarcity early in session)."""
        bars = self._bars([100_000] * 10)               # only 10, lookback=20
        assert check_volume_filter(bars, lookback=20) is True

    def test_empty_bars_passes_through(self):
        """Empty list → fewer than lookback+1 → True."""
        assert check_volume_filter([], lookback=20) is True

    def test_exactly_enough_bars_activates_filter(self):
        """Exactly lookback+1 bars: filter becomes active — low volume is blocked."""
        bars = self._bars([100_000] * 20 + [10_000])   # avg=100k, current=10k
        assert check_volume_filter(bars, lookback=20) is False

    def test_custom_lookback_5(self):
        """Custom lookback=5: avg of prev 5, current = 6th bar."""
        # [200k × 5] avg=200k; current=100k < avg → blocked
        bars = self._bars([200_000, 200_000, 200_000, 200_000, 200_000, 100_000])
        assert check_volume_filter(bars, lookback=5) is False

    def test_spike_in_history_raises_average(self):
        """
        One spike in lookback raises avg: 19×100k + 1×500k → avg=120k.
        Current bar at 130k > 120k → passes.
        """
        lookback_vols = [100_000] * 19 + [500_000]     # avg = (1900k + 500k)/20 = 120k
        bars = self._bars(lookback_vols + [130_000])
        assert check_volume_filter(bars, lookback=20) is True

    def test_extra_bars_beyond_lookback_ignored(self):
        """
        bars_1m[-(lookback+1):-1] uses only the last lookback bars for avg.
        Older bars at the front don't affect the result.
        """
        # 40 bars of 1M volume, then 20 bars of 100k, then 1 bar of 150k
        # avg over last 20 of those 100k bars = 100k; current=150k > 100k → passes
        old_bars = [1_000_000] * 40
        recent   = [100_000]  * 20
        current  = [150_000]
        bars = self._bars(old_bars + recent + current)
        assert check_volume_filter(bars, lookback=20) is True


# ─────────────────────────────────────────────────────────────────────────────
# check_s2_exit_conditions
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckS2ExitConditions:
    """
    Exit priority order: 1. Hard stop  2. Trail / breakeven  3. EMA cross
    Bars must have enough history for EMA cross detection (≥ ema_slow+2 = 23).
    All bars use _BASE timestamps so they're treated as completed.
    """

    def _call_state(self, entry=5.00, current=5.00, stop=3.75, be_stop_set=False,
                    bars=None):
        return dict(
            bars=bars or _rising_5m(n=25),
            direction="CALL",
            entry_price=entry,
            current_price=current,
            stop_price=stop,
            be_stop_set=be_stop_set,
        )

    # ── Hard stop ─────────────────────────────────────────────────────────────

    def test_hard_stop_fires_at_stop_price(self):
        """current_price == stop_price → STOP (or TRAILING_STOP) exit, close_all=True."""
        result = check_s2_exit_conditions(**self._call_state(
            entry=5.00, current=3.75, stop=3.75,
        ))
        assert result is not None
        assert result.close_all is True
        assert result.reason in ("STOP", "TRAILING_STOP")

    def test_hard_stop_fires_below_stop_price(self):
        """current_price < stop_price → exit."""
        result = check_s2_exit_conditions(**self._call_state(
            entry=5.00, current=3.50, stop=3.75,
        ))
        assert result is not None
        assert result.close_all is True

    def test_hard_stop_reason_is_stop_when_stop_is_original(self):
        """
        stop_price == original entry × (1 - stop_loss_pct) → reason = 'STOP' (not trailing).
        """
        from app.config import settings
        entry = 5.00
        original_stop = round(entry * (1.0 - settings.s2_stop_loss_pct), 2)
        result = check_s2_exit_conditions(**self._call_state(
            entry=entry, current=original_stop, stop=original_stop,
        ))
        assert result is not None
        assert result.reason == "STOP"

    def test_hard_stop_reason_is_trailing_stop_when_stop_raised(self):
        """
        stop_price > entry × (1 - stop_loss_pct) → stop was raised by trailing → 'TRAILING_STOP'.
        """
        entry = 5.00
        raised_stop = entry   # stop moved to breakeven
        current_at_stop = entry - 0.01
        result = check_s2_exit_conditions(**self._call_state(
            entry=entry, current=current_at_stop, stop=raised_stop, be_stop_set=True,
        ))
        assert result is not None
        assert result.reason == "TRAILING_STOP"

    # ── Trailing stop cascade ─────────────────────────────────────────────────

    def test_no_exit_price_above_stop_no_milestone(self):
        """Price above stop, no gain milestone → no exit, returns None."""
        result = check_s2_exit_conditions(**self._call_state(
            entry=5.00, current=5.10, stop=3.75,
        ))
        assert result is None

    def test_breakeven_stop_raised_at_threshold(self):
        """
        At s2_breakeven_pct gain (not yet be_stop_set): expect close_all=False and
        new_stop == entry_price.
        """
        from app.config import settings
        entry = 5.00
        be_pct = settings.s2_breakeven_pct         # 0.10
        current = round(entry * (1 + be_pct + 0.02), 2)   # +12%, safely above be threshold

        # Use flat bars to avoid EMA cross signal
        bars = _flat_5m(price=150.0, n=25)
        result = check_s2_exit_conditions(
            bars=bars, direction="CALL",
            entry_price=entry, current_price=current,
            stop_price=round(entry * 0.88, 2),
            be_stop_set=False,
        )
        # Breakeven fires (unless trail threshold also crossed and takes priority)
        if result is not None and not result.close_all:
            assert result.new_stop == round(entry, 2)

    def test_trail_stop_raised_above_current_stop(self):
        """
        At s2_trail_pct gain: close_all=False, new_stop > current stop_price.
        """
        from app.config import settings
        entry = 5.00
        trail_pct = settings.s2_trail_pct          # 0.20
        current = round(entry * (1 + trail_pct + 0.05), 2)   # +25%, above trail threshold
        stop = round(entry * (1 - settings.s2_stop_loss_pct), 2)

        bars = _flat_5m(price=150.0, n=25)
        result = check_s2_exit_conditions(
            bars=bars, direction="CALL",
            entry_price=entry, current_price=current,
            stop_price=stop, be_stop_set=True,
        )
        if result is not None and not result.close_all:
            assert result.new_stop is not None
            assert result.new_stop > stop

    # ── PUT position ──────────────────────────────────────────────────────────

    def test_put_no_exit_above_stop(self):
        """PUT with current_price > stop_price → no exit."""
        result = check_s2_exit_conditions(
            bars=_falling_5m(n=25),
            direction="PUT",
            entry_price=5.00,
            current_price=5.20,
            stop_price=3.75,
            be_stop_set=False,
        )
        assert result is None

    def test_put_stop_fires(self):
        """PUT: current_price <= stop_price → stop fires."""
        result = check_s2_exit_conditions(
            bars=_falling_5m(n=25),
            direction="PUT",
            entry_price=5.00,
            current_price=3.00,
            stop_price=3.75,
            be_stop_set=False,
        )
        assert result is not None
        assert result.close_all is True
