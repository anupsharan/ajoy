#!/usr/bin/env python3
"""
scripts/reset_db.py — Pre-live database reset.

Backs up the sandbox DB, then wipes all trade history so ajoy starts
fresh on go-live day with no sandbox noise polluting:
  • P&L totals
  • Daily trade / loss caps
  • Cooldown timers

Symbols and Indicators are preserved — your watchlist stays intact.

Usage:
  cd /path/to/ajoy
  python scripts/reset_db.py
"""

import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ── make app/ importable ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models import Trade


async def reset() -> None:
    # ── 1. Backup ────────────────────────────────────────────────────────
    db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))
    if not db_path.is_absolute():
        db_path = Path(__file__).parent.parent / db_path

    if db_path.exists():
        stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup  = db_path.with_name(f"ajoy_sandbox_backup_{stamp}.db")
        shutil.copy2(db_path, backup)
        print(f"✓ Backup saved → {backup}")
    else:
        print("  No existing DB found — starting fresh.")

    # ── 2. Wipe trades ───────────────────────────────────────────────────
    engine  = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(delete(Trade))
        await db.commit()
        print(f"✓ {result.rowcount} trade record(s) deleted.")

    # ── 3. Compact the DB ────────────────────────────────────────────────
    async with engine.begin() as conn:
        await conn.execute(text("VACUUM"))
    print("✓ DB compacted (VACUUM).")

    await engine.dispose()

    print()
    print("━" * 50)
    print("  DB reset complete — ready for go-live.")
    print("  Symbols and Indicators are untouched.")
    print("━" * 50)


if __name__ == "__main__":
    confirm = input(
        "\nThis will DELETE all trade history.\n"
        "A backup will be saved first.\n\n"
        "Type 'yes' to continue: "
    )
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        sys.exit(0)
    asyncio.run(reset())
