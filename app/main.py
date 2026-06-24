"""FastAPI application entry point for a-joy."""
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import AsyncSessionLocal, init_db
from app.models import Trade
from sqlalchemy import select
from app.routers import config, history, indicators, symbols, trades
from app.services.indicators import seed_indicators
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.tradier import close_tradier_client

# ── File logging ─────────────────────────────────────────────────────────
# Writes to ajoy.log alongside the DB, rotating at 10 MB, keeping 7 days.
# Run `tail -f ajoy.log | grep -E "\[L[1-6]\]|\[G[0-9]\]|CLOSED|OPEN|ERROR"`
_log_path = Path(__file__).parent.parent / "ajoy.log"
_file_handler = logging.handlers.RotatingFileHandler(
    _log_path, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

# Root logger at INFO — app code logs what it needs, libraries stay quiet.
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

# Silence chatty third-party libraries that flood the log with DEBUG noise.
for _noisy in (
    "aiosqlite",
    "sqlalchemy",
    "sqlalchemy.engine",
    "httpcore",
    "httpx",
    "apscheduler.executors",
    "apscheduler.scheduler",
    "asyncio",
    "uvicorn.access",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_indicators(db)
    start_scheduler()
    yield
    stop_scheduler()
    await close_tradier_client()


app = FastAPI(title="a-joy trading agent", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Routers
app.include_router(config.router)
app.include_router(symbols.router)
app.include_router(indicators.router)
app.include_router(trades.router)
app.include_router(history.router)


@app.post("/admin/patch-trade-pnl")
async def patch_trade_pnl(trade_id: int, exit_price: float, pnl: float):
    """One-time admin endpoint — correct a misrecorded exit price / P&L."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Trade).where(Trade.id == trade_id))
        trade = result.scalar_one_or_none()
        if not trade:
            raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
        old = {"exit_price": trade.exit_price, "pnl": trade.pnl}
        trade.exit_price = exit_price
        trade.pnl        = pnl
        await db.commit()
        return JSONResponse({"patched": True, "trade_id": trade_id, "old": old,
                             "new": {"exit_price": exit_price, "pnl": pnl}})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    response = templates.TemplateResponse("index.html", {"request": request})
    # Never cache the HTML shell — the browser must always fetch fresh markup
    # so that versioned app.js?vN cache-busts work correctly.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
