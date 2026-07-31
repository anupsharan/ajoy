#!/usr/bin/env python3
"""
Watchlist surgery — Jul 30 2026.

Run from the project folder:      python3 watchlist_update.py
Preview without writing:          python3 watchlist_update.py --dry-run
Undo:                             python3 watchlist_update.py --revert

WHY EACH REMOVAL
----------------
Measured over Jul 27-30 logs + every trade since Jul 15.  Each of these
produced signals that could never become a trade — they burned scan cycles
and inflated the funnel with candidates the contract stage always rejected:

    SOFI   41 "contract too cheap" blocks, 0 trades   ($15.94 -> ~$0.20 ATM)
    VTI    16 "spread too wide" blocks,   0 trades    (ETF, 24.8% spreads)
    CELH   15 too-cheap,                  0 trades    ($28.92 -> ~$0.36)
    F      14 too-cheap,                  0 trades    ($14.81 -> ~$0.19)
    JOBY   10 too-cheap,                  0 trades    ($7.08  -> ~$0.09)
    UBS    10 too-cheap,                  0 trades    ($53.15 -> ~$0.67)
    RIVN    0 trades                                  ($16.83 -> hopeless)

OPTION_MIN_PREMIUM=1.00 needs roughly  price x IV >= 40.  None of the above
can reach it.  They are deactivated, NOT deleted — every row and its history
stays, so --revert puts them straight back.

WHY EACH ADDITION
-----------------
Prices verified 2026-07-30 close.  Estimated ATM 1-DTE premium uses
prem ~ 0.4 x S x IV x sqrt(1/252); the band that clears both the $1.00 floor
and the $500/$600 budget cap is roughly $1.00-$5.00.

    QCOM   $151.60   ~$1.34   low end of the band, liquid chain
    TSLA   $308.85   ~$3.89   most liquid single-stock options in the US
    MSFT   $451.10   ~$3.41   very liquid (note: +15.5% earnings move Jul 30,
                              so near-term IV is elevated and the real premium
                              will run higher than this estimate for a few days)
    META   $539.03   ~$4.75   liquid, but this sits near the $500 S1 budget cap
                              so expect it to trade mostly on S2 (cap $600)

The IV figures are estimates, not quotes.  That is deliberately low-risk: a
symbol whose real premium lands out of band is simply skipped by the existing
min-premium / budget gates — the cost of a wrong guess is wasted scans, which
is exactly what the removals above just bought back.

NO QUALITY GATE IS TOUCHED.  Every added symbol runs the identical stack.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent / "ajoy.db"

REMOVE = ["SOFI", "VTI", "CELH", "F", "JOBY", "UBS", "RIVN"]
ADD    = ["QCOM", "TSLA", "MSFT", "META"]


def show(cur, title: str) -> None:
    rows = cur.execute(
        "SELECT ticker, s1_enabled, s2_enabled FROM symbols "
        "WHERE active=1 ORDER BY ticker"
    ).fetchall()
    print(f"\n{title}  ({len(rows)} active)")
    print("  " + "  ".join(
        f"{t}{'' if (a and b) else '(' + ('S1' if a else 'S2') + ')'}"
        for t, a, b in rows))


def main() -> int:
    dry    = "--dry-run" in sys.argv
    revert = "--revert" in sys.argv

    if not DB.exists():
        print(f"ERROR: {DB} not found — run this from the ajoy project folder.")
        return 1

    if not dry:
        backup = DB.with_suffix(".db.bak-watchlist-script")
        shutil.copy2(DB, backup)
        print(f"backup: {backup.name}")

    # Opening read-write also rolls back any stale journal left by a crashed
    # writer, which is the correct and safe way to clear one.
    db = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    db.execute("PRAGMA busy_timeout=30000")
    cur = db.cursor()

    show(cur, "BEFORE")

    cur.execute("BEGIN IMMEDIATE")
    try:
        if revert:
            for t in REMOVE:
                cur.execute("UPDATE symbols SET active=1, s1_enabled=1, s2_enabled=1 "
                            "WHERE ticker=?", (t,))
            for t in ADD:
                cur.execute("UPDATE symbols SET active=0, s1_enabled=0, s2_enabled=0 "
                            "WHERE ticker=?", (t,))
            print("\nreverting to the previous watchlist")
        else:
            for t in REMOVE:
                cur.execute("UPDATE symbols SET active=0, s1_enabled=0, s2_enabled=0 "
                            "WHERE ticker=?", (t,))
                print(f"  off  {t:<6} ({cur.rowcount} row)")
            for t in ADD:
                if cur.execute("SELECT 1 FROM symbols WHERE ticker=?", (t,)).fetchone():
                    cur.execute("UPDATE symbols SET active=1, s1_enabled=1, "
                                "s2_enabled=1, s3_enabled=0 WHERE ticker=?", (t,))
                    print(f"  on   {t:<6} (existing row re-enabled)")
                else:
                    cur.execute(
                        "INSERT INTO symbols (ticker, strategy, active, s1_enabled, "
                        "s2_enabled, s3_enabled) VALUES (?, 'S1', 1, 1, 1, 0)", (t,))
                    print(f"  add  {t:<6} (new)")

        if dry:
            cur.execute("ROLLBACK")
            print("\n--dry-run: nothing written")
        else:
            cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    show(cur, "AFTER")
    db.close()

    if not dry:
        print("\nThe scanners re-read the symbols table every scan — no restart needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
