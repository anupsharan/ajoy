#!/usr/bin/env python3
"""
One-time DB patch — corrects today's mis-recorded exits.
Run from the ajoy/ directory:  python patch_db_today.py
Requires the bot to be running (uses HTTP admin endpoint).
"""
import urllib.request, json, sys

BASE = "http://localhost:8000"

patches = [
    # (trade_id, symbol, tradier_fill, correct_pnl, description)
    (31, "INTC PUT",  3.65, -5.00,  "Tradier filled @$3.65 — DB had stale after-hours quote $5.25"),
    (30, "NOW PUT #2", 2.35,  0.00,  "Tradier filled @$2.35 — DB had wrong exit $2.30"),
]

for trade_id, label, exit_price, pnl, note in patches:
    url = f"{BASE}/admin/patch-trade-pnl?trade_id={trade_id}&exit_price={exit_price}&pnl={pnl}"
    try:
        with urllib.request.urlopen(url, data=b"", timeout=5) as r:
            result = json.loads(r.read())
            old = result["old"]
            print(f"✅ Trade {trade_id} {label}")
            print(f"   exit:  ${old['exit_price']:.2f} → ${exit_price:.2f}")
            print(f"   P&L:   ${old['pnl']:.2f} → ${pnl:.2f}")
            print(f"   note:  {note}")
    except Exception as e:
        print(f"❌ Trade {trade_id} {label}: {e}")
        print(f"   If the bot isn't running, start it first then re-run this script.")

print("\nDone. Refresh the UI to see updated P&Ls.")
