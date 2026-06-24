"""
Tests for Tradier client helpers: get_atm_iv().
These use no HTTP — either pure logic or mocked responses.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from datetime import date, timedelta
from app.services.tradier import TradierClient, OptionQuote


def make_option(strike: float, direction: str, iv: float = 0.45,
                bid: float = 1.0, ask: float = 1.20, volume: int = 500):
    return OptionQuote(
        symbol=f"AAPL{strike}{direction[0].upper()}",
        underlying="AAPL",
        expiration_date="2024-01-19",
        option_type=direction.lower(),
        strike=strike,
        bid=bid, ask=ask, last=1.10,
        volume=volume, open_interest=1000,
        iv=iv,
    )


# ── get_atm_iv ────────────────────────────────────────────────────────────────

class TestGetAtmIv:
    def setup_method(self):
        self.client = TradierClient.__new__(TradierClient)

    def _chain(self):
        return [
            make_option(145, "call", iv=0.30),
            make_option(150, "call", iv=0.40),   # ATM for price=150
            make_option(155, "call", iv=0.35),
            make_option(145, "put",  iv=0.31),
            make_option(150, "put",  iv=0.42),
            make_option(155, "put",  iv=0.36),
        ]

    def test_call_atm_iv_returned(self):
        iv = self.client.get_atm_iv(self._chain(), "CALL", 150.0)
        assert iv == 0.40

    def test_put_atm_iv_returned(self):
        iv = self.client.get_atm_iv(self._chain(), "PUT", 150.0)
        assert iv == 0.42

    def test_nearest_strike_chosen(self):
        # Price 151 → closest call is 150
        iv = self.client.get_atm_iv(self._chain(), "CALL", 151.0)
        assert iv == 0.40

    def test_empty_chain_returns_none(self):
        assert self.client.get_atm_iv([], "CALL", 150.0) is None

    def test_no_matching_direction_returns_none(self):
        calls_only = [make_option(150, "call", iv=0.40)]
        assert self.client.get_atm_iv(calls_only, "PUT", 150.0) is None

    def test_zero_iv_returns_none(self):
        chain = [make_option(150, "call", iv=0.0)]
        assert self.client.get_atm_iv(chain, "CALL", 150.0) is None

    def test_none_iv_returns_none(self):
        opt = make_option(150, "call")
        opt.iv = None
        assert self.client.get_atm_iv([opt], "CALL", 150.0) is None
