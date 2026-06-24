"""
Tests for Layer 1: check_entry_signal()
Verifies: trend_15min AND price_vs_vwap AND pullback_to_vwap
"""
import pytest
from datetime import datetime
from unittest.mock import patch
from tests.conftest import make_bar, rising_bars, falling_bars, flat_bars
from app.services.strategy import check_entry_signal, calculate_vwap
from app.config import settings


def _make_setup(trend: str, near_vwap: bool = True, correct_side: bool = True):
    """
    Build bars_1m and bars_15m for a given scenario.
    trend: 'bullish' | 'bearish'
    near_vwap: whether current price is within the VWAP band
    correct_side: whether price is on the correct VWAP side
    """
    if trend == "bullish":
        bars_15m = rising_bars(base=100.0, n=30, step=0.5)
        vwap_approx = 104.75  # rough VWAP of a rising series starting at 100
        if near_vwap and correct_side:
            # price slightly above VWAP — valid CALL pullback
            bars_1m = rising_bars(base=vwap_approx + 0.05, n=30, step=0.01)
        elif near_vwap and not correct_side:
            # price slightly below VWAP — wrong side for bullish
            bars_1m = falling_bars(base=vwap_approx - 0.05, n=30, step=0.01)
        else:
            # price far above VWAP — outside band
            bars_1m = rising_bars(base=vwap_approx + 10.0, n=30, step=0.01)
    else:  # bearish
        bars_15m = falling_bars(base=200.0, n=30, step=0.5)
        vwap_approx = 195.25
        if near_vwap and correct_side:
            bars_1m = falling_bars(base=vwap_approx - 0.05, n=30, step=0.01)
        elif near_vwap and not correct_side:
            bars_1m = rising_bars(base=vwap_approx + 0.05, n=30, step=0.01)
        else:
            bars_1m = falling_bars(base=vwap_approx - 10.0, n=30, step=0.01)
    return bars_1m, bars_15m


def test_l1_returns_none_on_empty_bars():
    assert check_entry_signal([], []) is None
    assert check_entry_signal(rising_bars(), []) is None
    assert check_entry_signal([], rising_bars()) is None

def test_l1_call_signal_valid():
    """Bullish 15m trend + price just above VWAP → CALL signal."""
    bars_15m = rising_bars(base=100.0, n=30, step=0.5)
    # Compute what VWAP will actually be
    from app.services.strategy import calculate_vwap
    # Build 1m bars that land right at VWAP level + tiny buffer
    vwap = calculate_vwap(bars_15m)
    bars_1m = [make_bar(vwap * 1.001, open_=vwap * 1.0005) for _ in range(30)]

    sig = check_entry_signal(bars_1m, bars_15m)
    assert sig is not None
    assert sig.direction == "CALL"
    assert sig.vwap > 0
    assert sig.trend == "bullish"

def test_l1_put_signal_valid():
    """Bearish 15m trend + price just below VWAP → PUT signal."""
    bars_15m = falling_bars(base=200.0, n=30, step=0.5)
    vwap = calculate_vwap(bars_15m)
    bars_1m = [make_bar(vwap * 0.999, open_=vwap * 0.9995) for _ in range(30)]

    sig = check_entry_signal(bars_1m, bars_15m)
    assert sig is not None
    assert sig.direction == "PUT"
    assert sig.trend == "bearish"

def test_l1_neutral_trend_blocks():
    """Flat 15m bars → neutral trend → no signal."""
    bars_15m = flat_bars(price=150.0, n=30)
    bars_1m  = flat_bars(price=150.0, n=30)
    assert check_entry_signal(bars_1m, bars_15m) is None

def test_l1_price_outside_vwap_band_blocks():
    """Price far from VWAP (not pulling back) → no signal.

    L1 computes VWAP from bars_1m (the session bars), so we need to anchor
    VWAP at one level (many bars at 150) and have the last bar jump far away.
    """
    bars_15m = rising_bars(base=100.0, n=30, step=0.5)
    # Anchor session VWAP near 150, last bar spikes to 158 (5%+ away)
    bars_1m = [make_bar(150.0)] * 25 + [make_bar(158.0)]
    assert check_entry_signal(bars_1m, bars_15m) is None

def test_l1_wrong_vwap_side_blocks():
    """Bullish trend but price below session VWAP → not a bounce, blocked.

    Anchor VWAP near 150 (many bars at 150), then last bar dips to 149.85
    (just below VWAP, still within the 0.2% band but on the wrong side).
    """
    bars_15m = rising_bars(base=100.0, n=30, step=0.5)
    bars_1m  = [make_bar(150.0)] * 25 + [make_bar(149.85)]
    assert check_entry_signal(bars_1m, bars_15m) is None

def test_l1_bearish_price_above_vwap_blocks():
    """Bearish trend but price above VWAP → blocked."""
    bars_15m = falling_bars(base=200.0, n=30, step=0.5)
    vwap = calculate_vwap(bars_15m)
    bars_1m = [make_bar(vwap * 1.001) for _ in range(30)]
    assert check_entry_signal(bars_1m, bars_15m) is None

def test_l1_too_few_15m_bars():
    """Fewer 15m bars than EMA period → neutral → no signal."""
    bars_15m = rising_bars(n=5)   # less than period=21+1=22
    bars_1m  = rising_bars(n=30)
    assert check_entry_signal(bars_1m, bars_15m) is None


# ---------------------------------------------------------------------------
# Valid CALL / PUT setup helpers
# ---------------------------------------------------------------------------

def _call_bars():
    """Rising bars_15m + bars_1m just above their VWAP — valid CALL setup."""
    bars_15m = rising_bars(base=100.0, n=40, step=0.5)
    vwap = calculate_vwap(bars_15m)
    bars_1m = [make_bar(vwap * 1.001) for _ in range(30)]
    return bars_1m, bars_15m


def _put_bars():
    """Falling bars_15m + bars_1m where last close is clearly below VWAP.

    Build 29 bars slightly ABOVE the target price, then 1 bar AT the target.
    This ensures VWAP(bars_1m) > last_close (correct PUT side) without relying
    on floating-point equality of 30 identical bars.
    """
    bars_15m = falling_bars(base=200.0, n=40, step=0.5)
    vwap_15m = calculate_vwap(bars_15m)
    target = round(vwap_15m * 0.999, 4)
    above  = round(target + 0.05, 4)   # slightly above → VWAP > target
    bars_1m = [make_bar(above) for _ in range(29)] + [make_bar(target)]
    return bars_1m, bars_15m


# ── Valid setup integration tests ────────────────────────────────────────────

def test_l1_valid_call_setup_produces_signal():
    """A clean rising-trend + VWAP-pullback setup must produce a CALL signal."""
    bars_1m, bars_15m = _call_bars()
    result = check_entry_signal(bars_1m, bars_15m)
    assert result is not None
    assert result.direction == "CALL"


def test_l1_valid_put_setup_produces_signal():
    """A clean falling-trend + VWAP-rejection setup must produce a PUT signal."""
    bars_1m, bars_15m = _put_bars()
    result = check_entry_signal(bars_1m, bars_15m)
    assert result is not None
    assert result.direction == "PUT"
