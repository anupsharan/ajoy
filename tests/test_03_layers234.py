"""
Tests for Layers 2, 3, 4:
  L2 — check_bounce_confirmation()
  L3 — check_momentum_candle()
  L4 — check_vwap_slope()
"""
import pytest
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import (
    check_bounce_confirmation,
    check_momentum_candle,
    check_vwap_slope,
    calculate_vwap,
)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — check_bounce_confirmation
# ─────────────────────────────────────────────────────────────────────────────

class TestLayer2:
    VWAP = 150.0

    def _bars_above(self, n=10):
        """Last bar is in-progress; all prior close above VWAP."""
        return [make_bar(self.VWAP + 0.10) for _ in range(n + 1)]

    def _bars_below(self, n=10):
        return [make_bar(self.VWAP - 0.10) for _ in range(n + 1)]

    def test_call_confirmed_above_vwap(self):
        assert check_bounce_confirmation(self._bars_above(), "CALL", self.VWAP) is True

    def test_put_confirmed_below_vwap(self):
        assert check_bounce_confirmation(self._bars_below(), "PUT", self.VWAP) is True

    def test_call_blocked_when_bar_below_vwap(self):
        bars = self._bars_above(n=10)
        # Inject a bar below VWAP in the confirmation window (second-to-last)
        bars[-2] = make_bar(self.VWAP - 0.05)
        assert check_bounce_confirmation(bars, "CALL", self.VWAP) is False

    def test_put_blocked_when_bar_above_vwap(self):
        bars = self._bars_below(n=10)
        bars[-2] = make_bar(self.VWAP + 0.05)
        assert check_bounce_confirmation(bars, "PUT", self.VWAP) is False

    def test_not_enough_bars_returns_false(self):
        # Only 2 bars total — need n+1 = 3 minimum
        assert check_bounce_confirmation([make_bar(151.0), make_bar(151.0)], "CALL", self.VWAP) is False

    def test_zero_vwap_returns_false(self):
        assert check_bounce_confirmation(self._bars_above(), "CALL", 0.0) is False

    def test_call_exactly_at_vwap_fails(self):
        # close == vwap (not strictly above) should fail
        bars = [make_bar(self.VWAP) for _ in range(5)]
        assert check_bounce_confirmation(bars, "CALL", self.VWAP) is False

    def test_custom_n_respected(self):
        # With n=1 only needs 1 confirmed bar
        bars = [make_bar(151.0)] * 3
        assert check_bounce_confirmation(bars, "CALL", self.VWAP, n=1) is True


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — check_momentum_candle
# ─────────────────────────────────────────────────────────────────────────────

class TestLayer3:

    def test_call_green_rising(self):
        """Green candle that closed higher than prior bar → CALL confirmed."""
        bars = (
            [make_bar(149.80)] * 5          # background bars (bars[-4]=149.80)
            + [make_bar(149.90, open_=149.80)]  # bars[-3]: rising vs bars[-4]
            + [make_bar(150.10, open_=149.95)]  # bars[-2]: last completed — rising again
            + [make_bar(150.15)]            # bars[-1]: in-progress (ignored)
        )
        assert check_momentum_candle(bars, "CALL") is True

    def test_call_doji_blocked(self):
        """Doji (open == close) — no momentum → CALL blocked."""
        bars = (
            [make_bar(150.0)] * 5
            + [make_bar(150.0, open_=150.0)]   # bars[-3]
            + [make_bar(150.0, open_=150.0)]   # bars[-2]: doji
            + [make_bar(150.0)]                # in-progress
        )
        assert check_momentum_candle(bars, "CALL") is False

    def test_call_red_bar_blocked(self):
        """Red candle (close < open) when expecting CALL → blocked."""
        bars = (
            [make_bar(150.0)] * 5
            + [make_bar(150.10, open_=150.05)]
            + [make_bar(149.90, open_=150.05)]  # RED candle
            + [make_bar(150.00)]
        )
        assert check_momentum_candle(bars, "CALL") is False

    def test_call_green_but_falling_close_blocked(self):
        """Green candle but close < prev close (spike then retreat) → blocked."""
        bars = (
            [make_bar(150.0)] * 5
            + [make_bar(150.50, open_=150.40)]   # bars[-3]: higher
            + [make_bar(150.30, open_=150.20)]   # bars[-2]: green but lower close
            + [make_bar(150.35)]
        )
        assert check_momentum_candle(bars, "CALL") is False

    def test_put_red_falling(self):
        """Red candle falling → PUT confirmed."""
        bars = (
            [make_bar(150.20)] * 5               # background bars (bars[-4]=150.20)
            + [make_bar(150.10, open_=150.20)]   # bars[-3]: falling vs bars[-4]
            + [make_bar(149.90, open_=150.05)]   # bars[-2]: falling again
            + [make_bar(149.80)]
        )
        assert check_momentum_candle(bars, "PUT") is True

    def test_put_green_bar_blocked(self):
        bars = (
            [make_bar(150.0)] * 5
            + [make_bar(149.90)]
            + [make_bar(150.10, open_=149.95)]   # green candle
            + [make_bar(150.20)]
        )
        assert check_momentum_candle(bars, "PUT") is False

    def test_too_few_bars_returns_false(self):
        assert check_momentum_candle([make_bar(150.0), make_bar(151.0)], "CALL") is False


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — check_vwap_slope
# ─────────────────────────────────────────────────────────────────────────────

class TestLayer4:

    def test_rising_vwap_allows_call(self):
        """Rising intraday VWAP → slope positive → CALL allowed."""
        bars = rising_bars(base=100.0, n=40, step=0.10)
        assert check_vwap_slope(bars, "CALL") is True

    def test_rising_vwap_blocks_put(self):
        """Rising VWAP contradicts a PUT → blocked."""
        bars = rising_bars(base=100.0, n=40, step=0.10)
        assert check_vwap_slope(bars, "PUT") is False

    def test_falling_vwap_blocks_call(self):
        """Falling VWAP contradicts a CALL → blocked."""
        bars = falling_bars(base=200.0, n=40, step=0.10)
        assert check_vwap_slope(bars, "CALL") is False

    def test_falling_vwap_allows_put(self):
        """Falling VWAP supports PUT → allowed."""
        bars = falling_bars(base=200.0, n=40, step=0.10)
        assert check_vwap_slope(bars, "PUT") is True

    def test_flat_vwap_allows_both(self):
        """Flat prices → tiny slope below threshold → both directions allowed."""
        bars = flat_bars(price=150.0, n=40)
        assert check_vwap_slope(bars, "CALL") is True
        assert check_vwap_slope(bars, "PUT")  is True

    def test_not_enough_bars_passes_through(self):
        """Too few bars (early in session) → don't block."""
        bars = rising_bars(n=10)   # less than lookback+5=25
        assert check_vwap_slope(bars, "CALL") is True
        assert check_vwap_slope(bars, "PUT")  is True

    def test_custom_threshold(self):
        """With very tight threshold (0.001%), even tiny rise blocks PUT."""
        bars = rising_bars(base=100.0, n=40, step=0.05)
        assert check_vwap_slope(bars, "PUT", threshold_pct=0.001) is False

    def test_custom_lookback(self):
        """With lookback=5, compare only last 5 bars' VWAP."""
        bars = falling_bars(base=200.0, n=40, step=0.10)
        # Falling → CALL should be blocked even with short lookback
        assert check_vwap_slope(bars, "CALL", lookback=5) is False
