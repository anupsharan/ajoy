"""
Tests for trading-window helpers in strategy.py.

Covers: is_market_open, is_before_trading_start, is_past_cutoff,
        is_past_last_entry_time, is_in_trading_window.
All time-based tests use fixed datetimes anchored to a known Wednesday (2024-01-10)
to eliminate any dependency on wall-clock time.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services.strategy import (
    is_market_open,
    is_before_trading_start,
    is_past_cutoff,
    is_past_last_entry_time,
    is_in_trading_window,
)
from app.config import settings

ET = ZoneInfo("America/New_York")

# Fixed reference dates
WED   = (2024, 1, 10)   # Wednesday — normal trading day
MON   = (2024, 1,  8)   # Monday
SAT   = (2024, 1, 13)   # Saturday
SUN   = (2024, 1, 14)   # Sunday


def _et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Return a UTC datetime that maps to HH:MM in America/New_York on the given date."""
    et_aware = datetime(year, month, day, hour, minute, tzinfo=ET)
    return et_aware.astimezone(timezone.utc)


# ===========================================================================
# is_market_open
# ===========================================================================

class TestIsMarketOpen:

    def test_exactly_at_open_930(self):
        """9:30 ET is the boundary — market is open (>= 9:30)."""
        assert is_market_open(_et(*MON, 9, 30)) is True

    def test_one_minute_before_open(self):
        assert is_market_open(_et(*MON, 9, 29)) is False

    def test_intraday_open(self):
        assert is_market_open(_et(*WED, 11, 30)) is True

    def test_last_minute_open(self):
        """15:59 ET is still open."""
        assert is_market_open(_et(*WED, 15, 59)) is True

    def test_exactly_at_close_1600(self):
        """16:00 ET — market is closed (< 16:00 is strict)."""
        assert is_market_open(_et(*WED, 16, 0)) is False

    def test_after_close(self):
        assert is_market_open(_et(*WED, 17, 0)) is False

    def test_early_morning(self):
        assert is_market_open(_et(*WED, 8, 0)) is False

    def test_saturday(self):
        assert is_market_open(_et(*SAT, 11, 0)) is False

    def test_sunday(self):
        assert is_market_open(_et(*SUN, 11, 0)) is False


# ===========================================================================
# is_before_trading_start
# ===========================================================================

class TestIsBeforeTradingStart:

    def test_exactly_at_start_not_before(self):
        """11:15 ET — exactly at start; not before (< is strict so 11:15 is already allowed)."""
        assert is_before_trading_start(_et(*WED, 11, 15)) is False

    def test_one_minute_before_start(self):
        # Pin start to 11:15 so the test is independent of .env TRADING_START_TIME
        with patch.object(settings, "trading_start_time", "11:15"):
            assert is_before_trading_start(_et(*WED, 11, 14)) is True

    def test_early_morning_before_start(self):
        assert is_before_trading_start(_et(*WED, 8, 0)) is True

    def test_well_after_start(self):
        assert is_before_trading_start(_et(*WED, 12, 0)) is False

    def test_custom_start_time(self):
        """Property aliases must update when settings change."""
        with patch.object(settings, "trading_start_time", "10:00"):
            # 09:59 should now be 'before start'
            assert is_before_trading_start(_et(*WED, 9, 59)) is True
            # 10:00 should not be before start
            assert is_before_trading_start(_et(*WED, 12, 0)) is False


# ===========================================================================
# is_past_cutoff
# ===========================================================================

class TestIsPastCutoff:

    def test_exactly_at_cutoff(self):
        """14:45 ET — past cutoff (>= is inclusive). Pinned to 14:45 for .env independence."""
        with patch.object(settings, "trading_end_time", "14:45"):
            assert is_past_cutoff(_et(*WED, 14, 45)) is True

    def test_one_minute_before_cutoff(self):
        with patch.object(settings, "trading_end_time", "14:45"):
            assert is_past_cutoff(_et(*WED, 14, 44)) is False

    def test_well_after_cutoff_hour(self):
        with patch.object(settings, "trading_end_time", "14:45"):
            assert is_past_cutoff(_et(*WED, 15, 0)) is True

    def test_morning_not_past_cutoff(self):
        assert is_past_cutoff(_et(*WED, 10, 0)) is False

    def test_custom_cutoff_time(self):
        with patch.object(settings, "trading_end_time", "15:30"):
            assert is_past_cutoff(_et(*WED, 15, 30)) is True
            assert is_past_cutoff(_et(*WED, 15, 29)) is False


# ===========================================================================
# is_past_last_entry_time
# ===========================================================================

class TestIsPastLastEntryTime:

    def test_exactly_at_last_entry_time(self):
        """14:15 ET — at the boundary, no new entries (>= is inclusive)."""
        with patch.object(settings, "last_entry_time", "14:15"):
            assert is_past_last_entry_time(_et(*WED, 14, 15)) is True

    def test_one_minute_before_last_entry(self):
        with patch.object(settings, "last_entry_time", "14:15"):
            assert is_past_last_entry_time(_et(*WED, 14, 14)) is False

    def test_well_after_last_entry(self):
        with patch.object(settings, "last_entry_time", "14:15"):
            assert is_past_last_entry_time(_et(*WED, 14, 45)) is True

    def test_morning_not_past_last_entry(self):
        with patch.object(settings, "last_entry_time", "14:15"):
            assert is_past_last_entry_time(_et(*WED, 11, 0)) is False

    def test_last_entry_earlier_than_cutoff(self):
        """LAST_ENTRY_TIME fires before TRADING_END_TIME — independent check."""
        with patch.object(settings, "last_entry_time", "14:15"), \
             patch.object(settings, "trading_end_time", "14:45"):
            assert is_past_last_entry_time(_et(*WED, 14, 15)) is True
            assert is_past_cutoff(_et(*WED, 14, 15)) is False   # cutoff not hit yet


# ===========================================================================
# is_in_trading_window
# ===========================================================================

class TestIsInTradingWindow:

    def test_valid_midday_no_lunch(self):
        with patch.object(settings, "lunch_break_enabled", False):
            assert is_in_trading_window(_et(*WED, 12, 0)) is True

    def test_before_start_blocked(self):
        assert is_in_trading_window(_et(*WED, 9, 34)) is False

    def test_after_last_entry_time_blocked(self):
        # is_in_trading_window uses LAST_ENTRY_TIME for new-entry gate
        with patch.object(settings, "last_entry_time", "14:15"):
            assert is_in_trading_window(_et(*WED, 14, 15)) is False   # exactly at — blocked
            assert is_in_trading_window(_et(*WED, 14, 14)) is True    # one minute before — allowed
            assert is_in_trading_window(_et(*WED, 14, 30)) is False   # well after — blocked

    def test_after_cutoff_but_before_last_entry_irrelevant(self):
        # TRADING_END_TIME no longer gates is_in_trading_window (only is_past_cutoff)
        # After LAST_ENTRY_TIME, window is closed regardless of TRADING_END_TIME
        with patch.object(settings, "last_entry_time", "14:15"), \
             patch.object(settings, "trading_end_time", "14:45"):
            assert is_in_trading_window(_et(*WED, 14, 46)) is False   # past both

    def test_weekend_blocked(self):
        assert is_in_trading_window(_et(*SAT, 11, 0)) is False

    def test_outside_market_hours_blocked(self):
        """Before market opens entirely."""
        assert is_in_trading_window(_et(*WED, 8, 0)) is False

    # ── Lunch break ─────────────────────────────────────────────────────────

    def test_lunch_start_boundary_blocked(self):
        """Exactly at 11:30 ET is inside the blocked window (start is inclusive)."""
        ts = _et(*WED, 11, 30)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "11:30"), \
             patch.object(settings, "lunch_break_end", "12:15"):
            assert is_in_trading_window(ts) is False

    def test_one_minute_before_lunch_allowed(self):
        ts = _et(*WED, 11, 29)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "11:30"), \
             patch.object(settings, "lunch_break_end", "12:15"):
            assert is_in_trading_window(ts) is True

    def test_lunch_midpoint_blocked(self):
        ts = _et(*WED, 11, 52)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "11:30"), \
             patch.object(settings, "lunch_break_end", "12:15"):
            assert is_in_trading_window(ts) is False

    def test_lunch_end_boundary_allowed(self):
        """Exactly at 12:15 ET is allowed (end is exclusive: current < lb_end)."""
        ts = _et(*WED, 12, 15)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "11:30"), \
             patch.object(settings, "lunch_break_end", "12:15"):
            assert is_in_trading_window(ts) is True

    def test_lunch_disabled_passes_through(self):
        """When LUNCH_BREAK_ENABLED=0 the gate must not block any intraday time."""
        ts = _et(*WED, 11, 50)
        with patch.object(settings, "lunch_break_enabled", False):
            assert is_in_trading_window(ts) is True

    def test_custom_lunch_window(self):
        """Lunch times can be changed via settings."""
        ts_inside = _et(*WED, 13, 0)
        ts_after  = _et(*WED, 13, 30)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "12:30"), \
             patch.object(settings, "lunch_break_end", "13:30"):
            assert is_in_trading_window(ts_inside) is False
            assert is_in_trading_window(ts_after) is True  # exactly at end → allowed

    def test_weekend_short_circuits_before_lunch_check(self):
        """Weekend should return False from is_market_open, not the lunch check."""
        ts = _et(*SAT, 11, 50)
        with patch.object(settings, "lunch_break_enabled", True), \
             patch.object(settings, "lunch_break_start", "11:30"), \
             patch.object(settings, "lunch_break_end", "12:15"):
            assert is_in_trading_window(ts) is False
