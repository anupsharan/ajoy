"""
Tests for pure math helpers: calculate_vwap, calculate_ema, ema_direction.
"""
import math
import pytest
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import calculate_vwap, calculate_ema, ema_direction


# ── calculate_vwap ────────────────────────────────────────────────────────────

def test_vwap_empty():
    assert calculate_vwap([]) == 0.0

def test_vwap_zero_volume():
    bars = [make_bar(100.0, volume=0), make_bar(110.0, volume=0)]
    assert calculate_vwap(bars) == 0.0

def test_vwap_single_bar():
    # typical_price = (H + L + C) / 3 = (105 + 95 + 100) / 3 = 100
    b = make_bar(100.0, high=105.0, low=95.0, volume=1000)
    assert abs(calculate_vwap([b]) - 100.0) < 0.001

def test_vwap_two_equal_bars():
    b1 = make_bar(100.0, high=105.0, low=95.0, volume=1000)
    b2 = make_bar(100.0, high=105.0, low=95.0, volume=1000)
    assert abs(calculate_vwap([b1, b2]) - 100.0) < 0.001

def test_vwap_weighted_towards_volume():
    # Low-price bar has 10x more volume → VWAP should be closer to 100
    b1 = make_bar(100.0, volume=10_000)
    b2 = make_bar(200.0, volume=1_000)
    vwap = calculate_vwap([b1, b2])
    assert vwap < 120.0   # closer to 100 than to 200

def test_vwap_rising_session():
    bars = rising_bars(base=150.0, n=30)
    vwap = calculate_vwap(bars)
    # VWAP should be between first and last close
    assert bars[0].close < vwap < bars[-1].close


# ── calculate_ema ─────────────────────────────────────────────────────────────

def test_ema_empty():
    assert calculate_ema([], 9) == []

def test_ema_invalid_period():
    assert calculate_ema([1.0, 2.0], 0) == []

def test_ema_length_matches_input():
    prices = [float(i) for i in range(1, 21)]
    result = calculate_ema(prices, period=9)
    assert len(result) == len(prices)

def test_ema_first_values_nan():
    prices = [float(i) for i in range(1, 21)]
    result = calculate_ema(prices, period=9)
    # First (period-1) = 8 values should be NaN
    for v in result[:8]:
        assert math.isnan(v)

def test_ema_seed_is_sma():
    prices = [10.0] * 9 + [10.0]   # flat prices
    result = calculate_ema(prices, period=9)
    # SMA of first 9 identical values = 10.0
    assert abs(result[8] - 10.0) < 1e-9

def test_ema_converges_upward():
    prices = [100.0] * 9 + [110.0] * 10   # step up
    result = calculate_ema(prices, period=9)
    valid = [v for v in result if not math.isnan(v)]
    # EMA should rise after the step but not immediately reach 110
    assert valid[-1] > valid[0]
    assert valid[-1] < 110.0


# ── ema_direction ─────────────────────────────────────────────────────────────

def test_ema_direction_neutral_too_few_bars():
    bars = rising_bars(n=5)
    assert ema_direction(bars, period=9) == "neutral"

def test_ema_direction_bullish():
    # Price well above EMA → bullish
    bars = rising_bars(base=100.0, n=30, step=0.5)
    result = ema_direction(bars, period=9)
    assert result == "bullish"

def test_ema_direction_bearish():
    bars = falling_bars(base=200.0, n=30, step=0.5)
    result = ema_direction(bars, period=9)
    assert result == "bearish"

def test_ema_direction_flat():
    bars = flat_bars(price=150.0, n=20)
    # Flat bars: price == EMA throughout → neutral
    result = ema_direction(bars, period=9)
    assert result == "neutral"
