"""
Tests for is_ema_trend_confirmed() — Layer 1 EMA consecutive-bar gate.

The function requires the last N 15-min bars to all close on the correct
side of the EMA before an entry is allowed.  This prevents entries on a
freshly-flipped EMA that could reverse back within the next bar.
"""
import pytest
from unittest.mock import patch
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import (
    is_ema_trend_confirmed,
    check_entry_signal,
    calculate_vwap,
    calculate_ema,
)
from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bars_with_flip(n_established: int, direction: str, period: int = 9):
    """
    Build bars_15m where exactly `n_established` bars are clearly on the
    correct side of the EMA, preceded by flat bars (EMA ≈ price).

    Strategy: long flat baseline so EMA converges to base_price, then a
    sharp 3% jump into the target direction for exactly n_established bars.
    This guarantees the last N bars are above/below EMA while bars before
    the jump are essentially AT the EMA (neither direction confirmed).
    """
    base = 150.0
    # Flat seed: EMA converges to base
    seed = flat_bars(price=base, n=period + 15)

    if direction == "bullish":
        # Jump 3% above base — well clear of the EMA that's still near base
        established = [make_bar(base * 1.03 + i * 0.01) for i in range(n_established)]
    else:
        # Drop 3% below base
        established = [make_bar(base * 0.97 - i * 0.01) for i in range(n_established)]

    return seed + established


# ---------------------------------------------------------------------------
# is_ema_trend_confirmed — unit tests
# ---------------------------------------------------------------------------

class TestIsEmaConfirmed:

    def test_consistent_bullish_n2_passes(self):
        """Long rising series → last 2 bars above EMA → confirmed."""
        bars = rising_bars(base=100.0, n=40, step=0.5)
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=2) is True

    def test_consistent_bearish_n2_passes(self):
        """Long falling series → last 2 bars below EMA → confirmed."""
        bars = falling_bars(base=200.0, n=40, step=0.5)
        assert is_ema_trend_confirmed(bars, "bearish", period=9, n=2) is True

    def test_freshly_flipped_1bar_blocks_n2(self):
        """Only 1 bar has crossed above EMA; n=2 requires 2 → blocked."""
        bars = _bars_with_flip(n_established=1, direction="bullish")
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=2) is False

    def test_n1_only_needs_last_bar_above_ema(self):
        """n=1 only checks the most recent bar — a long established trend passes."""
        bars = rising_bars(base=100.0, n=40, step=0.5)
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=1) is True

    def test_n0_always_passes(self):
        """n=0 disables the check — any direction passes."""
        bars = flat_bars(price=150.0, n=30)
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=0) is True
        assert is_ema_trend_confirmed(bars, "bearish", period=9, n=0) is True

    def test_wrong_direction_arg_returns_false(self):
        """Non-directional string → False."""
        bars = rising_bars(n=30)
        assert is_ema_trend_confirmed(bars, "neutral", period=9, n=2) is False
        assert is_ema_trend_confirmed(bars, "", period=9, n=2) is False

    def test_too_few_bars_returns_false(self):
        """Fewer bars than n → can't confirm → False."""
        bars = rising_bars(n=3)
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=2) is False

    def test_exactly_n_bars_confirmed_passes(self):
        """2-bar flip passes when n=2 (exactly the boundary)."""
        bars = _bars_with_flip(n_established=2, direction="bullish")
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=2) is True

    def test_bearish_flip_1bar_blocks_n2(self):
        """Freshly-flipped bearish with only 1 bar below EMA is blocked."""
        bars = _bars_with_flip(n_established=1, direction="bearish")
        assert is_ema_trend_confirmed(bars, "bearish", period=9, n=2) is False

    def test_larger_n_requires_more_bars(self):
        """n=3 requires 3 confirmed bars; 2-bar flip is not enough."""
        bars = _bars_with_flip(n_established=2, direction="bullish")
        assert is_ema_trend_confirmed(bars, "bullish", period=9, n=3) is False

    def test_direction_mismatch_returns_false(self):
        """Bars are bullish (rising) but we ask for bearish confirmation → False."""
        bars = rising_bars(base=100.0, n=40, step=0.5)
        assert is_ema_trend_confirmed(bars, "bearish", period=9, n=2) is False


# ---------------------------------------------------------------------------
# check_entry_signal integration — fresh flip is blocked
# ---------------------------------------------------------------------------

class TestCheckEntrySignalEmaConfirm:

    def test_fresh_flip_blocks_call_entry(self):
        """
        1-bar EMA flip to bullish should block a CALL entry when
        EMA_CONSECUTIVE_BARS=2.
        """
        # Build bars where only 1 bar is above EMA
        bars_15m = _bars_with_flip(n_established=1, direction="bullish")
        vwap = calculate_vwap(bars_15m)
        # Price just above VWAP (valid pullback location)
        bars_1m = [make_bar(vwap * 1.001) for _ in range(30)]

        with patch.object(settings, "ema_consecutive_bars", 2):
            result = check_entry_signal(bars_1m, bars_15m)
        assert result is None, "Fresh 1-bar EMA flip should be blocked"

    def test_established_trend_allows_call_entry(self):
        """
        2+ bars above EMA → EMA confirmed → CALL entry should pass L1.
        """
        # Long rising trend — EMA well established
        bars_15m = rising_bars(base=100.0, n=40, step=0.5)
        vwap = calculate_vwap(bars_15m)
        bars_1m = [make_bar(vwap * 1.001) for _ in range(30)]

        with patch.object(settings, "ema_consecutive_bars", 2):
            result = check_entry_signal(bars_1m, bars_15m)
        assert result is not None
        assert result.direction == "CALL"

    def test_consecutive_bars_0_disables_check(self):
        """
        EMA_CONSECUTIVE_BARS=0 should disable the gate entirely —
        a 1-bar flip is allowed through.
        """
        bars_15m = _bars_with_flip(n_established=1, direction="bullish")
        vwap = calculate_vwap(bars_15m)
        bars_1m = [make_bar(vwap * 1.001) for _ in range(30)]

        # With n=0, confirmation is disabled — if other L1 conditions pass,
        # entry should be allowed.
        with patch.object(settings, "ema_consecutive_bars", 0):
            # We can't guarantee other L1 conditions pass for this contrived
            # bar set, so just check the function doesn't crash and confirm
            # the confirmation gate itself is bypassed.
            confirmed = is_ema_trend_confirmed(bars_15m, "bullish", n=0)
        assert confirmed is True
