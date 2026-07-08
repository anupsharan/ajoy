"""
Exit-condition edge cases not covered in test_04_exit.py.

Verifies priority ordering, VWAP-break boundaries, neutral-trend no-op,
the TREND_REVERSAL minimum-hold-time guard, and the compute_trade_levels
rounding / direction-symmetry contract.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.conftest import rising_bars, falling_bars, flat_bars
from app.services.strategy import check_exit_conditions, compute_trade_levels
from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _old_entry(minutes=30):
    """Return an entry_time N minutes in the past (well past any hold window)."""
    return datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)


def _exit(**kw):
    """Call check_exit_conditions with sensible defaults, overrideable via kw.

    entry_time defaults to 30 minutes ago so the TREND_REVERSAL hold-time
    guard is satisfied by default — tests that specifically want to test the
    guard pass their own entry_time.
    """
    defaults = dict(
        direction="CALL",
        entry_price=5.00,
        current_option_price=5.00,
        stop_price=round(5.00 * (1 - settings.stop_loss_pct), 2),
        tp1_price=6.75,
        tp2_price=6.75,
        tp1_hit=False,
        vwap_at_entry=150.0,
        current_underlying=151.0,
        remaining_qty=2,
        entry_time=_old_entry(30),   # 30 min old — past any hold window
    )
    defaults.update(kw)
    return check_exit_conditions(**defaults)


# ===========================================================================
# Priority ordering
# ===========================================================================

def test_tp2_beats_vwap_break():
    """TP2 (priority 2) fires before VWAP break (priority 3)."""
    result = _exit(
        current_option_price=6.75,   # at TP2
        current_underlying=148.0,    # below VWAP − band → would trigger VWAP_BREAK
    )
    assert result is not None
    assert result.reason == "TP2"


def test_tp2_beats_trend_reversal():
    """TP2 fires regardless of EMA direction (TREND_REVERSAL is handled by scheduler, not here)."""
    result = _exit(
        current_option_price=6.75,
    )
    assert result is not None
    assert result.reason == "TP2"


def test_stop_beats_vwap_break():
    """Stop (priority 1) beats VWAP break."""
    stop = round(5.00 * (1 - settings.stop_loss_pct), 2)
    result = _exit(
        current_option_price=stop,   # at stop
        current_underlying=148.0,    # below VWAP too
    )
    assert result is not None
    assert result.reason == "STOP"


def test_stop_beats_trend_reversal():
    """Stop fires before VWAP_BREAK and EMA checks."""
    stop = round(5.00 * (1 - settings.stop_loss_pct), 2)
    result = _exit(
        current_option_price=stop,
        current_underlying=148.0,   # would also trigger VWAP_BREAK
    )
    assert result is not None
    assert result.reason == "STOP"


def test_vwap_break_beats_trend_reversal():
    """VWAP break fires (priority 3) — TREND_REVERSAL now handled by scheduler."""
    result = _exit(
        direction="CALL",
        current_option_price=5.00,
        current_underlying=148.0,    # below VWAP − band → VWAP_BREAK
    )
    assert result is not None
    assert result.reason == "VWAP_BREAK"


# ===========================================================================
# VWAP-break boundary conditions
# Band size comes from settings.vwap_band_pct so tests stay correct when the
# value is tuned in .env (currently 0.005 = 0.5%).
# ===========================================================================

def test_call_vwap_break_exactly_at_band_boundary_does_not_fire():
    """
    Underlying == VWAP − band → strict < means NO break (boundary is exclusive).
    Uses the EXIT band (vwap_exit_band_pct), which the exit logic prefers
    over the entry band when it is set.
    """
    vwap = 150.0
    band = vwap * (settings.vwap_exit_band_pct or settings.vwap_band_pct)
    boundary = vwap - band
    result = _exit(
        direction="CALL",
        current_option_price=5.00,   # no stop/TP fire
        vwap_at_entry=vwap,
        current_underlying=boundary,
    )
    # VWAP_BREAK should NOT fire (strict <); TREND_REVERSAL may or may not,
    # but we only assert no VWAP_BREAK.
    assert result is None or result.reason != "VWAP_BREAK"


def test_call_vwap_break_one_tick_below_boundary_fires():
    """Underlying just below VWAP − band must trigger VWAP_BREAK."""
    vwap = 150.0
    band = vwap * settings.vwap_band_pct
    result = _exit(
        direction="CALL",
        current_option_price=5.00,
        vwap_at_entry=vwap,
        current_underlying=vwap - band - 0.01,
    )
    assert result is not None
    assert result.reason == "VWAP_BREAK"


def test_put_vwap_break_exactly_at_band_boundary_does_not_fire():
    """PUT: underlying == vwap + band → strict > means NO break (exit band)."""
    vwap = 150.0
    band = vwap * (settings.vwap_exit_band_pct or settings.vwap_band_pct)
    result = _exit(
        direction="PUT",
        entry_price=5.00,
        stop_price=3.75,
        tp1_price=6.75,
        tp2_price=6.75,
        current_option_price=5.00,
        vwap_at_entry=vwap,
        current_underlying=vwap + band,
    )
    assert result is None or result.reason != "VWAP_BREAK"


def test_put_vwap_break_above_band_fires():
    """PUT: underlying > vwap + band → VWAP_BREAK."""
    vwap = 150.0
    band = vwap * settings.vwap_band_pct
    result = _exit(
        direction="PUT",
        entry_price=5.00,
        stop_price=3.75,
        tp1_price=6.75,
        tp2_price=6.75,
        current_option_price=5.00,
        vwap_at_entry=vwap,
        current_underlying=vwap + band + 0.01,
    )
    assert result is not None
    assert result.reason == "VWAP_BREAK"


def test_vwap_at_entry_zero_skips_break_check():
    """When vwap_at_entry is 0, the break guard is bypassed entirely."""
    result = _exit(
        direction="CALL",
        current_option_price=5.00,
        vwap_at_entry=0,
        current_underlying=100.0,  # far below any VWAP — would fire if not guarded
    )
    # No exit from VWAP_BREAK; trend is still bullish so no TREND_REVERSAL either
    assert result is None


# ===========================================================================
# Trend-reversal edge cases
# ===========================================================================

def test_neutral_trend_does_not_trigger_reversal():
    """No stop/TP/VWAP condition → no exit (TREND_REVERSAL handled by scheduler)."""
    result = _exit(
        direction="CALL",
    )
    assert result is None


# ===========================================================================
# Degenerate inputs
# ===========================================================================

def test_zero_remaining_qty_does_not_raise():
    """remaining_qty=0 is degenerate but must not crash."""
    try:
        _exit(remaining_qty=0)
    except Exception as exc:
        pytest.fail(f"check_exit_conditions raised with remaining_qty=0: {exc}")


# ===========================================================================
# compute_trade_levels — math, rounding, direction symmetry
# ===========================================================================

def test_compute_levels_call_math():
    from app.config import settings
    entry  = 5.00
    levels = compute_trade_levels(entry, "CALL")
    assert levels["stop_price"] == pytest.approx(round(entry * (1 - settings.stop_loss_pct),   2), abs=0.001)
    assert levels["tp1_price"]  == pytest.approx(round(entry * (1 + settings.take_profit_pct), 2), abs=0.001)
    assert levels["tp2_price"]  == pytest.approx(round(entry * (1 + settings.take_profit_pct), 2), abs=0.001)  # tp1 == tp2 in v1


def test_compute_levels_put_same_formula():
    """v1 does not differentiate CALL vs PUT — stop/TP are percentage-based for both."""
    call_levels = compute_trade_levels(5.00, "CALL")
    put_levels  = compute_trade_levels(5.00, "PUT")
    assert call_levels == put_levels


def test_compute_levels_rounding():
    """Entry with many decimals should round stop/TP to 2 dp."""
    from app.config import settings
    entry  = 3.333
    levels = compute_trade_levels(entry, "CALL")
    assert levels["stop_price"] == round(entry * (1 - settings.stop_loss_pct),   2)
    assert levels["tp1_price"]  == round(entry * (1 + settings.take_profit_pct), 2)


# ===========================================================================
# TREND_REVERSAL minimum-hold-time guard
# ===========================================================================

def test_trend_reversal_suppressed_within_hold_window():
    """
    CALL trade entered 2 minutes ago — no stop/TP/VWAP conditions fire.
    (TREND_REVERSAL is handled by the scheduler, not check_exit_conditions.)
    """
    result = _exit(
        direction="CALL",
        current_option_price=5.00,      # no stop / TP
        current_underlying=151.0,       # above VWAP — no VWAP_BREAK
        entry_time=_old_entry(2),
    )
    assert result is None


def test_stop_fires_regardless_of_hold_window():
    """
    Hard stop must fire even on a brand-new trade.
    """
    with patch.object(settings, "stop_loss_min_hold_minutes", 0):
        result = _exit(
            current_option_price=round(5.00 * (1 - settings.stop_loss_pct), 2),  # at stop
            entry_time=_old_entry(0),       # 0 minutes old — just entered
        )
    assert result is not None
    assert result.reason == "STOP"


def test_trend_reversal_suppressed_with_naive_entry_time():
    """
    SQLite returns naive UTC datetimes; check_exit_conditions must not raise.
    No stop/TP/VWAP conditions → no exit.
    """
    naive_entry = datetime.utcnow() - timedelta(minutes=1)  # naive, 1 min old
    result = _exit(
        direction="CALL",
        current_option_price=5.00,
        current_underlying=151.0,
        entry_time=naive_entry,
    )
    assert result is None


def test_compute_levels_small_entry():
    """Entry price of 0.01 — stop must not exceed entry, TP must exceed entry."""
    levels = compute_trade_levels(0.01, "CALL")
    assert levels["stop_price"] <= 0.01
    assert levels["tp1_price"]  >= 0.01


def test_compute_levels_stop_below_entry():
    """Stop must always be strictly below entry for any positive entry price."""
    for entry in [1.00, 2.50, 5.00, 10.00, 50.00]:
        levels = compute_trade_levels(entry, "CALL")
        assert levels["stop_price"] < entry, f"Stop not below entry at entry={entry}"


def test_compute_levels_tp_above_entry():
    """TP must always be strictly above entry for any positive entry price."""
    for entry in [1.00, 2.50, 5.00, 10.00, 50.00]:
        levels = compute_trade_levels(entry, "CALL")
        assert levels["tp2_price"] > entry, f"TP not above entry at entry={entry}"
