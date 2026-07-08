"""
Tests for is_trend_reversal_confirmed() — consecutive-bar TREND_REVERSAL gate.

Guard B: N consecutive 15-min bars on the wrong EMA side before firing.

Note: TREND_REVERSAL used to be part of check_exit_conditions() but was
moved to the scheduler (managed per-trade loop) so check_exit_conditions()
stays stateless and bar-free.  These tests exercise is_trend_reversal_confirmed()
directly, which is what the scheduler calls.
"""
import pytest
from unittest.mock import patch
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import is_trend_reversal_confirmed
from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bars_with_reversal(n_reversed: int, direction: str, period: int = 21):
    """
    Build bars_15m with a clear established trend followed by exactly
    `n_reversed` bars that have crossed to the wrong EMA side.

    direction = "CALL" → established bullish trend, then n_reversed bearish bars
    direction = "PUT"  → established bearish trend, then n_reversed bullish bars
    """
    base = 150.0
    # Long seed so EMA converges well
    seed = flat_bars(price=base, n=period + 20)

    if direction == "CALL":
        # Established bullish: price well above EMA
        established = [make_bar(base * 1.05 + i * 0.01) for i in range(10)]
        # Reversal: price drops below base (below EMA which is near base)
        reversed_bars = [make_bar(base * 0.97 - i * 0.01) for i in range(n_reversed)]
    else:
        # Established bearish: price well below EMA
        established = [make_bar(base * 0.95 - i * 0.01) for i in range(10)]
        # Reversal: price rises above base (above EMA which is near base)
        reversed_bars = [make_bar(base * 1.03 + i * 0.01) for i in range(n_reversed)]

    return seed + established + reversed_bars


# ---------------------------------------------------------------------------
# is_trend_reversal_confirmed — unit tests
# ---------------------------------------------------------------------------

class TestIsTrendReversalConfirmed:

    def test_n1_single_bearish_bar_triggers_call_reversal(self):
        """n=1 (original behaviour): 1 bar below EMA fires reversal for CALL."""
        bars = _bars_with_reversal(n_reversed=1, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=1) is True

    def test_n2_single_bearish_bar_does_not_trigger_call_reversal(self):
        """n=2: only 1 bar below EMA is NOT enough — reversal not confirmed."""
        bars = _bars_with_reversal(n_reversed=1, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=2) is False

    def test_n2_two_bearish_bars_triggers_call_reversal(self):
        """n=2: 2 consecutive bars below EMA confirms CALL reversal."""
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=2) is True

    def test_n1_single_bullish_bar_triggers_put_reversal(self):
        """n=1: 1 bar above EMA fires reversal for PUT."""
        bars = _bars_with_reversal(n_reversed=1, direction="PUT")
        assert is_trend_reversal_confirmed(bars, "PUT", period=21, n=1) is True

    def test_n2_single_bullish_bar_does_not_trigger_put_reversal(self):
        """n=2: only 1 bar above EMA is NOT enough for PUT reversal."""
        bars = _bars_with_reversal(n_reversed=1, direction="PUT")
        assert is_trend_reversal_confirmed(bars, "PUT", period=21, n=2) is False

    def test_n2_two_bullish_bars_triggers_put_reversal(self):
        """n=2: 2 consecutive bars above EMA confirms PUT reversal."""
        bars = _bars_with_reversal(n_reversed=2, direction="PUT")
        assert is_trend_reversal_confirmed(bars, "PUT", period=21, n=2) is True

    def test_n0_disables_reversal_for_call(self):
        """n=0 disables the exit — even 5 reversed bars return False."""
        bars = _bars_with_reversal(n_reversed=5, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=0) is False

    def test_n0_disables_reversal_for_put(self):
        """n=0 disables the exit — even 5 reversed bars return False."""
        bars = _bars_with_reversal(n_reversed=5, direction="PUT")
        assert is_trend_reversal_confirmed(bars, "PUT", period=21, n=0) is False

    def test_established_trend_no_reversal(self):
        """A clean rising series has no reversal — should return False for CALL."""
        bars = rising_bars(base=100.0, n=40, step=0.5)
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=2) is False

    def test_established_falling_no_put_reversal(self):
        """A clean falling series has no reversal — should return False for PUT."""
        bars = falling_bars(base=200.0, n=40, step=0.5)
        assert is_trend_reversal_confirmed(bars, "PUT", period=21, n=2) is False

    def test_invalid_direction_returns_false(self):
        bars = rising_bars(n=30)
        assert is_trend_reversal_confirmed(bars, "neutral", period=21, n=2) is False
        assert is_trend_reversal_confirmed(bars, "",        period=21, n=2) is False

    def test_n3_requires_three_bars(self):
        """n=3: two reversed bars are not enough."""
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=3) is False

    def test_n3_three_bars_confirms(self):
        """n=3: three reversed bars are exactly enough."""
        bars = _bars_with_reversal(n_reversed=3, direction="CALL")
        assert is_trend_reversal_confirmed(bars, "CALL", period=21, n=3) is True



# ---------------------------------------------------------------------------
# Guard C — VWAP confirmation tests (the AMZN May 28 pattern)
# ---------------------------------------------------------------------------
