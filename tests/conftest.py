"""
Shared fixtures for all test modules.
"""
import os, sys, asyncio

# Point to ajoy root so `from app.xxx import` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use a fresh in-process DB for every test session
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_ajoy.db")

import pytest
import pytest_asyncio

# ── pytest-asyncio config ───────────────────────────────────────────────────
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: async test")

# ── Shared Bar factory ───────────────────────────────────────────────────────
from datetime import datetime
from app.services.tradier import Bar

def make_bar(close: float, open_: float = None, high: float = None,
             low: float = None, volume: int = 500_000, ts=None) -> Bar:
    """Create a synthetic OHLCV bar."""
    o = open_ if open_ is not None else close
    return Bar(
        time=ts or datetime(2024, 1, 15, 10, 0),
        open=o,
        high=high if high is not None else max(o, close),
        low=low  if low  is not None else min(o, close),
        close=close,
        volume=volume,
    )

def rising_bars(base: float = 150.0, n: int = 30, step: float = 0.05) -> list:
    """n bars with rising closes and green bodies (close > open)."""
    bars = []
    for i in range(n):
        c = round(base + i * step, 4)
        o = round(c - 0.02, 4)          # green candle: open below close
        bars.append(make_bar(c, open_=o))
    return bars

def falling_bars(base: float = 150.0, n: int = 30, step: float = 0.05) -> list:
    """n bars with falling closes and red bodies (close < open)."""
    bars = []
    for i in range(n):
        c = round(base - i * step, 4)
        o = round(c + 0.02, 4)          # red candle: open above close
        bars.append(make_bar(c, open_=o))
    return bars

def flat_bars(price: float = 150.0, n: int = 30) -> list:
    """n doji bars (open == close, no momentum)."""
    return [make_bar(price, open_=price) for _ in range(n)]
