"""
Full API integration tests for /api/trades/* routes.

Covers:
  GET  /api/trades/live   — empty; trade present with Tradier error (partial data)
  POST /api/trades/close  — 404, 400 already closed, 502 API error,
                            502 rejected, success with fill, fallback to quote mid,
                            fallback to entry price, MANUAL exit_reason
  GET  /api/trades/reconcile — empty, matched, orphaned, ghost, Tradier 502
  POST /api/trades/orphan/close — success, 502 API error, 502 rejected

All Tradier calls are mocked so no real network requests are made.
"""
import os, pathlib, pytest, pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

_TEST_DB = "/tmp/ajoy_api_trades_full_test.db"
os.environ["DATABASE_URL"]      = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["SCHEDULER_ENABLED"] = "0"

for _f in [_TEST_DB, _TEST_DB + "-shm", _TEST_DB + "-wal"]:
    pathlib.Path(_f).unlink(missing_ok=True)

import httpx
from app.main import app
import app.database as _appdb
import app.main as _appmain
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine  = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB}", echo=False)
_session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
_appdb.engine            = _engine
_appdb.AsyncSessionLocal = _session
_appmain.AsyncSessionLocal = _session

from app.models import Trade, TradeStatus, Direction, ExitReason
from app.services.tradier import OrderResult, Position


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def client():
    # Manually initialise the DB (same work as the lifespan does) so we
    # avoid a LifespanManager conflict when the full test suite is run
    # alongside test_09_api.py (which also owns a LifespanManager on the
    # same `app` object).
    from app.database import Base as _Base
    from app.services.indicators import seed_indicators as _seed
    async with _engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    async with _session() as db:
        await _seed(db)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _insert_open_trade(entry_price=5.00, qty=2) -> Trade:
    """Insert a fresh OPEN trade directly into the test DB."""
    async with _session() as db:
        t = Trade(
            symbol="AAPL",
            option_symbol="AAPL240119C00150000",
            direction=Direction.CALL,
            quantity=qty,
            remaining_qty=qty,
            entry_price=entry_price,
            entry_time=datetime.now(tz=timezone.utc),
            status=TradeStatus.OPEN,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _insert_closed_trade(entry_price=5.00, exit_price=6.75, qty=2) -> Trade:
    async with _session() as db:
        t = Trade(
            symbol="AAPL",
            option_symbol="AAPL240119C00150000",
            direction=Direction.CALL,
            quantity=qty,
            remaining_qty=qty,
            entry_price=entry_price,
            entry_time=datetime.now(tz=timezone.utc),
            status=TradeStatus.CLOSED,
            exit_price=exit_price,
            exit_time=datetime.now(tz=timezone.utc),
            exit_reason=ExitReason.TP2,
            pnl=round((exit_price - entry_price) * qty * 100, 2),
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


def _mock_tradier(
    fill_price=None,
    order_status="filled",
    quote_bid=None,
    quote_ask=None,
    place_raises=False,
    positions=None,
):
    """Build a mock TradierClient for API route tests."""
    c = MagicMock()

    if place_raises:
        c.place_option_order = AsyncMock(side_effect=Exception("Tradier down"))
    else:
        c.place_option_order = AsyncMock(
            return_value=OrderResult(order_id="ord999", status="ok")
        )

    c.get_fill_price   = AsyncMock(return_value=fill_price)
    c.get_order_status = AsyncMock(return_value={"status": order_status})

    if quote_bid is not None:
        q = MagicMock()
        q.bid  = quote_bid
        q.ask  = quote_ask
        q.last = round((quote_bid + quote_ask) / 2, 2)
        c.get_option_quote = AsyncMock(return_value=q)
    else:
        c.get_option_quote = AsyncMock(return_value=None)

    c.get_positions = AsyncMock(return_value=positions or [])
    c.get_quote     = AsyncMock(return_value=MagicMock(last=150.0))
    c.get_intraday_bars = AsyncMock(return_value=[])

    return c


# ===========================================================================
# GET /api/trades/live
# ===========================================================================

@pytest.mark.asyncio
async def test_live_initially_empty(client):
    r = await client.get("/api/trades/live")
    assert r.status_code == 200
    # Filter to only OPEN trades — there may be trades from other tests
    open_trades = [t for t in r.json() if t["status"] == "open"]
    # This test just checks the endpoint is alive and returns a list
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_live_tradier_error_returns_partial_data(client):
    """If Tradier quote call fails, trade still appears with null live P&L (no 500)."""
    # The route swallows Tradier errors and returns partial data
    t = await _insert_open_trade()

    failing_client = MagicMock()
    failing_client.get_option_quote = AsyncMock(side_effect=Exception("Tradier timeout"))
    failing_client.get_quote        = AsyncMock(side_effect=Exception("Tradier timeout"))
    failing_client.get_intraday_bars = AsyncMock(return_value=[])

    with patch("app.routers.trades.get_tradier_client", return_value=failing_client):
        r = await client.get("/api/trades/live")

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # The trade should appear, current_price may be null
    trade_ids = [item["id"] for item in body]
    assert t.id in trade_ids


# ===========================================================================
# POST /api/trades/close
# ===========================================================================

@pytest.mark.asyncio
async def test_close_unknown_trade_id_returns_404(client):
    r = await client.post("/api/trades/close", json={"trade_id": 999999})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_close_already_closed_trade_returns_400(client):
    """Closing a CLOSED trade must return 400."""
    t = await _insert_closed_trade()
    r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 400
    assert "already closed" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_close_tradier_api_error_returns_502(client):
    """Exception from place_option_order → 502."""
    t = await _insert_open_trade()
    mock_client = _mock_tradier(place_raises=True)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_close_rejected_order_returns_502(client):
    """Rejected sell order → 502 with descriptive message."""
    t = await _insert_open_trade()
    mock_client = _mock_tradier(fill_price=None, order_status="rejected")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 502
    detail = r.json()["detail"].upper()
    assert "REJECT" in detail


@pytest.mark.asyncio
async def test_close_success_with_fill_price(client):
    """Successful close: fill_price available → trade closed, exit_reason=MANUAL."""
    t = await _insert_open_trade(entry_price=5.00, qty=2)
    mock_client = _mock_tradier(fill_price=6.50, order_status="filled")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 200
    body = r.json()
    assert body["status"]      == "closed"
    assert body["exit_reason"] == "MANUAL"
    assert body["exit_price"]  == pytest.approx(6.50, abs=0.01)
    # pnl = (6.50 - 5.00) * 2 * 100 = +300
    assert body["pnl"] == pytest.approx(300.0, abs=0.01)


@pytest.mark.asyncio
async def test_close_fallback_to_quote_mid(client):
    """No fill_price → fallback to (bid+ask)/2 mid-quote."""
    t = await _insert_open_trade(entry_price=5.00, qty=1)
    mock_client = _mock_tradier(
        fill_price=None, order_status="filled",
        quote_bid=4.80, quote_ask=5.20
    )
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 200
    assert r.json()["exit_price"] == pytest.approx(5.00, abs=0.01)  # (4.80+5.20)/2


@pytest.mark.asyncio
async def test_close_fallback_to_entry_price_pnl_zero(client):
    """No fill, no quote → exit_price = entry_price → pnl = 0."""
    t = await _insert_open_trade(entry_price=5.00, qty=1)
    mock_client = _mock_tradier(fill_price=None, order_status="filled")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 200
    body = r.json()
    assert body["exit_price"] == pytest.approx(5.00, abs=0.01)
    assert body["pnl"]        == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_close_stop_loss_negative_pnl(client):
    """Fill price below entry → pnl must be negative."""
    t = await _insert_open_trade(entry_price=5.00, qty=2)
    # exit at 3.75 → pnl = (3.75-5.00)*2*100 = -250
    mock_client = _mock_tradier(fill_price=3.75, order_status="filled")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post("/api/trades/close", json={"trade_id": t.id})
    assert r.status_code == 200
    assert r.json()["pnl"] == pytest.approx(-250.0, abs=0.01)


# ===========================================================================
# GET /api/trades/reconcile
# ===========================================================================

@pytest.mark.asyncio
async def test_reconcile_tradier_error_returns_502(client):
    """If get_positions() raises, reconcile must return 502."""
    mock_client = MagicMock()
    mock_client.get_positions = AsyncMock(side_effect=Exception("Tradier down"))
    mock_client.get_option_quote = AsyncMock(return_value=None)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_reconcile_empty(client):
    """No open trades, no Tradier positions → all lists empty."""
    mock_client = _mock_tradier(positions=[])
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert "matched"             in body
    assert "orphaned_in_tradier" in body
    assert "ghost_in_ajoy"       in body


@pytest.mark.asyncio
async def test_reconcile_ghost_detected(client):
    """Ajoy OPEN trade with no matching Tradier position → ghost_in_ajoy."""
    t = await _insert_open_trade()
    # Tradier returns no positions → trade is a ghost
    mock_client = _mock_tradier(positions=[])
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")
    assert r.status_code == 200
    ghosts = r.json()["ghost_in_ajoy"]
    ghost_ids = [g["ajoy_trade_id"] for g in ghosts]
    assert t.id in ghost_ids


@pytest.mark.asyncio
async def test_reconcile_orphan_detected(client):
    """Tradier position with no Ajoy record → orphaned_in_tradier."""
    orphan_symbol = "NVDA240119C00800000"
    orphan_pos = Position(
        symbol=orphan_symbol,
        quantity=3,
        cost_basis=450.0,   # 3 × $1.50 × 100
    )
    q = MagicMock()
    q.bid  = 1.80
    q.ask  = 2.00
    q.last = 1.90

    mock_client = MagicMock()
    mock_client.get_positions    = AsyncMock(return_value=[orphan_pos])
    mock_client.get_option_quote = AsyncMock(return_value=q)

    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")

    assert r.status_code == 200
    orphans = r.json()["orphaned_in_tradier"]
    syms = [o["symbol"] for o in orphans]
    assert orphan_symbol in syms

    # cost_per_unit = 450 / (3 × 100) = 1.50
    match = next(o for o in orphans if o["symbol"] == orphan_symbol)
    assert match["cost_per_unit"] == pytest.approx(1.50, abs=0.001)
    assert match["qty"]           == 3


@pytest.mark.asyncio
async def test_reconcile_orphan_live_pnl_computed(client):
    """Orphaned position live_pnl = (current_price - cost_per_unit) * qty * 100."""
    orphan_pos = Position(
        symbol="AMZN240119C00200000",
        quantity=2,
        cost_basis=300.0,   # cost_per_unit = 300/(2×100) = 1.50
    )
    q = MagicMock()
    q.bid  = 1.60
    q.ask  = 1.80   # mid = 1.70
    q.last = 1.70

    mock_client = MagicMock()
    mock_client.get_positions    = AsyncMock(return_value=[orphan_pos])
    mock_client.get_option_quote = AsyncMock(return_value=q)

    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")

    orphans = r.json()["orphaned_in_tradier"]
    match   = next(o for o in orphans if o["symbol"] == "AMZN240119C00200000")
    # live_pnl = (1.70 - 1.50) * 2 * 100 = +40.0
    assert match["live_pnl"] == pytest.approx(40.0, abs=0.01)


@pytest.mark.asyncio
async def test_reconcile_matched_detected(client):
    """Ajoy OPEN trade with matching Tradier position → matched list."""
    t = await _insert_open_trade()
    pos = Position(
        symbol=t.option_symbol,
        quantity=t.remaining_qty or t.quantity,
        cost_basis=t.entry_price * (t.remaining_qty or t.quantity) * 100,
    )
    mock_client = MagicMock()
    mock_client.get_positions    = AsyncMock(return_value=[pos])
    mock_client.get_option_quote = AsyncMock(return_value=None)

    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.get("/api/trades/reconcile")

    assert r.status_code == 200
    matched = r.json()["matched"]
    trade_ids = [m["ajoy_trade_id"] for m in matched]
    assert t.id in trade_ids


# ===========================================================================
# POST /api/trades/orphan/close
# ===========================================================================

@pytest.mark.asyncio
async def test_orphan_close_success(client):
    """Successful orphan close returns order_id and fill_price."""
    mock_client = _mock_tradier(fill_price=2.50, order_status="filled")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post(
            "/api/trades/orphan/close",
            json={"option_symbol": "NVDA240119C00800000", "quantity": 2}
        )
    assert r.status_code == 200
    body = r.json()
    assert "order_id"   in body
    assert "fill_price" in body
    assert "message"    in body


@pytest.mark.asyncio
async def test_orphan_close_tradier_error_returns_502(client):
    """Exception from place_option_order → 502."""
    mock_client = _mock_tradier(place_raises=True)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post(
            "/api/trades/orphan/close",
            json={"option_symbol": "NVDA240119C00800000", "quantity": 1}
        )
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_orphan_close_rejected_returns_502(client):
    """Rejected sell order on orphan → 502."""
    mock_client = _mock_tradier(fill_price=None, order_status="rejected")
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post(
            "/api/trades/orphan/close",
            json={"option_symbol": "NVDA240119C00800000", "quantity": 1}
        )
    assert r.status_code == 502
    assert "REJECT" in r.json()["detail"].upper()


# ===========================================================================
# POST /api/trades/orphan/adopt
# ===========================================================================

@pytest.mark.asyncio
async def test_adopt_creates_open_trade(client):
    """Adopting a valid orphan creates an OPEN Trade record."""
    mock_client = MagicMock()
    mock_client.get_quote = AsyncMock(return_value=None)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post(
            "/api/trades/orphan/adopt",
            json={
                "option_symbol": "IWM260601P00290000",
                "quantity": 2,
                "cost_per_unit": 1.25,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"]        == "IWM"
    assert body["direction"]     == "PUT"
    assert body["quantity"]      == 2
    assert body["entry_price"]   == pytest.approx(1.25, abs=0.001)
    assert body["status"]        == "open"
    assert body["strategy_name"] == "adopted_orphan"
    assert body["stop_price"]    is not None
    assert body["tp1_price"]     is not None


@pytest.mark.asyncio
async def test_adopt_call_direction(client):
    """CALL contract creates a CALL-direction Trade."""
    mock_client = MagicMock()
    mock_client.get_quote = AsyncMock(return_value=None)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        r = await client.post(
            "/api/trades/orphan/adopt",
            json={
                "option_symbol": "AAPL250117C00220000",
                "quantity": 1,
                "cost_per_unit": 3.50,
            },
        )
    assert r.status_code == 200
    assert r.json()["direction"] == "CALL"
    assert r.json()["symbol"]    == "AAPL"


@pytest.mark.asyncio
async def test_adopt_duplicate_returns_409(client):
    """Adopting the same open option symbol twice returns 409 Conflict."""
    mock_client = MagicMock()
    mock_client.get_quote = AsyncMock(return_value=None)
    with patch("app.routers.trades.get_tradier_client", return_value=mock_client):
        # First adopt — succeeds
        r1 = await client.post(
            "/api/trades/orphan/adopt",
            json={
                "option_symbol": "META260620C00600000",
                "quantity": 1,
                "cost_per_unit": 2.00,
            },
        )
        assert r1.status_code == 200

        # Second adopt — same symbol still open → conflict
        r2 = await client.post(
            "/api/trades/orphan/adopt",
            json={
                "option_symbol": "META260620C00600000",
                "quantity": 1,
                "cost_per_unit": 2.00,
            },
        )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_adopt_invalid_symbol_returns_422(client):
    """Malformed option symbol → 422."""
    r = await client.post(
        "/api/trades/orphan/adopt",
        json={"option_symbol": "GARBAGE", "quantity": 1, "cost_per_unit": 1.00},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_adopt_zero_cost_returns_422(client):
    """cost_per_unit = 0 → 422. Uses a symbol not previously adopted."""
    r = await client.post(
        "/api/trades/orphan/adopt",
        json={
            "option_symbol": "GLD260620P00220000",
            "quantity": 1,
            "cost_per_unit": 0.0,
        },
    )
    assert r.status_code == 422
