"""
Tests for is_trend_reversal_confirmed() and the updated check_exit_conditions()
consecutive-bar TREND_REVERSAL gate + VWAP confirmation guard (Guard C).

Guard B: N consecutive 15-min bars on the wrong EMA side before firing.
Guard C: underlying must have crossed back through entry VWAP — EMA dips
         while price stays above VWAP (CALL) are bull-flag consolidations,
         not genuine reversals (the AMZN May 28 pattern).
"""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import (
    is_trend_reversal_confirmed,
    check_exit_conditions,
    calculate_ema,
    calculate_vwap,
)
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


def _make_exit_kwargs(bars_15m, direction, entry_price=5.00, current_price=4.00,
                      entry_time=None, now=None, current_underlying=None):
    """
    Build a minimal kwargs dict for check_exit_conditions.

    current_underlying controls Guard C (VWAP confirmation):
      - default None  → placed AT entry VWAP (Guard C blocks TREND_REVERSAL)
      - pass a value below entry VWAP for CALL → Guard C allows TREND_REVERSAL
      - pass a value above entry VWAP for PUT  → Guard C allows TREND_REVERSAL
    """
    stop  = round(entry_price * (1 - settings.stop_loss_pct), 2)
    tp    = round(entry_price * (1 + settings.take_profit_pct), 2)
    vwap  = calculate_vwap(bars_15m) or entry_price

    # Default: underlying AT entry VWAP — VWAP_BREAK won't fire, but Guard C
    # will suppress TREND_REVERSAL (underlying not yet below VWAP for CALL).
    underlying = current_underlying if current_underlying is not None else vwap

    return dict(
        direction=direction,
        entry_price=entry_price,
        current_option_price=current_price,
        stop_price=stop,
        tp1_price=tp,
        tp2_price=tp,
        tp1_hit=False,
        vwap_at_entry=vwap,
        current_underlying=underlying,
        bars_15m=bars_15m,
        remaining_qty=1,
        entry_time=entry_time,
        now=now,
    )


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
# check_exit_conditions — TREND_REVERSAL integration
# ---------------------------------------------------------------------------

class TestCheckExitConditionsTrendReversal:

    def _past_hold(self, minutes=25):
        """Return (entry_time, now) so the min-hold window has passed."""
        now = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
        entry = now - timedelta(minutes=minutes)
        return entry, now

    def test_single_reversal_bar_blocked_with_n2(self):
        """
        1 bar below EMA does NOT trigger TREND_REVERSAL when n=2.
        This is the META / SPY pattern — single-bar chop shouldn't exit.
        """
        bars = _bars_with_reversal(n_reversed=1, direction="CALL")
        entry, now = self._past_hold(25)
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=4.80,
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2):
            result = check_exit_conditions(**kwargs)
        assert result is None, "Single noisy bar must NOT trigger TREND_REVERSAL"

    def test_two_reversal_bars_trigger_with_n2(self):
        """
        2 consecutive bars below EMA + underlying below entry VWAP → TREND_REVERSAL.
        underlying is set below vwap so Guard C (VWAP confirm) is satisfied.
        """
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        entry, now = self._past_hold(25)
        vwap = calculate_vwap(bars) or 150.0
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=4.80,
                                   current_underlying=vwap * 0.999,  # just below VWAP, inside band → Guard C passes, no VWAP_BREAK
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2):
            result = check_exit_conditions(**kwargs)
        assert result is not None
        assert result.reason == "TREND_REVERSAL"

    def test_single_bar_triggers_with_n1(self):
        """n=1 restores original behaviour — 1 bar fires TREND_REVERSAL (underlying below VWAP)."""
        bars = _bars_with_reversal(n_reversed=1, direction="CALL")
        entry, now = self._past_hold(25)
        vwap = calculate_vwap(bars) or 150.0
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=4.80,
                                   current_underlying=vwap * 0.999,  # just below VWAP, inside band → Guard C passes, no VWAP_BREAK
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 1):
            result = check_exit_conditions(**kwargs)
        assert result is not None
        assert result.reason == "TREND_REVERSAL"

    def test_put_single_bar_blocked_with_n2(self):
        """PUT: 1 bar above EMA does NOT trigger TREND_REVERSAL when n=2."""
        bars = _bars_with_reversal(n_reversed=1, direction="PUT")
        entry, now = self._past_hold(25)
        kwargs = _make_exit_kwargs(bars, "PUT", entry_price=5.00,
                                   current_price=4.80,
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2):
            result = check_exit_conditions(**kwargs)
        assert result is None, "Single noisy bar must NOT trigger PUT TREND_REVERSAL"

    def test_put_two_bars_trigger_with_n2(self):
        """
        PUT: 2 consecutive bars above EMA + underlying above entry VWAP → TREND_REVERSAL.
        underlying is set above vwap so Guard C (VWAP confirm) is satisfied.
        """
        bars = _bars_with_reversal(n_reversed=2, direction="PUT")
        entry, now = self._past_hold(25)
        vwap = calculate_vwap(bars) or 150.0
        kwargs = _make_exit_kwargs(bars, "PUT", entry_price=5.00,
                                   current_price=4.80,
                                   current_underlying=vwap * 1.001,  # just above VWAP, inside band → Guard C passes, no VWAP_BREAK
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2):
            result = check_exit_conditions(**kwargs)
        assert result is not None
        assert result.reason == "TREND_REVERSAL"

    def test_min_hold_suppresses_reversal_when_trade_is_profitable(self):
        """
        Trade within hold window AND currently profitable (current > entry):
        TREND_REVERSAL must be suppressed.
        This is the NVDA scenario — entered at mid, option already up on first tick.
        """
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        now = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
        entry = now - timedelta(minutes=10)   # only 10 min in — below 20-min hold
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=5.20,   # above entry — profitable
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2), \
             patch.object(settings, "trend_reversal_min_hold_minutes", 20):
            result = check_exit_conditions(**kwargs)
        assert result is None, (
            "Min-hold must block TREND_REVERSAL when trade is profitable within the window"
        )

    def test_min_hold_allows_reversal_when_trade_is_at_loss(self):
        """
        Trade within hold window AND at a loss AND underlying below VWAP:
        TREND_REVERSAL must fire — no protection when trade is losing AND
        price has fallen through VWAP (genuine reversal, not consolidation).
        """
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        now = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
        entry = now - timedelta(minutes=10)   # only 10 min in — below 20-min hold
        vwap = calculate_vwap(bars) or 150.0
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=4.80,          # below entry — losing
                                   current_underlying=vwap * 0.999,  # just below VWAP, inside band → Guard C passes, no VWAP_BREAK
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2), \
             patch.object(settings, "trend_reversal_min_hold_minutes", 20):
            result = check_exit_conditions(**kwargs)
        assert result is not None, (
            "Min-hold must NOT block TREND_REVERSAL when the trade is already at a loss"
        )
        assert result.reason == "TREND_REVERSAL"

    def test_min_hold_allows_reversal_at_exact_entry_price(self):
        """
        current_price == entry_price (exactly at breakeven): guard still applies.
        We only bypass the guard when BELOW entry (an actual loss).
        """
        bars = _bars_with_reversal(n_reversed=2, direction="CALL")
        now = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
        entry = now - timedelta(minutes=10)
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=5.00,   # exactly at entry
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 2), \
             patch.object(settings, "trend_reversal_min_hold_minutes", 20):
            result = check_exit_conditions(**kwargs)
        assert result is None, (
            "At exact entry price the guard still applies (not yet a loss)"
        )

    def test_n0_disables_trend_reversal_exit_entirely(self):
        """n=0: TREND_REVERSAL is never triggered regardless of bar count."""
        bars = _bars_with_reversal(n_reversed=5, direction="CALL")
        entry, now = self._past_hold(60)
        vwap = calculate_vwap(bars) or 150.0
        kwargs = _make_exit_kwargs(bars, "CALL", entry_price=5.00,
                                   current_price=4.80,
                                   current_underlying=vwap * 0.999,  # just below VWAP, inside band (Guard C would pass)
                                   entry_time=entry, now=now)
        with patch.object(settings, "trend_reversal_confirm_bars", 0):
            result = check_exit_conditions(**kwargs)
        assert result is None, "n=0 must disable TREND_REVERSAL exit entirely"


# ---------------------------------------------------------------------------
# Guard C — VWAP confirmation tests (the AMZN May 28 pattern)
# ---------------------------------------------------------------------------
