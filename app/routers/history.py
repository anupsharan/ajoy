from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Trade, TradeStatus
from app.schemas import TradeOut

router = APIRouter(prefix="/api/history", tags=["history"])


def _account_filter(stmt, account_id: Optional[int]):
    """
    Optionally restrict a history query to one account (multi-account,
    Jul 25 2026).

    `account_id=None` means "all accounts" — the combined book, which is what
    every existing caller (and the dashboard's default view) expects.  The UI
    passes an id when the user picks a single account from the filter.
    """
    if account_id is None:
        return stmt
    return stmt.where(Trade.account_id == account_id)


@router.get("/today", response_model=list[TradeOut])
async def closed_today(
    db: AsyncSession = Depends(get_db), account_id: Optional[int] = None
):
    """Trades closed today (UTC midnight boundary)."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_account_filter(
        select(Trade)
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start)
        .order_by(Trade.exit_time.desc()), account_id)
    )
    return result.scalars().all()


@router.get("/last30", response_model=list[TradeOut])
async def last_30_days(
    db: AsyncSession = Depends(get_db), account_id: Optional[int] = None
):
    """All closed trades from the last 30 days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    result = await db.execute(_account_filter(
        select(Trade)
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= cutoff)
        .order_by(Trade.exit_time.desc()), account_id)
    )
    return result.scalars().all()


@router.get("/summary/today")
async def today_summary(
    db: AsyncSession = Depends(get_db), account_id: Optional[int] = None
):
    """Aggregate stats for today's closed trades."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_account_filter(
        select(
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.pnl).label("total_pnl"),
            func.sum(case((Trade.pnl > 0, 1), else_=0)).label("winners"),
            func.sum(case((Trade.pnl <= 0, 1), else_=0)).label("losers"),
        ).where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start),
        account_id)
    )
    row = result.one()
    return {
        "trade_count": row.trade_count or 0,
        "total_pnl": round(row.total_pnl or 0, 2),
        "winners": row.winners or 0,
        "losers": row.losers or 0,
    }


@router.get("/summary/by-account")
async def summary_by_account(db: AsyncSession = Depends(get_db)):
    """
    Today's closed-trade stats broken out per account.

    Backs the dashboard's account strip: the whole point of running several
    accounts is being able to compare them, and a combined P&L number hides
    which account is actually working.
    """
    from app.models import Account

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(
        select(
            Trade.account_id,
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.pnl).label("total_pnl"),
            func.sum(case((Trade.pnl > 0, 1), else_=0)).label("winners"),
            func.sum(case((Trade.pnl <= 0, 1), else_=0)).label("losers"),
        )
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start)
        .group_by(Trade.account_id)
    )
    rows = result.all()

    names = await db.execute(select(Account.id, Account.name))
    name_by_id = {i: n for i, n in names.all()}

    return [
        {
            "account_id": r.account_id,
            "account_name": name_by_id.get(r.account_id, "Unassigned"),
            "trade_count": r.trade_count or 0,
            "total_pnl": round(r.total_pnl or 0, 2),
            "winners": r.winners or 0,
            "losers": r.losers or 0,
        }
        for r in rows
    ]
