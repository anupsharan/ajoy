"""
reconcile.py — P&L reconciliation between the ajoy database and Tradier sandbox orders.

Usage:
    python reconcile.py            # today only
    python reconcile.py 2026-05-21 # specific date

The script prints a side-by-side table showing:
  • DB entry/exit prices vs Tradier actual fill prices
  • Computed P&L (DB) vs actual P&L (Tradier fills)
  • Any mismatches flagged with  ◄ MISMATCH
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
cfg = dotenv_values(".env")
TOKEN_SANDBOX = cfg.get("TRADIER_API_TOKEN_SANDBOX", "")
ACCOUNT_ID    = cfg.get("TRADIER_ACCOUNT_ID", "")
BASE_SANDBOX  = "https://sandbox.tradier.com/v1"
DB_PATH       = "ajoy.db"

HEADERS = {
    "Authorization": f"Bearer {TOKEN_SANDBOX}",
    "Accept": "application/json",
}

TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


# ---------------------------------------------------------------------------
# Fetch DB trades
# ---------------------------------------------------------------------------
def load_db_trades(target_date: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, direction, option_symbol,
               tradier_order_id,
               quantity, entry_price, exit_price,
               entry_time, exit_time, exit_reason,
               (COALESCE(exit_price,0) - COALESCE(entry_price,0))
                   * quantity * 100 AS db_pnl
        FROM trades
        WHERE DATE(entry_time) = ?
        ORDER BY entry_time
    """, (target_date,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Fetch Tradier sandbox orders
# ---------------------------------------------------------------------------
async def fetch_tradier_orders() -> dict[str, dict]:
    """Return a map of order_id → order dict for ALL orders in the account."""
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        resp = await client.get(
            f"{BASE_SANDBOX}/accounts/{ACCOUNT_ID}/orders",
            headers=HEADERS,
            params={"includeTags": "false"},
        )
        resp.raise_for_status()
        data = resp.json()

    orders_raw = data.get("orders", {})
    if not orders_raw or orders_raw == "null":
        return {}

    orders = orders_raw.get("order", [])
    if isinstance(orders, dict):
        orders = [orders]

    return {str(o["id"]): o for o in orders}


# ---------------------------------------------------------------------------
# Match & reconcile
# ---------------------------------------------------------------------------
def reconcile(trades: list[dict], orders: dict[str, dict]) -> list[dict]:
    rows = []
    for t in trades:
        oid = str(t["tradier_order_id"] or "")
        order = orders.get(oid)

        if order is None:
            fill_buy  = None
            fill_sell = None
            tradier_pnl = None
            note = "ORDER NOT FOUND IN TRADIER"
        else:
            # An option round-trip creates TWO orders linked by the option symbol:
            # one buy_to_open and one sell_to_close (or sell_to_open / buy_to_close).
            # The DB stores ONLY the buy order ID.  We need to find the matching sell.
            buy_fill  = float(order.get("avg_fill_price") or 0)
            fill_buy  = buy_fill

            # Find the paired sell order: same option_symbol, opposite side, same date
            opt_sym   = order.get("option_symbol") or ""
            buy_date  = (order.get("create_date") or "")[:10]
            buy_side  = (order.get("side") or "").lower()

            sell_side = "sell_to_close" if "buy" in buy_side else "buy_to_close"
            sell_order = None
            for o in orders.values():
                if (
                    (o.get("option_symbol") or "") == opt_sym
                    and (o.get("side") or "").lower() == sell_side
                    and (o.get("create_date") or "")[:10] == buy_date
                ):
                    sell_order = o
                    break

            fill_sell = float(sell_order.get("avg_fill_price") or 0) if sell_order else None
            qty       = t["quantity"]

            if fill_sell:
                tradier_pnl = round((fill_sell - fill_buy) * qty * 100, 2)
            else:
                tradier_pnl = None

            db_pnl = round(t["db_pnl"] or 0, 2)
            if tradier_pnl is not None:
                diff = round(tradier_pnl - db_pnl, 2)
                note = f"◄ MISMATCH diff=${diff:+.2f}" if abs(diff) >= 0.01 else "OK"
            else:
                note = "SELL ORDER NOT FOUND"

        rows.append({
            **t,
            "fill_buy":    fill_buy,
            "fill_sell":   fill_sell,
            "tradier_pnl": tradier_pnl,
            "note":        note,
        })
    return rows


# ---------------------------------------------------------------------------
# Print report
# ---------------------------------------------------------------------------
def print_report(rows: list[dict]) -> None:
    print(f"\n{'='*110}")
    print(f"  Ajoy ↔ Tradier P&L Reconciliation  —  {TARGET_DATE}")
    print(f"{'='*110}")

    hdr = (f"  {'Symbol':6} {'Dir':4} {'Qty':3}  "
           f"{'DB Entry':>9} {'DB Exit':>8}  {'DB P&L':>8}  "
           f"{'Tdier Buy':>9} {'Tdier Sell':>10}  {'Tdier P&L':>9}  "
           f"{'Reason':16}  Status")
    print(hdr)
    print(f"  {'-'*105}")

    db_total     = 0.0
    tradier_total = 0.0
    mismatches   = 0

    for r in rows:
        db_pnl      = r["db_pnl"] or 0
        tdier_pnl   = r["tradier_pnl"]
        db_total   += db_pnl
        if tdier_pnl is not None:
            tradier_total += tdier_pnl
        else:
            tradier_total  = None  # can't total if any are missing

        fb = f"${r['fill_buy']:.2f}"   if r["fill_buy"]  is not None else "    —  "
        fs = f"${r['fill_sell']:.2f}"  if r["fill_sell"] is not None else "     —  "
        tp = f"${tdier_pnl:+.2f}"      if tdier_pnl       is not None else "       —"

        mismatch_star = "◄" if "MISMATCH" in r["note"] else " "
        if "MISMATCH" in r["note"]:
            mismatches += 1

        print(f"  {r['symbol']:6} {r['direction']:4} {r['quantity']:3}  "
              f"${r['entry_price']:8.2f} ${r['exit_price'] or 0:7.2f}  "
              f"${db_pnl:+7.2f}  "
              f"{fb:>9} {fs:>10}  {tp:>9}  "
              f"{r['exit_reason']:16}  {r['note']}  {mismatch_star}")

    print(f"  {'-'*105}")
    tdier_str = f"${tradier_total:+.2f}" if tradier_total is not None else "N/A (missing orders)"
    print(f"  {'TOTAL':6}                         ${db_total:+7.2f}  "
          f"{'':>9} {'':>10}  {tdier_str:>9}")
    print()

    if mismatches:
        print(f"  ⚠  {mismatches} mismatch(es) found.  "
              "Possible causes: sandbox fills differ from production ask price used for DB entry.")
    else:
        print("  ✓  All matched P&L records agree within $0.01.")

    print(f"{'='*110}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    trades = load_db_trades(TARGET_DATE)
    if not trades:
        print(f"No trades in DB for {TARGET_DATE}")
        return

    print(f"Loaded {len(trades)} trades from DB for {TARGET_DATE}.")
    print("Fetching Tradier sandbox orders...")

    try:
        orders = await fetch_tradier_orders()
        print(f"Fetched {len(orders)} total orders from Tradier sandbox.")
    except Exception as exc:
        print(f"ERROR fetching Tradier orders: {exc}")
        print("Showing DB-only totals:")
        orders = {}

    rows = reconcile(trades, orders)
    print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
