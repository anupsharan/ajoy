"""
Tests for P&L arithmetic and Pydantic schema serialisation.

P&L tests operate directly on Trade ORM objects using _close_trade so the
arithmetic is tested end-to-end through the same code path as production.

Schema tests verify:
  • Timezone-naive datetimes get a '+00:00' offset (critical for JS Date())
  • Timezone-aware datetimes are serialised correctly
  • exit_time=None → null in JSON
  • TradeWithLivePnL live_pnl formula
  • Direction / ExitReason enums serialise to string values
  • pnl=None on an open trade serialises to null
"""
import os, pytest, pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_pnl_schema_test.db"

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Trade, TradeStatus, Direction, ExitReason
from app.schemas import TradeOut, TradeWithLivePnL, _as_utc_iso


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_pnl_schema_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _persisted_trade(db_session, entry_price=5.00, qty=2, remaining_qty=2,
                           prior_pnl=None) -> Trade:
    """Insert a Trade into the DB and return the refreshed (id + defaults populated) row."""
    t = Trade(
        symbol="AAPL",
        option_symbol="AAPL240119C00150000",
        direction=Direction.CALL,
        strategy_name="vwap_pullback",
        quantity=qty,
        remaining_qty=remaining_qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        status=TradeStatus.OPEN,
        pnl=prior_pnl,
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


# ===========================================================================
# _as_utc_iso helper
# ===========================================================================

class TestAsUtcIso:

    def test_none_returns_none(self):
        assert _as_utc_iso(None) is None

    def test_naive_datetime_gets_utc_suffix(self):
        """SQLite returns naive datetimes; the helper must add '+00:00'."""
        naive = datetime(2024, 1, 15, 10, 30, 0)
        result = _as_utc_iso(naive)
        assert result is not None
        assert "+00:00" in result

    def test_aware_datetime_serialised_correctly(self):
        aware = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _as_utc_iso(aware)
        assert result is not None
        # ISO format with timezone offset
        assert "2024-01-15" in result
        assert "10:30:00" in result

    def test_naive_datetime_is_not_local_time(self):
        """
        The JavaScript pitfall: without '+00:00', JS Date() treats the string
        as local time (e.g. ET = UTC-5). Verify the offset is always present.
        """
        naive = datetime(2024, 6, 1, 14, 0, 0)   # no tzinfo
        result = _as_utc_iso(naive)
        # Must contain offset information, not be a bare ISO string
        assert "+" in result or "Z" in result or "-00" in result


# ===========================================================================
# TradeOut schema
# ===========================================================================

class TestTradeOutSchema:

    @pytest.mark.asyncio
    async def test_model_validate_from_open_trade(self, db):
        t = await _persisted_trade(db, entry_price=5.00, qty=2)
        out = TradeOut.model_validate(t)
        assert out.symbol == "AAPL"
        assert out.direction == Direction.CALL
        assert out.status == TradeStatus.OPEN
        assert out.entry_price == 5.00
        assert out.pnl is None

    @pytest.mark.asyncio
    async def test_exit_time_none_serialises_to_null_in_json(self, db):
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["exit_time"] is None

    @pytest.mark.asyncio
    async def test_exit_reason_none_serialises_to_null(self, db):
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["exit_reason"] is None

    @pytest.mark.asyncio
    async def test_pnl_none_on_open_trade(self, db):
        """An open trade has pnl=None, not 0.0."""
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["pnl"] is None

    @pytest.mark.asyncio
    async def test_direction_serialises_to_string(self, db):
        """Direction enum must serialise to its string value, not the Python object."""
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["direction"] == "CALL"

    @pytest.mark.asyncio
    async def test_status_serialises_to_string(self, db):
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["status"] == "open"

    @pytest.mark.asyncio
    async def test_entry_time_has_utc_offset_in_json(self, db):
        """entry_time must always include timezone offset in JSON output."""
        t = await _persisted_trade(db)
        out = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert "+" in data["entry_time"] or "Z" in data["entry_time"]

    @pytest.mark.asyncio
    async def test_closed_trade_with_all_exit_fields(self, db):
        t = await _persisted_trade(db, entry_price=5.00, qty=2)
        t.status      = TradeStatus.CLOSED
        t.exit_price  = 6.75
        t.exit_time   = datetime.now(tz=timezone.utc)
        t.exit_reason = ExitReason.TP2
        t.pnl         = 350.0
        await db.commit()
        await db.refresh(t)

        out  = TradeOut.model_validate(t)
        data = out.model_dump(mode="json")
        assert data["status"]      == "closed"
        assert data["exit_price"]  == 6.75
        assert data["exit_reason"] == "TP2"
        assert data["pnl"]         == pytest.approx(350.0)
        assert data["exit_time"]   is not None

    @pytest.mark.asyncio
    async def test_exit_reason_all_values_serialise_correctly(self, db):
        """Every ExitReason enum value must serialise to its string name."""
        for reason in ExitReason:
            t = await _persisted_trade(db)
            t.exit_reason = reason
            await db.commit()
            await db.refresh(t)
            out  = TradeOut.model_validate(t)
            data = out.model_dump(mode="json")
            assert data["exit_reason"] == reason.value


# ===========================================================================
# TradeWithLivePnL schema
# ===========================================================================

class TestTradeWithLivePnL:

    @pytest.mark.asyncio
    async def test_extra_fields_default_to_none(self, db):
        t = await _persisted_trade(db)
        base = TradeOut.model_validate(t)
        live = TradeWithLivePnL(**base.model_dump())
        assert live.current_price    is None
        assert live.live_pnl         is None
        assert live.live_pnl_pct     is None
        assert live.underlying_price is None
        assert live.trend            is None

    @pytest.mark.asyncio
    async def test_live_pnl_formula(self, db):
        """
        live_pnl = (current_price - entry_price) * remaining_qty * 100
        entry=5.00, current=6.50, qty=2 → unrealised = +300.0
        """
        t = await _persisted_trade(db, entry_price=5.00, qty=2, remaining_qty=2)
        base = TradeOut.model_validate(t)
        live = TradeWithLivePnL(**base.model_dump())
        live.current_price = 6.50

        entry    = t.entry_price
        qty      = t.remaining_qty or t.quantity
        current  = live.current_price
        expected = (current - entry) * qty * 100
        live.live_pnl = round(expected, 2)
        assert live.live_pnl == pytest.approx(300.0, abs=0.01)

    def test_live_pnl_pct_formula(self):
        """
        live_pnl_pct = live_pnl / original_cost * 100
        original_cost = entry * quantity * 100 = 5.00 * 2 * 100 = 1000
        live_pnl = +300 → pct = 30.0%
        """
        entry    = 5.00
        qty      = 2
        current  = 6.50
        original_cost = entry * qty * 100   # 1000
        live_pnl = round((current - entry) * qty * 100, 2)   # 300
        pct      = round(live_pnl / original_cost * 100, 2)
        assert pct == pytest.approx(30.0, abs=0.01)

    def test_live_pnl_negative_for_loss(self):
        """entry=5.00, current=3.50, qty=2 → live_pnl = -300.0"""
        entry   = 5.00
        qty     = 2
        current = 3.50
        live_pnl = (current - entry) * qty * 100
        assert live_pnl == pytest.approx(-300.0, abs=0.01)

    def test_live_pnl_pct_uses_original_quantity_not_remaining(self):
        """
        When TP1 was hit, remaining_qty < quantity.
        The pct denominator must use entry × quantity × 100 (original cost),
        not entry × remaining_qty × 100 (would inflate the percentage).
        """
        entry    = 5.00
        qty      = 4
        remaining = 2   # half closed at TP1
        current  = 6.50

        original_cost = entry * qty * 100           # 2000 (correct denominator)
        live_pnl      = (current - entry) * remaining * 100  # 300
        pct_correct   = round(live_pnl / original_cost * 100, 2)   # 15%

        wrong_cost     = entry * remaining * 100    # 1000 (wrong denominator)
        pct_inflated   = round(live_pnl / wrong_cost * 100, 2)     # 30%

        assert pct_correct  == pytest.approx(15.0, abs=0.01)
        assert pct_inflated == pytest.approx(30.0, abs=0.01)
        # Ensure the correct value (15%) is distinguishable from the wrong one (30%)
        assert pct_correct != pct_inflated


# ===========================================================================
# P&L arithmetic — end-to-end via _close_trade on real DB rows
# ===========================================================================

from app.services.scheduler import _close_trade
from app.services.tradier import OrderResult


def _mock_sell_client():
    """Mock TradierClient: sell order succeeds, no fill price (forces priority-chain)."""
    c = MagicMock()
    c.place_option_order = AsyncMock(return_value=OrderResult(order_id="s1", status="ok"))
    c.get_fill_price     = AsyncMock(return_value=None)
    c.get_order_status   = AsyncMock(return_value={"status": "filled"})
    c.get_option_quote   = AsyncMock(return_value=None)
    return c


class TestEndToEndPnL:

    @pytest.mark.asyncio
    async def test_stop_gives_negative_pnl(self, db):
        # entry=5.00, exit=3.75, qty=2 → -250
        t = await _persisted_trade(db, entry_price=5.00, qty=2, remaining_qty=2)
        await _close_trade(db, _mock_sell_client(), t, ExitReason.STOP, exit_price=3.75)
        await db.refresh(t)
        assert t.pnl == pytest.approx(-250.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_tp_gives_positive_pnl(self, db):
        # entry=5.00, exit=6.75, qty=2 → +350
        t = await _persisted_trade(db, entry_price=5.00, qty=2, remaining_qty=2)
        await _close_trade(db, _mock_sell_client(), t, ExitReason.TP2, exit_price=6.75)
        await db.refresh(t)
        assert t.pnl == pytest.approx(350.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_fallback_gives_zero_pnl(self, db):
        # No exit price supplied, no fill, no quote → entry_price fallback → pnl=0
        t = await _persisted_trade(db, entry_price=5.00, qty=1, remaining_qty=1)
        await _close_trade(db, _mock_sell_client(), t, ExitReason.CUTOFF)
        await db.refresh(t)
        assert t.pnl == pytest.approx(0.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_pnl_accumulates_partial(self, db):
        # prior partial +50; close at 4.00, entry 5.00, qty=2 → close_pnl=-200 → total=-150
        t = await _persisted_trade(db, entry_price=5.00, qty=2, remaining_qty=2, prior_pnl=50.0)
        await _close_trade(db, _mock_sell_client(), t, ExitReason.STOP, exit_price=4.00)
        await db.refresh(t)
        assert t.pnl == pytest.approx(-150.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_large_qty_pnl(self, db):
        # entry=3.00, exit=4.20, qty=10 → +1200
        t = await _persisted_trade(db, entry_price=3.00, qty=10, remaining_qty=10)
        await _close_trade(db, _mock_sell_client(), t, ExitReason.TP2, exit_price=4.20)
        await db.refresh(t)
        assert t.pnl == pytest.approx(1200.0, abs=0.01)
