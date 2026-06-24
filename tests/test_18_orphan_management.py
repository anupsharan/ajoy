"""
Tests for orphan position management (non-API portion):
  1. _parse_option_symbol()  — OCC symbol parser in trades.py
  2. _manage_orphan_stops()  — scheduler auto-stop for untracked positions

See test_15_api_trades_full.py for POST /orphan/adopt HTTP-layer tests.
"""
import os, pytest, pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/ajoy_orphan_test.db")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Trade, TradeStatus, Direction
from app.routers.trades import _parse_option_symbol
from app.services.scheduler import _manage_orphan_stops
from app.services.tradier import Position


# ---------------------------------------------------------------------------
# DB fixture (isolated test DB)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_orphan_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. _parse_option_symbol — unit tests
# ---------------------------------------------------------------------------

class TestParseOptionSymbol:

    def test_standard_put(self):
        underlying, expiry, direction, strike = _parse_option_symbol("IWM260601P00290000")
        assert underlying == "IWM"
        assert expiry    == "2026-06-01"
        assert direction == "PUT"
        assert strike    == 290.0

    def test_standard_call(self):
        underlying, expiry, direction, strike = _parse_option_symbol("AAPL240119C00150000")
        assert underlying == "AAPL"
        assert expiry    == "2024-01-19"
        assert direction == "CALL"
        assert strike    == 150.0

    def test_spx_long_symbol(self):
        """SPX is a 3-char symbol — should parse correctly."""
        underlying, expiry, direction, strike = _parse_option_symbol("SPX250620C04500000")
        assert underlying == "SPX"
        assert expiry    == "2025-06-20"
        assert direction == "CALL"
        assert strike    == 4500.0

    def test_fractional_strike(self):
        """Option symbols can encode strikes like 123.50 as 00123500."""
        underlying, expiry, direction, strike = _parse_option_symbol("TSLA240322C00123500")
        assert underlying == "TSLA"
        assert direction  == "CALL"
        assert strike     == pytest.approx(123.5, abs=0.001)

    def test_spy_call(self):
        underlying, expiry, direction, strike = _parse_option_symbol("SPY260501C00580000")
        assert underlying == "SPY"
        assert expiry    == "2026-05-01"
        assert direction == "CALL"
        assert strike    == 580.0

    def test_invalid_symbol_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_option_symbol("NOT_A_SYMBOL")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _parse_option_symbol("")

    def test_lowercase_raises(self):
        """Symbol letters must be uppercase — lowercase should fail."""
        with pytest.raises(ValueError):
            _parse_option_symbol("iwm260601p00290000")


# ---------------------------------------------------------------------------
# 2. _manage_orphan_stops — scheduler unit tests
# ---------------------------------------------------------------------------

def _make_opt_quote(bid=None, ask=None, last=None):
    q = MagicMock()
    q.bid  = bid
    q.ask  = ask
    q.last = last
    return q


def _make_position(symbol: str, qty: int, cost_basis_total: float) -> Position:
    return Position(symbol=symbol, quantity=qty, cost_basis=cost_basis_total)


@pytest.mark.asyncio
class TestManageOrphanStops:
    """
    Tests for _manage_orphan_stops():
      - orphan above stop threshold: no sell placed
      - orphan below stop threshold: market sell placed
      - orphan already tracked by Ajoy: no sell placed
      - no positions: no sell placed
      - quote fetch fails: no sell placed (graceful)
      - loss exactly at threshold boundary: sell placed (>= threshold)
    """

    async def _run(self, db, positions, ajoy_trades, quotes):
        """
        Helper: add ajoy_trades to DB, then call _manage_orphan_stops()
        with a mocked client that returns `positions` and `quotes`.
        """
        for t in ajoy_trades:
            db.add(t)
        await db.commit()

        client = MagicMock()
        client.get_positions      = AsyncMock(return_value=positions)
        client.get_option_quote   = AsyncMock(side_effect=lambda sym: quotes.get(sym))
        client.place_option_order = AsyncMock(return_value=MagicMock(order_id="X001"))

        await _manage_orphan_stops(db, client)
        return client

    def _trade(self, option_symbol="AAPL240119C00150000"):
        return Trade(
            symbol="AAPL", option_symbol=option_symbol,
            direction=Direction.CALL, quantity=1, remaining_qty=1,
            entry_price=5.00,
            entry_time=datetime.now(tz=timezone.utc),
            status=TradeStatus.OPEN,
        )

    async def test_no_positions_no_sell(self, db):
        """No Tradier positions → place_option_order never called."""
        client = await self._run(db, positions=[], ajoy_trades=[], quotes={})
        client.place_option_order.assert_not_called()

    async def test_orphan_above_stop_no_sell(self, db):
        """
        Orphan cost $2.00, current $1.70 → loss 15%.
        With STOP_LOSS_PCT=0.18 (18%) → NOT triggered.
        """
        pos   = _make_position("NVDA240315C00500000", qty=1, cost_basis_total=200.0)
        quote = _make_opt_quote(bid=1.68, ask=1.72)  # mid = 1.70 → loss 15%

        from app.config import settings
        with patch.object(settings, "stop_loss_pct", 0.18):
            client = await self._run(
                db,
                positions=[pos],
                ajoy_trades=[],
                quotes={"NVDA240315C00500000": quote},
            )
        client.place_option_order.assert_not_called()

    async def test_orphan_below_stop_triggers_sell(self, db):
        """
        Orphan cost $2.00, current $1.50 → loss 25%.
        With STOP_LOSS_PCT=0.18 → triggered.
        """
        pos   = _make_position("NVDA240315C00501000", qty=1, cost_basis_total=200.0)
        quote = _make_opt_quote(bid=1.48, ask=1.52)  # mid = 1.50 → loss 25%

        from app.config import settings
        with patch.object(settings, "stop_loss_pct", 0.18):
            client = await self._run(
                db,
                positions=[pos],
                ajoy_trades=[],
                quotes={"NVDA240315C00501000": quote},
            )

        client.place_option_order.assert_called_once()
        call_kwargs = client.place_option_order.call_args.kwargs
        assert call_kwargs["side"]       == "sell_to_close"
        assert call_kwargs["order_type"] == "market"
        assert call_kwargs["quantity"]   == 1

    async def test_tracked_position_not_auto_stopped(self, db):
        """
        Position that Ajoy tracks (matched option_symbol) is skipped.
        Even if price has crashed, _manage_orphan_stops must not touch it —
        _close_trade() handles it through the normal exit logic.
        """
        opt_sym = "AMZN240315C00180000"
        pos     = _make_position(opt_sym, qty=2, cost_basis_total=400.0)
        quote   = _make_opt_quote(bid=0.50, ask=0.60)  # deep loss but tracked
        trade   = self._trade(option_symbol=opt_sym)

        from app.config import settings
        with patch.object(settings, "stop_loss_pct", 0.18):
            client = await self._run(
                db,
                positions=[pos],
                ajoy_trades=[trade],
                quotes={opt_sym: quote},
            )

        client.place_option_order.assert_not_called()

    async def test_quote_fetch_failure_no_sell(self, db):
        """
        If get_option_quote() raises, the orphan is skipped gracefully.
        No sell order is placed and no exception propagates out.
        """
        pos = _make_position("XYZ240315C00010000", qty=1, cost_basis_total=100.0)

        client = MagicMock()
        client.get_positions      = AsyncMock(return_value=[pos])
        client.get_option_quote   = AsyncMock(side_effect=Exception("network error"))
        client.place_option_order = AsyncMock()

        # Should not raise
        await _manage_orphan_stops(db, client)
        client.place_option_order.assert_not_called()

    async def test_orphan_above_stop_threshold_triggers(self, db):
        """
        Loss clearly above STOP_LOSS_PCT: triggers sell.
        cost=2.00, stop_pct=0.20, current=1.50 → loss=25% > 20% → sell.
        (Uses a distinct symbol to avoid cross-test DB state.)
        """
        pos   = _make_position("TSLA240315C00500000", qty=1, cost_basis_total=200.0)
        # cost_per_unit=2.00, bid=1.48, ask=1.52 → mid=1.50 → loss=25%
        quote = _make_opt_quote(bid=1.48, ask=1.52)

        from app.config import settings
        with patch.object(settings, "stop_loss_pct", 0.20):
            client = await self._run(
                db,
                positions=[pos],
                ajoy_trades=[],
                quotes={"TSLA240315C00500000": quote},
            )

        client.place_option_order.assert_called_once()
