"""
Tests for Layer 5: get_regime_from_vwap() — QQQ VWAP position regime gate.

The regime gate was redesigned from an async SPY EMA cache to a synchronous
function that takes pre-fetched QQQ 1-min bars and computes regime from
QQQ's position vs its session VWAP.  No API call, no cache.
"""
import pytest
from unittest.mock import patch
from tests.conftest import rising_bars, falling_bars, flat_bars
from app.services.strategy import get_regime_from_vwap
from app.config import settings


def _bullish_bars():
    """QQQ bars where last close is clearly above VWAP."""
    return rising_bars(base=450.0, n=60, step=0.10)


def _bearish_bars():
    """QQQ bars where last close is clearly below VWAP."""
    return falling_bars(base=460.0, n=60, step=0.10)


def _neutral_bars():
    """QQQ bars where last close is very close to VWAP (flat session)."""
    return flat_bars(price=450.0, n=60)


# ---------------------------------------------------------------------------
# Gate disabled
# ---------------------------------------------------------------------------

def test_regime_gate_disabled_returns_neutral(monkeypatch):
    monkeypatch.setattr(settings, "regime_gate_enabled", False)
    result = get_regime_from_vwap(_bullish_bars())
    assert result == "neutral"


# ---------------------------------------------------------------------------
# Bullish / bearish / neutral classification
# ---------------------------------------------------------------------------

def test_regime_returns_bullish_when_qqq_above_vwap():
    """Rising session → last close well above VWAP → bullish."""
    with patch.object(settings, "regime_gate_enabled", True):
        result = get_regime_from_vwap(_bullish_bars())
    assert result == "bullish"


def test_regime_returns_bearish_when_qqq_below_vwap():
    """Falling session → last close well below VWAP → bearish."""
    with patch.object(settings, "regime_gate_enabled", True):
        result = get_regime_from_vwap(_bearish_bars())
    assert result == "bearish"


def test_regime_returns_neutral_when_flat():
    """Flat session → last close at VWAP within threshold → neutral."""
    with patch.object(settings, "regime_gate_enabled", True):
        result = get_regime_from_vwap(_neutral_bars())
    assert result == "neutral"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_regime_empty_bars_returns_neutral():
    with patch.object(settings, "regime_gate_enabled", True):
        result = get_regime_from_vwap([])
    assert result == "neutral"


def test_regime_none_bars_returns_neutral():
    with patch.object(settings, "regime_gate_enabled", True):
        result = get_regime_from_vwap(None)
    assert result == "neutral"


def test_regime_threshold_boundary_neutral(monkeypatch):
    """
    QQQ exactly at the regime threshold boundary → classified as neutral
    since we need to be STRICTLY outside threshold to declare bullish/bearish.
    """
    monkeypatch.setattr(settings, "regime_gate_enabled", True)
    monkeypatch.setattr(settings, "regime_vwap_threshold", 0.005)  # 0.5%
    # flat_bars gives last close == VWAP (diff_pct ≈ 0) → neutral
    result = get_regime_from_vwap(_neutral_bars())
    assert result == "neutral"
