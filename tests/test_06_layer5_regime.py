"""
Tests for Layer 5: get_market_regime() — SPY trend cache + gate logic.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta
from tests.conftest import rising_bars, falling_bars, flat_bars
from app.services import strategy as strat_module


@pytest.fixture(autouse=True)
def reset_regime_cache():
    """Reset the in-module cache before each test."""
    strat_module._regime_cache["trend"]      = None
    strat_module._regime_cache["fetched_at"] = None
    yield
    strat_module._regime_cache["trend"]      = None
    strat_module._regime_cache["fetched_at"] = None


@pytest.mark.asyncio
async def test_regime_gate_disabled_returns_neutral(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", False)
    mock_client = MagicMock()
    result = await strat_module.get_market_regime(mock_client)
    assert result == "neutral"
    mock_client.get_intraday_bars.assert_not_called()

@pytest.mark.asyncio
async def test_regime_returns_bullish_when_spy_rising(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(
        return_value=rising_bars(base=450.0, n=25, step=0.5)
    )
    result = await strat_module.get_market_regime(mock_client)
    assert result == "bullish"

@pytest.mark.asyncio
async def test_regime_returns_bearish_when_spy_falling(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(
        return_value=falling_bars(base=450.0, n=25, step=0.5)
    )
    result = await strat_module.get_market_regime(mock_client)
    assert result == "bearish"

@pytest.mark.asyncio
async def test_regime_cached_avoids_second_api_call(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    monkeypatch.setattr(strat_module.settings, "regime_gate_ttl_seconds", 300)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(
        return_value=rising_bars(base=450.0, n=25, step=0.5)
    )
    # First call fetches
    r1 = await strat_module.get_market_regime(mock_client)
    # Second call should use cache
    r2 = await strat_module.get_market_regime(mock_client)
    assert r1 == r2
    assert mock_client.get_intraday_bars.call_count == 1

@pytest.mark.asyncio
async def test_regime_cache_expires(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    monkeypatch.setattr(strat_module.settings, "regime_gate_ttl_seconds", 1)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(
        return_value=rising_bars(base=450.0, n=25, step=0.5)
    )
    await strat_module.get_market_regime(mock_client)
    # Manually expire the cache
    strat_module._regime_cache["fetched_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(seconds=10)
    )
    await strat_module.get_market_regime(mock_client)
    assert mock_client.get_intraday_bars.call_count == 2

@pytest.mark.asyncio
async def test_regime_api_failure_returns_neutral(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(side_effect=Exception("Tradier down"))
    result = await strat_module.get_market_regime(mock_client)
    assert result == "neutral"

@pytest.mark.asyncio
async def test_regime_empty_bars_returns_neutral(monkeypatch):
    monkeypatch.setattr(strat_module.settings, "regime_gate_enabled", True)
    mock_client = MagicMock()
    mock_client.get_intraday_bars = AsyncMock(return_value=[])
    result = await strat_module.get_market_regime(mock_client)
    assert result == "neutral"
