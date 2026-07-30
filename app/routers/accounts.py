"""
Accounts API — manage the brokerage accounts the bot trades (Jul 25 2026).

    GET    /api/accounts                 list accounts (tokens masked)
    POST   /api/accounts                 add an account
    PATCH  /api/accounts/{id}            edit name / credentials / toggles / sizing
    DELETE /api/accounts/{id}            remove an account (blocked while it has open trades)
    POST   /api/accounts/{id}/verify     live credential check against Tradier
    GET    /api/accounts/{id}/balances   balances for one account
    GET    /api/accounts/summary         per-account open-trade count + today's P&L

Security notes
--------------
Tokens are write-only over the API: reads return `api_token_masked` and never
the secret.  Editing an account evicts its cached Tradier client so the next
scan authenticates with the new credentials rather than the stale ones.

Deleting is deliberately conservative: an account holding an OPEN trade cannot
be removed, because deleting it would orphan a live position that nothing
would then manage or exit.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Account, Trade, TradeStatus
from app.schemas import AccountCreate, AccountOut, AccountUpdate
from app.services.accounts import (
    OVERRIDABLE,
    STRATEGY_FLAGS,
    invalidate_account_cache,
    view_from_row,
)
from app.services.tradier import evict_account_client, get_tradier_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# Sent back by the UI when the user did not retype the token.  Treated as
# "leave the stored token alone" so a PATCH can never blank a credential by
# round-tripping the masked value.
_MASK_PREFIX = "••••"


def _mask(token: str | None) -> str:
    if not token:
        return ""
    tail = token[-4:] if len(token) >= 4 else token
    return f"{_MASK_PREFIX}{tail}"


def _to_out(row: Account) -> AccountOut:
    out = AccountOut.model_validate(row)
    out.api_token_masked = _mask(row.api_token)
    out.has_data_token = bool(row.data_api_token)
    return out


def _is_mask(value: str | None) -> bool:
    return bool(value) and value.startswith(_MASK_PREFIX)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@router.get("", response_model=list[AccountOut])
async def list_accounts(db: AsyncSession = Depends(get_db)) -> list[AccountOut]:
    result = await db.execute(
        select(Account).order_by(Account.sort_order, Account.id)
    )
    return [_to_out(r) for r in result.scalars().all()]


@router.get("/summary")
async def accounts_summary(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """
    Per-account dashboard strip: open positions and realized P&L today.

    One query per aggregate rather than per account — the dashboard polls this
    and the bot runs on a laptop, so keep it cheap.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )

    open_rows = await db.execute(
        select(Trade.account_id, sqlfunc.count(Trade.id))
        .where(Trade.status == TradeStatus.OPEN)
        .group_by(Trade.account_id)
    )
    open_by_acct = {aid: int(n or 0) for aid, n in open_rows.all()}

    pnl_rows = await db.execute(
        select(Trade.account_id, sqlfunc.sum(Trade.pnl))
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start)
        .group_by(Trade.account_id)
    )
    pnl_by_acct = {aid: float(v or 0) for aid, v in pnl_rows.all()}

    result = await db.execute(
        select(Account).order_by(Account.sort_order, Account.id)
    )
    summary = []
    for row in result.scalars().all():
        # Legacy rows (account_id NULL) belong to the primary account.
        extra_open = open_by_acct.get(None, 0) if row.is_primary else 0
        extra_pnl = pnl_by_acct.get(None, 0.0) if row.is_primary else 0.0
        summary.append({
            "id": row.id,
            "name": row.name,
            "enabled": bool(row.enabled),
            "use_sandbox": bool(row.use_sandbox),
            "is_primary": bool(row.is_primary),
            "open_trades": open_by_acct.get(row.id, 0) + extra_open,
            "pnl_today": round(pnl_by_acct.get(row.id, 0.0) + extra_pnl, 2),
            "strategies": [f for f in STRATEGY_FLAGS if getattr(row, f, False)],
        })
    return summary


@router.get("/{account_id}/balances")
async def account_balances(
    account_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    """Live cash / equity / buying power for one account."""
    row = await _get_or_404(db, account_id)
    client = get_tradier_client(view_from_row(row))
    try:
        bal = await client.get_account_balances()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tradier balances failed for '{row.name}': {exc}",
        )
    return {
        "account_id": row.id,
        "name": row.name,
        "account_value": bal.account_value,
        "cash": bal.cash,
        "buying_power": bal.buying_power,
        "option_buying_power": bal.option_buying_power,
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountCreate, db: AsyncSession = Depends(get_db)
) -> AccountOut:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Account name is required")

    dupe = await db.execute(select(Account).where(Account.name == name))
    if dupe.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Account '{name}' already exists")

    if not (payload.account_number or "").strip():
        raise HTTPException(status_code=400, detail="Tradier account number is required")
    if not (payload.api_token or "").strip():
        raise HTTPException(status_code=400, detail="Tradier API token is required")

    row = Account(
        name=name,
        broker="tradier",
        account_number=payload.account_number.strip(),
        api_token=payload.api_token.strip(),
        data_api_token=(payload.data_api_token or "").strip(),
        use_sandbox=payload.use_sandbox,
        enabled=payload.enabled,
        is_primary=False,
        sort_order=payload.sort_order,
        notes=payload.notes or "",
        **{f: getattr(payload, f) for f in STRATEGY_FLAGS},
        **{k: getattr(payload, k) for k in OVERRIDABLE},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    invalidate_account_cache()
    logger.warning(
        "[accounts] Account '%s' ADDED (%s, account %s) — strategies: %s",
        row.name, "SANDBOX" if row.use_sandbox else "LIVE", row.account_number,
        ", ".join(f for f in STRATEGY_FLAGS if getattr(row, f)) or "none",
    )
    return _to_out(row)


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int, payload: AccountUpdate, db: AsyncSession = Depends(get_db)
) -> AccountOut:
    row = await _get_or_404(db, account_id)
    data = payload.model_dump(exclude_unset=True)

    # Never let a masked token round-trip into storage.
    for tok in ("api_token", "data_api_token"):
        if tok in data and (_is_mask(data[tok]) or data[tok] is None):
            data.pop(tok)

    if "name" in data:
        new_name = (data["name"] or "").strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Account name cannot be empty")
        dupe = await db.execute(
            select(Account).where(Account.name == new_name, Account.id != account_id)
        )
        if dupe.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Account '{new_name}' already exists")
        data["name"] = new_name

    credentials_changed = any(
        k in data for k in ("api_token", "data_api_token", "account_number", "use_sandbox")
    )

    for key, value in data.items():
        if isinstance(value, str) and key in ("api_token", "data_api_token", "account_number"):
            value = value.strip()
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    invalidate_account_cache()

    # Drop the cached client so the next tick authenticates with the new
    # credentials instead of continuing as the old account.
    if credentials_changed:
        await evict_account_client(account_id)
        logger.warning(
            "[accounts] Credentials changed for '%s' — cached Tradier client evicted",
            row.name,
        )

    logger.info("[accounts] Account '%s' updated: %s",
                row.name, ", ".join(sorted(data)) or "no changes")
    return _to_out(row)


@router.delete("/{account_id}")
async def delete_account(
    account_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await _get_or_404(db, account_id)

    open_count = await db.execute(
        select(sqlfunc.count(Trade.id)).where(
            Trade.account_id == account_id,
            Trade.status == TradeStatus.OPEN,
        )
    )
    n_open = int(open_count.scalar() or 0)
    if n_open:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{row.name}' still holds {n_open} open trade(s). Close them first "
                f"— deleting the account would leave a live position with nothing "
                f"managing its exit. (Disable the account instead: it stops new "
                f"entries but keeps managing what's already open.)"
            ),
        )
    if row.is_primary:
        raise HTTPException(
            status_code=409,
            detail="The primary account cannot be deleted — disable it instead.",
        )

    name = row.name
    await db.delete(row)
    await db.commit()
    invalidate_account_cache()
    await evict_account_client(account_id)
    logger.warning("[accounts] Account '%s' DELETED", name)
    return {"deleted": True, "id": account_id, "name": name}


@router.post("/{account_id}/verify")
async def verify_account(
    account_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    """
    Live credential check: ask Tradier for this account's balances.

    Use it right after adding an account — a typo in the token or account
    number would otherwise only surface when the first real order is rejected.
    """
    row = await _get_or_404(db, account_id)
    client = get_tradier_client(view_from_row(row))
    try:
        bal = await client.get_account_balances()
    except Exception as exc:
        return {
            "ok": False,
            "account_id": row.id,
            "name": row.name,
            "mode": "SANDBOX" if row.use_sandbox else "LIVE",
            "error": str(exc),
        }
    return {
        "ok": True,
        "account_id": row.id,
        "name": row.name,
        "mode": "SANDBOX" if row.use_sandbox else "LIVE",
        "account_number": row.account_number,
        "account_value": bal.account_value,
        "option_buying_power": bal.option_buying_power,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_or_404(db: AsyncSession, account_id: int) -> Account:
    result = await db.execute(select(Account).where(Account.id == account_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return row
