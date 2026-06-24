"""
API integration tests — all routes via ASGI transport (no external server).
A fresh DB is created for each test run by wiping the file before the app starts.
"""
import os, pathlib, pytest, pytest_asyncio

# ── Isolate this module's DB and disable the scheduler ─────────────────────
_TEST_DB = "/tmp/ajoy_api_test.db"
os.environ["DATABASE_URL"]      = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["SCHEDULER_ENABLED"] = "0"

# Wipe any DB left over from previous runs BEFORE importing the app,
# so lifespan always starts against a clean slate.
for _f in [_TEST_DB, _TEST_DB + "-shm", _TEST_DB + "-wal"]:
    pathlib.Path(_f).unlink(missing_ok=True)

import httpx
from asgi_lifespan import LifespanManager
from app.main import app

# ── Re-point the SQLAlchemy engine to this module's isolated DB ─────────────
# conftest.py may have already imported app.database with a different DB URL
# (./test_ajoy.db).  We replace the engine + session factory everywhere it
# is referenced so that lifespan's init_db(), seed_indicators(), and all
# route-handler get_db() calls all hit our clean /tmp DB.
import app.database as _appdb
import app.main as _appmain
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_new_engine  = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB}", echo=False)
_new_session = async_sessionmaker(_new_engine, expire_on_commit=False, class_=AsyncSession)

_appdb.engine            = _new_engine
_appdb.AsyncSessionLocal = _new_session
_appmain.AsyncSessionLocal = _new_session  # used in lifespan for seed_indicators


@pytest_asyncio.fixture(scope="module")
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ── Root HTML ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_root_returns_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text.lower()

@pytest.mark.asyncio
async def test_static_css_served(client):
    r = await client.get("/static/css/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]

@pytest.mark.asyncio
async def test_static_js_served(client):
    r = await client.get("/static/js/app.js")
    assert r.status_code == 200


# ── /api/symbols ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_symbols_list_initially_empty(client):
    r = await client.get("/api/symbols")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 0

@pytest.mark.asyncio
async def test_symbol_create(client):
    r = await client.post("/api/symbols", json={"ticker": "AAPL", "active": True})
    assert r.status_code == 201
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["active"] is True
    assert "id" in body

@pytest.mark.asyncio
async def test_symbol_duplicate_rejected(client):
    # AAPL was just created above — a second attempt must get 409
    r = await client.post("/api/symbols", json={"ticker": "AAPL"})
    assert r.status_code == 409

@pytest.mark.asyncio
async def test_symbol_ticker_uppercased(client):
    r = await client.post("/api/symbols", json={"ticker": "msft"})
    assert r.status_code == 201
    assert r.json()["ticker"] == "MSFT"

@pytest.mark.asyncio
async def test_symbol_patch_active(client):
    r = await client.post("/api/symbols", json={"ticker": "NVDA", "active": True})
    assert r.status_code == 201
    sym_id = r.json()["id"]
    r2 = await client.patch(f"/api/symbols/{sym_id}", json={"active": False})
    assert r2.status_code == 200
    assert r2.json()["active"] is False
    # Restore
    await client.patch(f"/api/symbols/{sym_id}", json={"active": True})

@pytest.mark.asyncio
async def test_symbol_patch_not_found(client):
    r = await client.patch("/api/symbols/99999", json={"active": False})
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_symbol_delete(client):
    r = await client.post("/api/symbols", json={"ticker": "DELETEME"})
    assert r.status_code == 201
    sym_id = r.json()["id"]
    r2 = await client.delete(f"/api/symbols/{sym_id}")
    assert r2.status_code == 204
    # Confirm it's gone
    r3 = await client.get("/api/symbols")
    tickers = [s["ticker"] for s in r3.json()]
    assert "DELETEME" not in tickers

@pytest.mark.asyncio
async def test_symbol_delete_not_found(client):
    r = await client.delete("/api/symbols/99999")
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_symbols_list_after_creates(client):
    r = await client.get("/api/symbols")
    tickers = [s["ticker"] for s in r.json()]
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "DELETEME" not in tickers


# ── /api/trades ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trades_live_initially_empty(client):
    r = await client.get("/api/trades/live")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 0

@pytest.mark.asyncio
async def test_manual_close_unknown_trade(client):
    r = await client.post("/api/trades/close", json={"trade_id": 99999})
    assert r.status_code == 404


# ── /api/indicators ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_indicators_seeded(client):
    """Lifespan seed_indicators() should populate the default indicator set."""
    r = await client.get("/api/indicators")
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0
    keys = [i["key"] for i in data]
    assert "trend_15min"      in keys
    assert "price_vs_vwap"    in keys
    assert "pullback_to_vwap" in keys

@pytest.mark.asyncio
async def test_indicator_toggle(client):
    r = await client.get("/api/indicators")
    ind = r.json()[0]
    ind_id, original_active = ind["id"], ind["active"]
    r2 = await client.patch(f"/api/indicators/{ind_id}", json={"active": not original_active})
    assert r2.status_code == 200
    assert r2.json()["active"] == (not original_active)
    # Restore
    await client.patch(f"/api/indicators/{ind_id}", json={"active": original_active})


# ── /api/history ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_today_empty(client):
    r = await client.get("/api/history/today")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

@pytest.mark.asyncio
async def test_history_last30_empty(client):
    r = await client.get("/api/history/last30")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

@pytest.mark.asyncio
async def test_history_summary_today(client):
    r = await client.get("/api/history/summary/today")
    assert r.status_code == 200
    body = r.json()
    # Should return a summary dict with known keys
    assert body["trade_count"] == 0
    assert body["total_pnl"] == 0.0
    assert "winners" in body and "losers" in body
