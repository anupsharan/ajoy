"""
Tests for compute_trailing_stop() in strategy.py.

Covers:
  - No movement below both thresholds
  - Stage 1: breakeven lock at 5% (was 9%)
  - Stage 2: trail from current price at 10% gain (new dynamic formula)
  - Minimum hold time guard (don't activate trailing stop before N minutes)
  - Stop only ever moves UP (never down)
  - Transition between stages
  - Config disable (both thresholds = 0)
  - Edge cases: zero / None prices
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from app.services.strategy import compute_trailing_stop
from app.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    return Settings(
        trailing_stop_breakeven_pct=overrides.get("breakeven_pct", 0.06),
        trailing_stop_lock_profit_pct=overrides.get("lock_profit_pct", 0.01),
        trailing_stop_trail_pct=overrides.get("trail_pct", 0.10),
        trailing_stop_trail_from_current_pct=overrides.get("trail_from_current_pct", 0.10),
        trailing_stop_min_hold_minutes=overrides.get("min_hold", 0),  # default: hold disabled
    )


def _ts(entry, current, current_stop, entry_time=None, now=None, **cfg_overrides):
    """Call compute_trailing_stop with optional Settings overrides."""
    s = _make_settings(**cfg_overrides)
    with patch("app.services.strategy.settings", s):
        return compute_trailing_stop(entry, current, current_stop,
                                     entry_time=entry_time, now=now)


# ---------------------------------------------------------------------------
# Below both thresholds — no change
# ---------------------------------------------------------------------------

def test_below_breakeven_threshold_no_change():
    """Option up 3% — below 6% breakeven threshold → stop unchanged."""
    entry, current = 5.00, 5.15      # +3%
    original_stop  = 3.75
    result = _ts(entry, current, original_stop)
    assert result == original_stop


def test_just_below_breakeven_threshold_no_change():
    """Option up 5.99% — just below 6% → stop unchanged."""
    entry   = 5.00
    current = round(entry * 1.0599, 4)   # 5.99% gain
    result  = _ts(entry, current, 3.75)
    assert result == 3.75


def test_negative_pnl_no_change():
    """Trade in loss — stop must not move."""
    result = _ts(entry=5.00, current=4.50, current_stop=3.75)
    assert result == 3.75


# ---------------------------------------------------------------------------
# Stage 1 — profit lock at 6% gain (stop = entry × 1.01)
# ---------------------------------------------------------------------------

def test_exactly_at_breakeven_threshold_raises_stop():
    """Option at exactly +6% → stop raised to entry × 1.01 (1% above entry)."""
    entry   = 5.00
    current = entry * 1.06            # exactly 6%
    result  = _ts(entry, current, 3.75)
    assert result == pytest.approx(entry * 1.01, abs=0.01)   # $5.05


def test_above_breakeven_below_trail_raises_to_lock():
    """Option between 6% and 10% → stop = entry × 1.01."""
    entry   = 5.00
    current = entry * 1.08            # 8% gain — between Stage 1 and Stage 2
    result  = _ts(entry, current, 3.75)
    assert result == pytest.approx(entry * 1.01, abs=0.01)


def test_breakeven_stop_never_lowers_existing_stop():
    """
    If the stop was already raised above Stage 1 level (manually or by trailing),
    Stage 1 must NOT lower it.
    """
    entry         = 5.00
    current       = entry * 1.08      # 8% gain — Stage 1 territory
    lock_level    = round(entry * 1.01, 2)   # $5.05
    existing_stop = 5.20              # already above lock level
    result        = _ts(entry, current, existing_stop)
    assert result == existing_stop    # unchanged — not lowered


def test_lock_profit_pct_zero_gives_pure_breakeven():
    """lock_profit_pct=0 restores the original breakeven-at-entry behaviour."""
    entry   = 5.00
    current = entry * 1.06
    result  = _ts(entry, current, 3.75, lock_profit_pct=0.0)
    assert result == entry            # stop = $5.00 exactly (no added buffer)


# ---------------------------------------------------------------------------
# Stage 2 — trail from current price (10% gain threshold, new formula)
#
# Formula: stop = current × (1 − trail_from_current_pct)
# Default trail_from_current_pct = 0.10 → stop = 90% of current price
# ---------------------------------------------------------------------------

def test_exactly_at_trail_threshold():
    """
    Option at exactly +10% → Stage 2 fires.
    current = $5.50, stop = 5.50 × (1 − 0.10) = $4.95
    """
    entry   = 5.00
    current = entry * 1.10            # $5.50
    result  = _ts(entry, current, 3.75)
    assert result == pytest.approx(4.95, abs=0.01)


def test_trail_stop_tracks_rising_option():
    """
    At 20% gain: current = $6.00
    stop = 6.00 × 0.90 = $5.40
    """
    entry   = 5.00
    current = entry * 1.20            # $6.00
    result  = _ts(entry, current, 3.75)
    assert result == pytest.approx(5.40, abs=0.01)


def test_trail_stop_at_50_pct_gain():
    """
    At 50% gain: current = $7.50
    stop = 7.50 × 0.90 = $6.75
    """
    entry   = 5.00
    current = entry * 1.50            # $7.50
    result  = _ts(entry, current, 3.75)
    assert result == pytest.approx(6.75, abs=0.01)


def test_trail_stop_never_lowers_after_peak():
    """
    Option peaked at 40% (stop raised to $5.40 = 6.50×0.90),
    now pulled back to 22% (current $6.10).
    Stage 2 candidate at 22% = 6.10 × 0.90 = $5.49.
    If stop was already at $5.85 from peak: stays at $5.85.
    """
    entry         = 5.00
    current       = entry * 1.22      # $6.10 — still Stage 2
    existing_stop = 5.85              # set at prior peak (40% gain: 7.00×0.90=6.30... hmm)
    result        = _ts(entry, current, existing_stop)
    assert result == existing_stop    # preserved — not lowered


def test_trail_stop_with_narrow_buffer():
    """
    trail_from_current_pct=0.05 (5% buffer — the 'tight' setting user asked about).
    At 20% gain: current = $6.00, stop = 6.00 × 0.95 = $5.70
    """
    entry   = 5.00
    current = entry * 1.20            # $6.00
    result  = _ts(entry, current, 3.75, trail_from_current_pct=0.05)
    assert result == pytest.approx(5.70, abs=0.01)


# ---------------------------------------------------------------------------
# Stage transition
# ---------------------------------------------------------------------------

def test_transition_from_stage1_to_trail():
    """
    Stop was at Stage 1 lock level ($5.05 = entry×1.01) after a 6% move.
    Option now at 20% → Stage 2 candidate = $6.00 × 0.90 = $5.40 > $5.05.
    Stop should advance to $5.40.
    """
    entry         = 5.00
    current       = entry * 1.20      # 20%
    existing_stop = round(entry * 1.01, 2)   # $5.05 — set by Stage 1
    result        = _ts(entry, current, existing_stop)
    assert result == pytest.approx(5.40, abs=0.01)


def test_drop_from_stage2_back_to_stage1_zone_preserves_trail_stop():
    """
    Stop was raised to $5.49 at peak (22% gain, 6.10×0.90).
    Option pulls back to 7% (Stage 1 zone).
    Stage 1 candidate = $5.05 (entry×1.01) < $5.49 → stop stays at $5.49.
    """
    entry         = 5.00
    current       = entry * 1.07      # in Stage 1 zone
    existing_stop = 5.49              # set at prior peak
    result        = _ts(entry, current, existing_stop)
    assert result == existing_stop    # $5.49 preserved


def test_stage2_candidate_below_existing_stop_preserved():
    """
    At 10% gain the trail candidate is $4.95 (10% buffer from $5.50).
    If the existing stop is already $5.05 (Stage 1 lock from 6% move),
    max($4.95, $5.05) = $5.05 → stop doesn't move backward.
    """
    entry         = 5.00
    current       = entry * 1.10     # $5.50 — just entered Stage 2
    existing_stop = round(entry * 1.01, 2)   # $5.05 from Stage 1
    result        = _ts(entry, current, existing_stop)
    assert result == existing_stop   # $5.05 — candidate ($4.95) rejected


# ---------------------------------------------------------------------------
# Real-world scenario
# ---------------------------------------------------------------------------

def test_xlf_real_world_scenario():
    """
    XLF CALL from earlier session: entry=$0.24, option rose ~18%.
    At 18% gain current ≈ $0.2832.
    Stage 2 candidate = 0.2832 × 0.90 = ~$0.255 ≈ $0.25 or $0.26.
    This is above entry ($0.24) → trade is protected.
    Without trailing stop it exited at $0.21 for -$60; with trail ~breakeven.
    """
    entry         = 0.24
    current       = round(entry * 1.18, 4)   # ~$0.2832
    result        = _ts(entry, current, round(entry * 0.75, 2))
    # Candidate = 0.2832 × 0.90 ≈ 0.255 → above entry ($0.24)
    assert result > entry
    assert result == pytest.approx(0.25, abs=0.02)


# ---------------------------------------------------------------------------
# Config: disable trailing stop
# ---------------------------------------------------------------------------

def test_trailing_stop_disabled_when_both_zero():
    """Setting both thresholds to 0 disables trailing stop entirely."""
    entry   = 5.00
    current = entry * 1.50            # 50% gain — would normally trail
    result  = _ts(entry, current, 3.75, breakeven_pct=0, trail_pct=0)
    assert result == 3.75             # unchanged


def test_only_breakeven_disabled_trail_still_works():
    """With breakeven_pct=0 but trail_pct active, Stage 2 still fires."""
    entry   = 5.00
    current = entry * 1.20            # 20% gain
    result  = _ts(entry, current, 3.75, breakeven_pct=0, trail_pct=0.10)
    assert result > entry             # stop advanced above entry


def test_custom_breakeven_threshold_respected():
    """Custom breakeven at 4% — 5% gain should trigger Stage 1 (entry × 1.01)."""
    entry   = 10.00
    current = entry * 1.05            # 5% gain — above custom 4% threshold
    result  = _ts(entry, current, 7.50, breakeven_pct=0.04)
    assert result == pytest.approx(entry * 1.01, abs=0.01)  # $10.10


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_zero_entry_price_no_crash():
    """entry_price=0 must not raise ZeroDivisionError."""
    result = compute_trailing_stop(0, 5.00, 3.75)
    assert result == 3.75


def test_none_current_price_no_crash():
    """None current_option_price must return current_stop unchanged."""
    result = compute_trailing_stop(5.00, None, 3.75)
    assert result == 3.75


def test_stop_already_above_trail_candidate_preserved():
    """
    If an existing stop is somehow already higher than the new trailing
    candidate, the higher value is kept.
    """
    entry         = 5.00
    current       = entry * 1.20     # Stage 2: candidate = 6.00 × 0.90 = 5.40
    existing_stop = 5.80             # already higher
    result        = _ts(entry, current, existing_stop)
    assert result == existing_stop


# ---------------------------------------------------------------------------
# Minimum hold time guard
#
# The trailing stop must not activate until the trade has been open for
# TRAILING_STOP_MIN_HOLD_MINUTES.  This prevents the stop from jumping to
# breakeven on the very first management tick when entering at mid-price
# causes the option to appear > 5% in-the-money immediately.
# ---------------------------------------------------------------------------

def _times(held_minutes: float):
    """Return (entry_time, now) with the given hold duration."""
    now   = datetime(2024, 1, 15, 14, 0, tzinfo=timezone.utc)
    entry = now - timedelta(minutes=held_minutes)
    return entry, now


def test_hold_time_blocks_stage1_within_window():
    """
    Option is 7% above entry (Stage 1 territory) but trade is only 5 min old.
    With min_hold=15 the trailing stop must NOT activate yet.
    This is the NVDA scenario: limit entry at mid, option immediately up.
    """
    entry, now = _times(5)           # only 5 min since entry
    entry_price, current = 2.48, 2.65  # +7% — above 6% threshold
    result = _ts(entry_price, current, 1.93,
                 entry_time=entry, now=now, min_hold=15)
    assert result == 1.93, "Trailing stop must not fire within min_hold window"


def test_hold_time_blocks_stage2_within_window():
    """Stage 2 (12% gain) also blocked within the hold window."""
    entry, now = _times(10)          # 10 min in — still below 15-min hold
    result = _ts(5.00, 5.00 * 1.12, 3.75,
                 entry_time=entry, now=now, min_hold=15)
    assert result == 3.75, "Stage 2 must not fire within min_hold window"


def test_hold_time_allows_stage1_after_window():
    """Stage 1 fires normally once the hold window has passed."""
    entry, now = _times(20)          # 20 min in — past 15-min hold
    result = _ts(5.00, 5.00 * 1.07, 3.75,   # 7% gain — above 6% threshold
                 entry_time=entry, now=now, min_hold=15)
    assert result == pytest.approx(5.00 * 1.01, abs=0.01), (
        "Stage 1 (profit lock) must fire after hold window"
    )


def test_hold_time_allows_stage2_after_window():
    """Stage 2 fires normally once the hold window has passed."""
    entry, now = _times(20)
    result = _ts(5.00, 5.00 * 1.20, 3.75,   # 20% gain
                 entry_time=entry, now=now, min_hold=15)
    assert result == pytest.approx(5.40, abs=0.01), (
        "Stage 2 trail must fire after hold window"
    )


def test_hold_time_zero_disables_guard():
    """min_hold=0 disables the guard — Stage 1 fires immediately."""
    entry, now = _times(1)           # 1 min in
    result = _ts(5.00, 5.00 * 1.07, 3.75,   # 7% gain
                 entry_time=entry, now=now, min_hold=0)
    assert result == pytest.approx(5.00 * 1.01, abs=0.01), (
        "With min_hold=0 trailing stop fires at any time"
    )


def test_hold_time_no_entry_time_no_guard():
    """
    When entry_time is not provided the guard is skipped and trailing stop
    works as before (for backward compat and manual calls).
    """
    result = _ts(5.00, 5.00 * 1.07, 3.75, min_hold=15)   # 7% gain
    assert result == pytest.approx(5.00 * 1.01, abs=0.01), (
        "No entry_time → guard skipped → Stage 1 fires"
    )


def test_hold_time_exactly_at_boundary_allows():
    """Trade held for exactly min_hold minutes — stop activates (>= boundary)."""
    entry, now = _times(15)          # exactly 15 min
    result = _ts(5.00, 5.00 * 1.07, 3.75,   # 7% gain
                 entry_time=entry, now=now, min_hold=15)
    assert result == pytest.approx(5.00 * 1.01, abs=0.01), (
        "Exactly at hold boundary must allow trailing stop"
    )
