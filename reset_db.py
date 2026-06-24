#!/usr/bin/env python3
"""
Reset the a-joy database.

    uv run python reset_db.py          # interactive — asks for confirmation
    uv run python reset_db.py --force  # skip confirmation prompt
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path


def _db_path() -> Path:
    """Resolve the SQLite file path from config."""
    try:
        from app.config import settings
        url = settings.database_url  # e.g. sqlite+aiosqlite:///./ajoy.db
        rel = url.split("///")[-1]   # ./ajoy.db  or  /abs/path/ajoy.db
        return Path(rel).resolve()
    except Exception:
        return Path("ajoy.db").resolve()


async def _recreate_schema() -> None:
    """Re-create all tables and re-seed indicators."""
    from app.database import AsyncSessionLocal, init_db
    from app.services.indicators import seed_indicators

    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_indicators(db)
    print("  Schema created and indicators seeded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset the a-joy SQLite database.")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    db_path = _db_path()

    print(f"\n  Database: {db_path}")

    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        print(f"  Size    : {size_kb:.1f} KB")
    else:
        print("  Status  : file does not exist yet")

    if not args.force:
        answer = input("\n  Delete and recreate? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    # Delete
    if db_path.exists():
        os.remove(db_path)
        print(f"  Deleted : {db_path.name}")

    # Recreate
    print("  Creating fresh schema…")
    asyncio.run(_recreate_schema())
    print("\n  Done — database is clean. Restart the server.\n")


if __name__ == "__main__":
    main()
