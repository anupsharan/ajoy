#!/usr/bin/env python3
"""
guardian.py — End-of-day safety close for ajoy.

Runs independently at end-of-day (default 2:50 PM ET) via cron.
Closes ALL open option positions in Tradier with market sell-to-close
orders, regardless of whether the main bot process is running.

This serves two purposes:
  1. Bot offline, position below stop  → guardian closes it (prevents bigger loss)
  2. Bot offline, position above stop  → guardian closes it (locks in profit)

After closing, it marks the corresponding bot DB trades as CLOSED so the
bot doesn't try to manage stale open records when it restarts.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SETUP — add to crontab (crontab -e):

  # Close all positions at 2:50 PM ET (18:50 UTC) Mon–Fri
  50 18 * * 1-5  cd /path/to/ajoy && /path/to/ajoy/.venv/bin/python guardian.py >> guardian.log 2>&1

  Tip: verify the UTC offset for your region — EST is UTC-5, EDT is UTC-4.
    EST (Nov–Mar): 14:50 ET = 19:50 UTC → "50 19 * * 1-5"
    EDT (Mar–Nov): 14:50 ET = 18:50 UTC → "50 18 * * 1-5"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── make sure app/ is importable from any working directory ──────────────
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models import Trade, TradeStatus, ExitReason
from app.services.tradier import TradierClient

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GUARDIAN] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("guardian")


# ── Helpers ───────────────────────────────────────────────────────────────

def _is_option_symbol(symbol: str) -> bool:
    """OCC option symbols are 21 chars: underlying + expiry + C/P + strike."""
    return len(symbol) >= 15 and any(c in symbol for c in ("C", "P"))


async def _close_position(client: TradierClient, symbol: str, qty: int) -> bool:
    """Place a market sell-to-close for qty contracts. Returns True on success."""
    try:
        result = await client.place_option_order(
            option_symbol=symbol,
            side="sell_to_close",
            quantity=qty,
            order_type="market",
        )
        if result and result.order_id:
            log.info("  ✓ Sell-to-close placed  %s  qty=%d  order=%s",
                     symbol, qty, result.order_id)
            return True
        log.warning("  ✗ No order_id returned for %s", symbol)
        return False
    except Exception as exc:
        log.error("  ✗ Failed to close %s: %s", symbol, exc)
        return False


async def _mark_db_trades_closed(
    session_factory,
    closed_symbols: set[str],
    exit_time: datetime,
) -> None:
    """
    Mark any OPEN bot trades whose option_symbol was just closed as CLOSED.
    Uses ExitReason.CUTOFF since this is an end-of-day forced close.
    """
    if not closed_symbols:
        return

    async with session_factory() as db:
        result = await db.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN)
        )
        open_trades = result.scalars().all()

        updated = 0
        for trade in open_trades:
            if trade.option_symbol in closed_symbols:
                trade.status      = TradeStatus.CLOSED
                trade.exit_time   = exit_time
                trade.exit_reason = ExitReason.CUTOFF
                # P&L left as None — we don't have a fill price from the
                # guardian.  The Tradier Gain/Loss page will show the truth.
                updated += 1
                log.info("  DB trade #%d (%s %s) marked CLOSED",
                         trade.id, trade.symbol, trade.option_symbol)

        if updated:
            await db.commit()
            log.info("  %d DB trade(s) marked CLOSED", updated)
        else:
            log.info("  No matching open DB trades found")


# ── Main ──────────────────────────────────────────────────────────────────

async def run() -> None:
    now_utc = datetime.now(tz=timezone.utc)
    log.info("=" * 60)
    log.info("Guardian starting at %s UTC", now_utc.strftime("%Y-%m-%d %H:%M:%S"))

    client = TradierClient()

    # ── 1. Fetch open positions ──────────────────────────────────────────
    try:
        positions = await client.get_positions()
    except Exception as exc:
        log.error("Could not fetch positions from Tradier: %s", exc)
        log.error("Aborting — no positions were closed.")
        return

    option_positions = [
        p for p in positions
        if _is_option_symbol(p.symbol) and p.quantity != 0
    ]

    if not option_positions:
        log.info("No open option positions found — nothing to close.")
        log.info("=" * 60)
        return

    log.info("Found %d open option position(s):", len(option_positions))
    for p in option_positions:
        log.info("  %s  qty=%d  cost_basis=$%.2f", p.symbol, p.quantity, p.cost_basis)

    # ── 1b. Cancel pending option orders (e.g. resting broker stops) ──────
    # An open sell order reserves the contracts — our sell-to-close would be
    # rejected (or double-sell) while it is live.
    try:
        pending = await client.get_open_orders()
        for o in pending:
            if (o.get("class") or "").lower() != "option":
                continue
            oid = str(o.get("id", ""))
            if not oid:
                continue
            try:
                await client.cancel_order(oid)
                log.info("  Canceled pending order %s (%s %s)",
                         oid, o.get("side", "?"), o.get("option_symbol", "?"))
            except Exception as exc:
                log.warning("  Could not cancel pending order %s: %s", oid, exc)
    except Exception as exc:
        log.warning("Could not list pending orders: %s", exc)

    # ── 2. Close each position ───────────────────────────────────────────
    log.info("Placing sell-to-close orders...")
    closed_symbols: set[str] = set()

    for pos in option_positions:
        qty = abs(pos.quantity)   # qty is always positive for sell-to-close
        ok  = await _close_position(client, pos.symbol, qty)
        if ok:
            closed_symbols.add(pos.symbol)

    log.info("%d / %d position(s) closed successfully.",
             len(closed_symbols), len(option_positions))

    # ── 3. Update bot DB ─────────────────────────────────────────────────
    log.info("Updating bot DB...")
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _mark_db_trades_closed(Session, closed_symbols, now_utc)
    finally:
        await engine.dispose()

    log.info("Guardian finished at %s UTC",
             datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
