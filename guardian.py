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

MULTI-ACCOUNT (Jul 25 2026): guardian sweeps EVERY account in the accounts
table — including accounts that are disabled in the UI, because a disabled
account can still be holding a position that was opened before it was paused.
Sweeping only the .env account would leave the other accounts' positions open
overnight, which is precisely the failure this script exists to prevent.
If the accounts table is missing or empty it falls back to the .env account,
so an install that predates multi-account behaves exactly as it always did.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SETUP — add to crontab (crontab -e):

  IMPORTANT: cron runs in the machine's LOCAL timezone, not UTC.  The old
  "50 18" entry (meant as 18:50 UTC = 2:50 PM ET) actually fired at 6:50 PM
  PACIFIC = 9:50 PM ET — hours after the close.  With the bot's force-close
  now at 15:50 ET, guardian should run at 15:55 ET as the after-close sweep:

  # 15:55 ET = 12:55 PT (PDT) Mon–Fri — for a Pacific-timezone machine:
  55 12 * * 1-5  cd /path/to/ajoy && /path/to/ajoy/.venv/bin/python guardian.py >> guardian.log 2>&1
  # (During PST, Nov–Mar, 15:55 ET = 12:55 PST — same entry works year-round
  #  since both zones shift together.)

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
from app.services.accounts import legacy_view, scope, view_from_row
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
    client: TradierClient,
    closed_symbols: set[str],
    exit_time: datetime,
    acct=None,
) -> None:
    """
    Mark any OPEN bot trades whose option_symbol was just closed as CLOSED.
    Uses ExitReason.CUTOFF since this is an end-of-day forced close.
    Exit price is taken from the actual Tradier fill when available so P&L
    stays accurate (previously left None).

    Scoped to `acct` so that closing AMZN in one account does not mark an
    AMZN position in a DIFFERENT account as closed — the two are separate
    positions that happen to share a contract symbol.
    """
    if not closed_symbols:
        return

    async with session_factory() as db:
        result = await db.execute(scope(
            select(Trade).where(Trade.status == TradeStatus.OPEN), acct)
        )
        open_trades = result.scalars().all()

        updated = 0
        for trade in open_trades:
            if trade.option_symbol in closed_symbols:
                fill = None
                try:
                    fill = await client.get_last_sell_fill(trade.option_symbol)
                except Exception:
                    pass
                trade.status      = TradeStatus.CLOSED
                trade.exit_time   = exit_time
                trade.exit_reason = ExitReason.CUTOFF
                if fill:
                    qty = trade.remaining_qty or trade.quantity
                    trade.exit_price = fill
                    trade.pnl = round(
                        (trade.pnl or 0) + (fill - trade.entry_price) * qty * 100, 2
                    )
                # else: P&L left as None — Tradier Gain/Loss page has the truth.
                updated += 1
                log.info("  DB trade #%d (%s %s) marked CLOSED%s",
                         trade.id, trade.symbol, trade.option_symbol,
                         f" @ ${fill:.2f}" if fill else " (no fill found)")

        if updated:
            await db.commit()
            log.info("  %d DB trade(s) marked CLOSED", updated)
        else:
            log.info("  No matching open DB trades found")


# ── Main ──────────────────────────────────────────────────────────────────

async def _load_accounts(session_factory) -> list:
    """
    Every account to sweep, including disabled ones.

    A disabled account can still hold a position opened before it was paused,
    and leaving that open overnight is exactly what guardian exists to stop.
    Falls back to the `.env` account when the table is missing or empty.
    """
    from app.models import Account
    try:
        async with session_factory() as db:
            result = await db.execute(
                select(Account).order_by(Account.sort_order, Account.id)
            )
            rows = list(result.scalars().all())
        if rows:
            return [view_from_row(r) for r in rows]
    except Exception as exc:
        log.warning("Could not read accounts table (%s) — using the .env account", exc)
    return [legacy_view()]


async def _sweep_account(session_factory, acct, now_utc: datetime) -> None:
    """Close every open option position in ONE account and update the DB."""
    label = acct.name if acct.id is not None else "Primary (.env)"
    log.info("-" * 60)
    log.info("Account: %s  (%s, account %s)",
             label, "SANDBOX" if acct.use_sandbox else "LIVE",
             acct.account_number or "?")

    client = TradierClient(acct if acct.id is not None else None)

    # ── 1. Fetch open positions ──────────────────────────────────────────
    try:
        positions = await client.get_positions()
    except Exception as exc:
        log.error("[%s] Could not fetch positions from Tradier: %s", label, exc)
        log.error("[%s] SKIPPING this account — no positions were closed. "
                  "CHECK TRADIER MANUALLY.", label)
        return

    option_positions = [
        p for p in positions
        if _is_option_symbol(p.symbol) and p.quantity != 0
    ]

    if not option_positions:
        log.info("[%s] No open option positions found — nothing to close.", label)
        return

    log.info("[%s] Found %d open option position(s):", label, len(option_positions))
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
        log.warning("[%s] Could not list pending orders: %s", label, exc)

    # ── 2. Close each position ───────────────────────────────────────────
    log.info("[%s] Placing sell-to-close orders...", label)
    closed_symbols: set[str] = set()

    for pos in option_positions:
        qty = abs(pos.quantity)   # qty is always positive for sell-to-close
        ok  = await _close_position(client, pos.symbol, qty)
        if ok:
            closed_symbols.add(pos.symbol)

    log.info("[%s] %d / %d position(s) closed successfully.",
             label, len(closed_symbols), len(option_positions))

    # Give the market sells a moment to fill so we can record real exit prices.
    if closed_symbols:
        await asyncio.sleep(5)

    # ── 3. Update bot DB (scoped to this account) ────────────────────────
    log.info("[%s] Updating bot DB...", label)
    await _mark_db_trades_closed(session_factory, client, closed_symbols,
                                 now_utc, acct)
    await client.close()


async def run() -> None:
    now_utc = datetime.now(tz=timezone.utc)
    log.info("=" * 60)
    log.info("Guardian starting at %s UTC", now_utc.strftime("%Y-%m-%d %H:%M:%S"))

    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Schema-drift guard: guardian runs standalone, so the DB may lack
        # columns added since the bot's last restart (failed once with
        # "no such column: trades.runner_mode").  The app's migrations are
        # idempotent — run them BEFORE anything reads the tables.  This now
        # also has to happen before the account list is read, since the
        # accounts table itself may not exist yet on an upgrading install.
        from app.database import Base, _migrate
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _migrate(conn)

        accounts = await _load_accounts(Session)
        log.info("Sweeping %d account(s): %s", len(accounts),
                 ", ".join(a.name for a in accounts))

        for acct in accounts:
            try:
                await _sweep_account(Session, acct, now_utc)
            except Exception as exc:
                # One bad account must never stop the others from being
                # flattened — that is the whole point of this script.
                log.error("Account '%s' sweep FAILED: %s — CHECK TRADIER MANUALLY",
                          acct.name, exc, exc_info=True)
    finally:
        await engine.dispose()

    log.info("=" * 60)
    log.info("Guardian finished at %s UTC",
             datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
