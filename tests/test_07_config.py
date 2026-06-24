"""
Tests for config.py: default values and new Layer 5/6/guard fields.
"""
import pytest
from app.config import Settings


def test_config_defaults():
    """
    Structural sanity checks.  Exact values come from .env and change whenever
    the user tunes settings in the UI, so we assert types/ranges — not values.
    """
    s = Settings()
    # URLs
    assert s.tradier_base_url.startswith("https://")
    assert s.tradier_base_url_sandbox.startswith("https://")
    # Scheduler intervals are positive
    assert s.scan_interval_seconds > 0
    assert s.manage_interval_seconds > 0
    # Trading window strings parse as HH:MM
    for t in (s.trading_start_time, s.trading_end_time, s.last_entry_time):
        h, m = t.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    # Position sizing / risk
    assert s.max_open_trades >= 1
    assert s.amount_per_trade > 0
    assert s.max_daily_loss > 0
    assert s.risk_per_trade >= 0
    assert isinstance(s.broker_stop_enabled, bool)
    # Risk/reward percentages are sane fractions
    assert 0 < s.stop_loss_pct < 1
    assert 0 < s.take_profit_pct < 1
    # Strategy params
    assert s.ema_period >= 2
    assert s.bounce_bars_required >= 1
    assert 0 < s.vwap_band_pct < 0.1
    assert s.trend_lookback_days >= 1
    # Layer 5 / 6
    assert isinstance(s.regime_gate_enabled, bool)
    assert s.regime_gate_symbol
    assert s.iv_max_threshold > 0
    # Guards
    assert s.cooldown_minutes >= 0
    assert s.max_losses_per_symbol_per_day >= 0
    assert s.max_trades_per_symbol_per_day >= 0


def test_config_property_aliases():
    """HH:MM property aliases must agree with the parsed source strings."""
    s = Settings()
    assert s.cutoff_hour       == int(s.trading_end_time.split(":")[0])
    assert s.cutoff_minute     == int(s.trading_end_time.split(":")[1])
    assert s.start_hour        == int(s.trading_start_time.split(":")[0])
    assert s.start_minute      == int(s.trading_start_time.split(":")[1])
    assert s.last_entry_hour   == int(s.last_entry_time.split(":")[0])
    assert s.last_entry_minute == int(s.last_entry_time.split(":")[1])


def test_config_risk_reward_math():
    """stop = entry*(1-stop_loss_pct), tp = entry*(1+take_profit_pct)."""
    from app.services.strategy import compute_trade_levels
    from app.config import settings
    levels = compute_trade_levels(5.00, "CALL")
    # Use settings values so test stays valid across .env changes
    assert levels["stop_price"] == round(5.00 * (1 - settings.stop_loss_pct), 2)
    assert levels["tp1_price"]  == round(5.00 * (1 + settings.take_profit_pct), 2)
    assert levels["tp2_price"]  == round(5.00 * (1 + settings.take_profit_pct), 2)


# ── Old TP1/TP2 assertions (commented out — not active in v1) ────────────────
# def test_config_tp1_tp2_defaults():
#     s = Settings()
#     assert s.tp1_multiplier       == 1.5
#     assert s.tp2_multiplier       == 3.0
#     assert s.tp1_exit_pct         == 0.50
#     assert s.breakeven_buffer_pct == 0.02


# ---------------------------------------------------------------------------
# Lunch-break gate tests
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch

ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int) -> datetime:
    """Return a UTC datetime that maps to HH:MM America/New_York on a trading weekday."""
    # Use a known Wednesday so weekday() == 2 (market open)
    from datetime import date
    # 2024-01-10 was a Wednesday; combine with ET time then convert to UTC
    naive_et = datetime(2024, 1, 10, hour, minute, 0)
    et_aware = naive_et.replace(tzinfo=ET)
    return et_aware.astimezone(timezone.utc)


def test_lunch_break_blocks_entry():
    """is_in_trading_window() should return False when inside lunch window."""
    from app.services.strategy import is_in_trading_window
    from app.config import settings

    # 11:45 ET is inside the default 11:30–12:15 window
    ts = _et(11, 45)
    with patch.object(settings, "lunch_break_enabled", True), \
         patch.object(settings, "lunch_break_start", "11:30"), \
         patch.object(settings, "lunch_break_end", "12:15"):
        assert is_in_trading_window(ts) is False


def test_lunch_break_allows_before_window():
    """is_in_trading_window() should return True before lunch starts."""
    from app.services.strategy import is_in_trading_window
    from app.config import settings

    # 11:29 ET — one minute before lunch
    ts = _et(11, 29)
    with patch.object(settings, "lunch_break_enabled", True), \
         patch.object(settings, "lunch_break_start", "11:30"), \
         patch.object(settings, "lunch_break_end", "12:15"):
        assert is_in_trading_window(ts) is True


def test_lunch_break_allows_after_window():
    """is_in_trading_window() should return True once lunch ends."""
    from app.services.strategy import is_in_trading_window
    from app.config import settings

    # 12:15 ET — exactly at the end boundary (exclusive end)
    ts = _et(12, 15)
    with patch.object(settings, "lunch_break_enabled", True), \
         patch.object(settings, "lunch_break_start", "11:30"), \
         patch.object(settings, "lunch_break_end", "12:15"):
        assert is_in_trading_window(ts) is True


def test_lunch_break_disabled_passes_through():
    """When LUNCH_BREAK_ENABLED=0 the gate must not block any intraday time."""
    from app.services.strategy import is_in_trading_window
    from app.config import settings

    # 11:50 ET — inside window, but gate is off
    ts = _et(11, 50)
    with patch.object(settings, "lunch_break_enabled", False), \
         patch.object(settings, "lunch_break_start", "11:30"), \
         patch.object(settings, "lunch_break_end", "12:15"):
        assert is_in_trading_window(ts) is True
