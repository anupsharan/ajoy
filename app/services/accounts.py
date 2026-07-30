"""
Multi-account support (Jul 25 2026).

Before this module the bot was hard-wired to the single Tradier account in
`.env`.  Now every brokerage account lives in the `accounts` table with its
own credentials, its own strategy enrolment and its own sizing/slot limits,
and the scheduler runs the whole signal stack once per ENABLED account.

The unit that travels through the codebase is `AccountView` — an immutable
snapshot of one account row.  It is deliberately NOT a SQLAlchemy object:
scanners run concurrently in separate DB sessions, and a detached ORM row
would blow up on lazy attribute access.  A view is cheap to copy, safe to
share across coroutines, and carries a `setting()` helper that resolves
per-account overrides against the global `settings` singleton.

Back-compatibility contract
---------------------------
`legacy_view()` reproduces the pre-multi-account behaviour exactly: the
credentials come from `.env`, `id` is None, and `id is None` means "do not
filter trades by account" — so every existing test that creates trades with
no account_id keeps passing, and a database that has never been migrated
still trades normally.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select, update

from app.config import settings

logger = logging.getLogger(__name__)


# Settings that an account may override.  Anything not listed here is global
# and shared by every account (entry windows, gates, exit tuning, …) — see the
# design note in CLAUDE.md §10.  NULL in the DB column = inherit the global.
OVERRIDABLE: tuple[str, ...] = (
    "max_open_trades",
    "risk_per_trade",
    "amount_per_trade",
    "max_daily_loss",
    "s2_max_open_trades",
    "s2_risk_per_trade",
    "s2_amount_per_trade",
    "s2_max_daily_loss",
    "put_scalp_max_open",
    "put_scalp_risk_per_trade",
)

# Per-account strategy toggles, in the order the UI shows them.
STRATEGY_FLAGS: tuple[str, ...] = (
    "s1_enabled",
    "s2_enabled",
    "s3_enabled",
    "put_scalp_enabled",
)


@dataclass(frozen=True)
class AccountView:
    """Immutable snapshot of one brokerage account."""

    id: int | None
    name: str
    account_number: str
    api_token: str
    data_api_token: str = ""
    use_sandbox: bool = True
    enabled: bool = True
    is_primary: bool = False
    s1_enabled: bool = True
    s2_enabled: bool = True
    s3_enabled: bool = False
    put_scalp_enabled: bool = True
    overrides: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def setting(self, key: str) -> Any:
        """
        Resolve a setting for THIS account: the per-account override when one
        is set, otherwise the global value from `.env`.

        Only keys in OVERRIDABLE can be overridden; anything else falls
        straight through to the global settings object, so callers can use
        this uniformly without checking first.
        """
        val = self.overrides.get(key)
        if val is None:
            return getattr(settings, key)
        return val

    def strategy_enabled(self, flag: str) -> bool:
        """Per-account enrolment for one strategy (s1/s2/s3/put_scalp)."""
        return bool(getattr(self, flag, False))

    @property
    def label(self) -> str:
        """Short human label for log lines: 'Primary#1'."""
        return f"{self.name}#{self.id}" if self.id else self.name

    @property
    def client_key(self) -> tuple:
        """
        Cache key for the Tradier client.  Includes the credentials so that
        editing a token in the UI produces a NEW client instead of silently
        reusing the old authenticated one.
        """
        return (self.id, self.account_number, self.api_token,
                self.data_api_token, self.use_sandbox)


# ---------------------------------------------------------------------------
# Legacy / fallback view
# ---------------------------------------------------------------------------

def legacy_view() -> AccountView:
    """
    The pre-multi-account account, built from `.env`.

    Returned when the accounts table is empty or unreachable, and used as the
    default for any code path that has no account in hand (tests, ad-hoc
    scripts).  `id=None` is meaningful: it disables account filtering on trade
    queries, which is exactly the old single-account behaviour.
    """
    return AccountView(
        id=None,
        name="Primary (.env)",
        account_number=(settings.tradier_account_id_sandbox if settings.use_sandbox
                        else settings.tradier_account_id),
        api_token=(settings.tradier_api_token_sandbox if settings.use_sandbox
                   else settings.tradier_api_token),
        data_api_token=settings.tradier_api_token,
        use_sandbox=settings.use_sandbox,
        enabled=True,
        is_primary=True,
        s1_enabled=True,
        s2_enabled=True,
        s3_enabled=True,
        put_scalp_enabled=True,
        overrides={},
    )


def view_from_row(row) -> AccountView:
    """Build an AccountView from an `accounts` ORM row."""
    return AccountView(
        id=row.id,
        name=row.name,
        account_number=row.account_number or "",
        api_token=row.api_token or "",
        # Blank per-account data token → share the global production token.
        # Market data is account-agnostic so this is the normal setup.
        data_api_token=(row.data_api_token or settings.tradier_api_token),
        use_sandbox=bool(row.use_sandbox),
        enabled=bool(row.enabled),
        is_primary=bool(row.is_primary),
        s1_enabled=bool(row.s1_enabled),
        s2_enabled=bool(row.s2_enabled),
        s3_enabled=bool(row.s3_enabled),
        put_scalp_enabled=bool(row.put_scalp_enabled),
        overrides={k: getattr(row, k) for k in OVERRIDABLE
                   if getattr(row, k, None) is not None},
    )


def account_of(client) -> AccountView:
    """
    Return the AccountView a Tradier client belongs to.

    Falls back to `legacy_view()` for clients that carry no account — that
    includes every mock client in the test-suite, which is what keeps the
    ~578 pre-existing tests passing unchanged.
    """
    acct = getattr(client, "ajoy_account", None)
    return acct if isinstance(acct, AccountView) else legacy_view()


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------

async def list_account_rows(db) -> list:
    """All account rows, ordered for display.  [] if the table doesn't exist."""
    from app.models import Account
    try:
        result = await db.execute(
            select(Account).order_by(Account.sort_order, Account.id)
        )
        return list(result.scalars().all())
    except Exception as exc:          # table missing on a pre-migration DB
        logger.debug("list_account_rows: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Account snapshot cache
#
# The scanners, the trade manager and the startup jobs all need the account
# list, several times a minute.  Without a cache that is a stream of tiny
# read transactions against a SQLite file that the same process is also
# writing trades to — which produces "database is locked" contention rather
# than any useful freshness.  One short-lived snapshot per couple of seconds
# is plenty: accounts change when a human edits them, and the router
# invalidates the cache explicitly when that happens.
# ---------------------------------------------------------------------------

_CACHE_TTL: float = 3.0
_cache: tuple[float, list[AccountView]] | None = None
_cache_lock: "asyncio.Lock | None" = None


def invalidate_account_cache() -> None:
    """Force the next lookup to re-read the accounts table."""
    global _cache
    _cache = None


def _get_cache_lock():
    """Lazily create the lock — it must bind to the running event loop."""
    global _cache_lock
    import asyncio
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def _load_account_views(force: bool = False) -> list[AccountView]:
    """
    All account views, cached briefly.  Empty list = table exists but empty.

    The lock is a stampede guard: several scanners plus the manager can ask
    for the account list in the same instant, and without it each one opens
    its own SQLite read transaction while the process is also writing trades.
    One reader refreshes; the rest get the fresh snapshot for free.
    """
    global _cache
    import time as _time

    def _fresh():
        return (_cache is not None
                and (_time.monotonic() - _cache[0]) < _CACHE_TTL)

    if not force and _fresh():
        return _cache[1]

    async with _get_cache_lock():
        # Someone may have refreshed while we waited for the lock.
        if not force and _fresh():
            return _cache[1]

        from app.database import AsyncSessionLocal
        from app.models import Account
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Account).order_by(Account.sort_order, Account.id)
                )
                views = [view_from_row(r) for r in result.scalars().all()]
        except Exception as exc:
            # Table missing (pre-migration DB) or transient read error: do NOT
            # cache this, and let the caller fall back to the .env account.
            logger.warning(
                "accounts: could not read the accounts table (%s) — "
                "falling back to the .env account", exc,
            )
            raise

        _cache = (_time.monotonic(), views)
        return views


async def active_account_views() -> list[AccountView]:
    """
    Every account the scanners should trade this tick.

    An empty or missing table falls back to the single `.env` account so a
    database that predates this feature keeps trading exactly as before.
    An explicit "every account disabled" returns [] — that is a deliberate
    stop, and must NOT silently resurrect .env trading.
    """
    try:
        views = await _load_account_views()
    except Exception:
        return [legacy_view()]
    if not views:
        return [legacy_view()]
    return [v for v in views if v.enabled]


async def all_account_views() -> list[AccountView]:
    """
    Every account including disabled ones — used by the trade MANAGER.

    Disabling an account stops new entries but must never abandon an open
    position: the manager still needs a working client to exit it.
    """
    try:
        views = await _load_account_views()
    except Exception:
        return [legacy_view()]
    return views or [legacy_view()]


async def get_account_view(db, account_id: int | None) -> AccountView:
    """Look up one account by id; primary (or legacy) when not found."""
    from app.models import Account
    if account_id is not None:
        try:
            result = await db.execute(select(Account).where(Account.id == account_id))
            row = result.scalar_one_or_none()
            if row is not None:
                return view_from_row(row)
        except Exception as exc:
            logger.warning("get_account_view(%s) failed: %s", account_id, exc)
    return await primary_account_view(db)


async def primary_account_view(db) -> AccountView:
    """The primary account row, or the `.env` account when there is none."""
    from app.models import Account
    try:
        result = await db.execute(
            select(Account)
            .where(Account.is_primary == True)            # noqa: E712
            .order_by(Account.id)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return view_from_row(row)
        # No primary flagged — fall back to the lowest-id account.
        result = await db.execute(select(Account).order_by(Account.id).limit(1))
        row = result.scalar_one_or_none()
        if row is not None:
            return view_from_row(row)
    except Exception as exc:
        logger.debug("primary_account_view: %s", exc)
    return legacy_view()


async def account_view_for_trade(db, trade) -> AccountView:
    """
    The account that HOLDS this trade — the only client allowed to sell it.

    A trade with no account_id predates the accounts table and belongs to the
    primary account.
    """
    return await get_account_view(db, getattr(trade, "account_id", None))


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

async def seed_default_account(db) -> AccountView | None:
    """
    One-time migration of the `.env` account into the accounts table.

    Runs on every startup and is idempotent: it does nothing once any account
    exists.  After inserting the primary row it backfills `trades.account_id`
    so historical positions (and any position still OPEN at upgrade time) are
    owned by the account that actually holds them.
    """
    from app.models import Account, Trade
    from sqlalchemy import func as sqlfunc

    try:
        existing = await db.execute(select(sqlfunc.count(Account.id)))
        if int(existing.scalar() or 0) > 0:
            return None
    except Exception as exc:
        logger.warning("seed_default_account: accounts table unavailable (%s)", exc)
        return None

    live_id  = (settings.tradier_account_id or "").strip()
    sand_id  = (settings.tradier_account_id_sandbox or "").strip()
    use_sand = bool(settings.use_sandbox)
    number   = sand_id if use_sand else live_id
    token    = (settings.tradier_api_token_sandbox if use_sand
                else settings.tradier_api_token) or ""

    row = Account(
        name="Primary",
        broker="tradier",
        account_number=number,
        api_token=token.strip(),
        data_api_token="",          # blank → share the global market-data token
        use_sandbox=use_sand,
        enabled=True,
        is_primary=True,
        sort_order=0,
        notes="Migrated from .env on first startup after multi-account support.",
        # Enrol in everything the .env master switches already allow; the
        # global switches still gate each strategy, so this changes nothing
        # about what trades today.
        s1_enabled=True,
        s2_enabled=True,
        s3_enabled=True,
        put_scalp_enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    # Backfill every pre-existing trade to this account.
    try:
        await db.execute(
            update(Trade)
            .where(Trade.account_id.is_(None))
            .values(account_id=row.id)
        )
        await db.commit()
    except Exception as exc:
        logger.warning("seed_default_account: trade backfill failed: %s", exc)

    invalidate_account_cache()
    logger.info(
        "[accounts] Seeded primary account '%s' (%s, %s) from .env and "
        "backfilled existing trades",
        row.name, number or "no account id", "SANDBOX" if use_sand else "LIVE",
    )
    return view_from_row(row)


# ---------------------------------------------------------------------------
# Query scoping
# ---------------------------------------------------------------------------

def account_clause(acct: AccountView | None):
    """
    SQLAlchemy filter restricting a Trade query to one account.

    Returns None when no filtering should happen — i.e. for the legacy view
    (`id is None`), which preserves the single-account semantics that every
    existing test relies on.  Callers do:

        clause = account_clause(acct)
        stmt = select(Trade).where(Trade.status == OPEN)
        if clause is not None:
            stmt = stmt.where(clause)
    """
    from app.models import Trade
    if acct is None or acct.id is None:
        return None
    if acct.is_primary:
        # The primary account also owns rows written before the migration,
        # in the (unlikely) event the backfill missed any.
        return (Trade.account_id == acct.id) | (Trade.account_id.is_(None))
    return Trade.account_id == acct.id


def scope(stmt, acct: AccountView | None):
    """Apply account_clause() to a select() if one applies."""
    clause = account_clause(acct)
    return stmt if clause is None else stmt.where(clause)
