from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# `timeout` is SQLite's busy-handler window: how long a writer waits for a
# competing transaction before raising "database is locked".  The default is
# 5 s, which was fine when one scanner touched the DB.  With multi-account the
# process runs several scanners, a manager and the startup jobs concurrently,
# so brief overlaps are normal and should WAIT rather than fail an entry or —
# far worse — an exit.  30 s is far longer than any query here takes.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"timeout": 30} if settings.database_url.startswith("sqlite") else {},
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables, then run lightweight column migrations."""
    async with engine.begin() as conn:
        from app import models  # noqa: F401 — registers all models
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn) -> None:
    """
    Add columns that didn't exist in earlier versions of the schema.
    SQLite doesn't support IF NOT EXISTS on ALTER TABLE, so we swallow
    the error if the column already exists.
    Each statement is idempotent — safe to run on every startup.
    """
    migrations = [
        # v0 → v1: indicator key slug
        "ALTER TABLE indicators ADD COLUMN key VARCHAR(50) NOT NULL DEFAULT ''",

        # v1 → v2: trade state flags (added when partial-exit / BE-stop logic landed)
        "ALTER TABLE trades ADD COLUMN tp1_hit BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN be_stop_set BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN remaining_qty INTEGER",

        # v2 → v3: entry context columns for exit logic
        "ALTER TABLE trades ADD COLUMN underlying_entry FLOAT",
        "ALTER TABLE trades ADD COLUMN vwap_at_entry FLOAT",

        # v3 → v4: broker-side resting stop order id
        "ALTER TABLE trades ADD COLUMN stop_order_id VARCHAR(50)",

        # v4 → v5: strategy tag on symbols (S1 = VWAP pullback, S2 = EMA cross)
        "ALTER TABLE symbols ADD COLUMN strategy VARCHAR(20) NOT NULL DEFAULT 'S1'",

        # v5 → v6: drop the old UNIQUE index on symbols.ticker so the same ticker
        # can appear in both S1 and S2 symbol lists.  DROP INDEX has no IF NOT EXISTS
        # in older SQLite, so errors are swallowed by the try/except like all others.
        "DROP INDEX ix_symbols_ticker",
        # Recreate as a plain (non-unique) index — ticker lookups stay fast.
        "CREATE INDEX IF NOT EXISTS ix_symbols_ticker ON symbols (ticker)",

        # v6 → v7: broker-side resting TP limit order id
        "ALTER TABLE trades ADD COLUMN tp_order_id VARCHAR(50)",

        # v7 → v8: unified watchlist — per-symbol strategy flags replace the old
        # strategy column (which forced S1 XOR S2).  Both default to 1 so every
        # existing symbol is enrolled in both strategies; use the UI to opt out.
        "ALTER TABLE symbols ADD COLUMN s1_enabled BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE symbols ADD COLUMN s2_enabled BOOLEAN NOT NULL DEFAULT 1",
        # Deduplicate rows: previously the same ticker could appear twice (once as
        # strategy='S1' and once as strategy='S2').  Keep the lowest-id row per ticker.
        "DELETE FROM symbols WHERE id NOT IN (SELECT MIN(id) FROM symbols GROUP BY ticker)",
        # Re-add a unique index on ticker now that duplicates are gone.
        "CREATE UNIQUE INDEX IF NOT EXISTS uix_symbols_ticker ON symbols (ticker)",

        # v8 → v9: runner mode — TP waived at the target when momentum is
        # strong; the trade is managed by the runner trail from that point on.
        "ALTER TABLE trades ADD COLUMN runner_mode BOOLEAN NOT NULL DEFAULT 0",

        # v9 → v10: per-symbol S3 enrollment (stocks / Moomoo engine).
        # Defaults to 1 like s1/s2 — use the Symbols page to opt out of
        # symbols whose share price is too high for the S3 capital
        # allocation (the s3_min_shares guard also skips those entries).
        "ALTER TABLE symbols ADD COLUMN s3_enabled BOOLEAN NOT NULL DEFAULT 1",

        # v10 → v11: human-set TP flag — runner mode never waives a manual
        # target (GOOGL #140: user's $3.84 TP was waived and trailed to a loss)
        "ALTER TABLE trades ADD COLUMN tp_manual BOOLEAN NOT NULL DEFAULT 0",

        # v11 → v12: entry-time stop snapshot — STOP/TRAILING_STOP labels are
        # decided against this instead of a settings-derived percentage
        "ALTER TABLE trades ADD COLUMN original_stop_price FLOAT",

        # v12 → v13: first trend+thesis conflict timestamp (SIGNAL_FADE exit)
        "ALTER TABLE trades ADD COLUMN signal_conflict_time DATETIME",

        # v13 → v14: multi-account support (Jul 25 2026).  Every trade now
        # records WHICH brokerage account holds it, so exits/cancels are
        # routed to the right Tradier client.  Existing rows stay NULL here
        # and are backfilled to the primary account by seed_default_account().
        "ALTER TABLE trades ADD COLUMN account_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_trades_account_id ON trades (account_id)",
    ]
    for stmt in migrations:
        try:
            await conn.execute(text(stmt))
        except Exception:
            pass  # column already exists — fine
