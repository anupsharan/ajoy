"""
Tests for exit logic: check_exit_conditions(), compute_trade_levels().

v1 uses simple single-target exits:
  stop  = entry × (1 − STOP_LOSS_PCT)
  tp    = entry × (1 + TAKE_PROFIT_PCT)

Tests use settings values directly so they stay valid across .env changes.
"""
import pytest
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.config import settings
from app.services.strategy import (
    check_exit_conditions,
    compute_trade_levels,
)


# ── compute_trade_levels ──────────────────────────────────────────────────────

def test_trade_levels_call():
    """stop = entry*(1-stop_loss_pct),  tp = entry*(1+take_profit_pct)."""
    entry  = 5.00
    levels = compute_trade_levels(entry, "CALL")
    assert levels["stop_price"] == round(entry * (1 - settings.stop_loss_pct), 2)
    assert levels["tp1_price"]  == round(entry * (1 + settings.take_profit_pct), 2)
    assert levels["tp2_price"]  == round(entry * (1 + settings.take_profit_pct), 2)

def test_trade_levels_tp_equals_tp1_and_tp2():
    """tp1_price and tp2_price are always equal in v1."""
    levels = compute_trade_levels(10.00, "PUT")
    assert levels["stop_price"] < 10.00
    assert levels["tp1_price"]  > 10.00
    assert levels["tp1_price"]  == levels["tp2_price"]

def test_trade_levels_stop_is_below_entry():
    entry  = 8.00
    levels = compute_trade_levels(entry, "CALL")
    assert levels["stop_price"] == round(entry * (1 - settings.stop_loss_pct), 2)

def test_trade_levels_tp_is_above_entry():
    entry  = 8.00
    levels = compute_trade_levels(entry, "CALL")
    assert levels["tp2_price"] == round(entry * (1 + settings.take_profit_pct), 2)


# ── check_exit_conditions ─────────────────────────────────────────────────────
# entry=5.00; stop/tp derived from settings so the suite stays valid across
# .env changes.  A stop ABOVE the original level is labeled TRAILING_STOP.

ENTRY = 5.00
ORIGINAL_STOP = round(ENTRY * (1 - settings.stop_loss_pct), 2)

BASE = dict(
    direction="CALL",
    entry_price=ENTRY,
    stop_price=ORIGINAL_STOP,
    tp1_price=6.75,
    tp2_price=6.75,
    tp1_hit=False,
    vwap_at_entry=150.0,
    current_underlying=151.0,   # still above VWAP
    bars_15m=rising_bars(base=100.0, n=30, step=0.5),
    remaining_qty=10,
)

def make_exit(**overrides):
    return check_exit_conditions(**{**BASE, **overrides})


def test_no_exit_conditions_met():
    """Price between stop and target — no exit triggered."""
    result = make_exit(current_option_price=5.50)
    assert result is None

def test_stop_loss_fires():
    result = make_exit(current_option_price=ORIGINAL_STOP - 0.10)
    assert result is not None
    assert result.reason == "STOP"
    assert result.close_all is True

def test_stop_loss_exactly_at_stop():
    result = make_exit(current_option_price=ORIGINAL_STOP)
    assert result.reason == "STOP"

def test_raised_stop_labeled_trailing_stop():
    """A stop above the original entry-derived level is a trailing stop."""
    raised = round(ENTRY * 1.01, 2)   # profit-lock stop above entry
    result = make_exit(stop_price=raised, current_option_price=raised - 0.05)
    assert result is not None
    assert result.reason == "TRAILING_STOP"
    assert result.close_all is True

def test_profit_target_fires_full_close():
    """v1: hitting the target closes 100 % of the position."""
    result = make_exit(current_option_price=6.80)
    assert result is not None
    assert result.reason == "TP2"
    assert result.close_all is True

def test_profit_target_exactly_at_tp():
    result = make_exit(current_option_price=6.75)
    assert result.reason == "TP2"

def test_price_just_below_target_no_exit():
    result = make_exit(current_option_price=6.74)
    assert result is None

def test_vwap_break_call():
    """Underlying drops well below VWAP for a CALL → VWAP_BREAK."""
    result = make_exit(
        current_option_price=5.20,
        current_underlying=148.0,   # clearly below VWAP 150
    )
    assert result is not None
    assert result.reason == "VWAP_BREAK"

def test_vwap_break_put():
    result = make_exit(
        direction="PUT",
        current_option_price=5.20,
        current_underlying=152.5,   # above VWAP 150 → break for PUT
        bars_15m=falling_bars(base=200.0, n=30, step=0.5),
    )
    assert result.reason == "VWAP_BREAK"

def test_no_vwap_break_within_band():
    """Underlying only slightly below VWAP — within band, no break."""
    result = make_exit(
        current_option_price=5.20,
        current_underlying=149.85,  # 0.1% below VWAP — within 0.2% band
    )
    assert result is None

def test_trend_reversal_call_blocked_by_bearish():
    # underlying just below VWAP (149.9 < 150) so Guard C passes,
    # but still above VWAP_BREAK threshold (150 - 1%*150 = 148.5)
    result = make_exit(
        current_option_price=5.20,
        bars_15m=falling_bars(base=200.0, n=30, step=0.5),  # bearish trend
        current_underlying=149.9,   # just below VWAP — Guard C satisfied
    )
    assert result.reason == "TREND_REVERSAL"

def test_trend_reversal_put_blocked_by_bullish():
    # underlying just above VWAP (150.1 > 150) so Guard C passes for PUT,
    # but still below VWAP_BREAK threshold (150 + 1%*150 = 151.5)
    result = make_exit(
        direction="PUT",
        current_option_price=5.20,
        bars_15m=rising_bars(base=100.0, n=30, step=0.5),   # bullish trend
        current_underlying=150.1,   # just above VWAP — Guard C satisfied
        vwap_at_entry=150.0,
    )
    assert result.reason == "TREND_REVERSAL"

def test_stop_priority_over_vwap_break():
    """Stop loss takes priority over VWAP break."""
    result = make_exit(
        current_option_price=ORIGINAL_STOP - 0.15,   # below stop
        current_underlying=147.0,    # also a VWAP break
    )
    assert result.reason == "STOP"

def test_stop_priority_over_trend_reversal():
    """Stop loss fires before trend reversal check."""
    result = make_exit(
        current_option_price=ORIGINAL_STOP - 0.15,
        bars_15m=falling_bars(base=200.0, n=30, step=0.5),
        current_underlying=151.0,
    )
    assert result.reason == "STOP"


# ── Old TP1/TP2 staged-exit tests (commented out — not active in v1) ─────────
# Uncomment along with the TP1/TP2 logic in strategy.py and config.py.
#
# def test_tp1_fires_partial():
#     result = make_exit(current_option_price=6.60, tp1_hit=False)
#     assert result.reason == "TP1"
#     assert result.close_all is False
#     assert result.quantity > 0
#
# def test_tp1_partial_qty_is_50_pct():
#     result = make_exit(current_option_price=6.60, remaining_qty=10)
#     assert result.quantity == 5
#
# def test_tp2_fires_after_tp1():
#     result = make_exit(current_option_price=8.10, tp1_hit=True)
#     assert result.reason == "TP2"
#     assert result.close_all is True
#
# def test_tp2_blocked_before_tp1():
#     result = make_exit(current_option_price=8.10, tp1_hit=False)
#     assert result.reason == "TP1"   # TP1 takes priority
#
# def test_breakeven_stop():
#     from app.services.strategy import compute_breakeven_stop
#     be = compute_breakeven_stop(5.00)
#     assert be == 5.10
