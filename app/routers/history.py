from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Trade, TradeStatus
from app.schemas import TradeOut

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/today", response_model=list[TradeOut])
async def closed_today(db: AsyncSession = Depends(get_db)):
    """Trades closed today (UTC midnight boundary)."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(
        select(Trade)
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start)
        .order_by(Trade.exit_time.desc())
    )
    return result.scalars().all()


@router.get("/last30", response_model=list[TradeOut])
async def last_30_days(db: AsyncSession = Depends(get_db)):
    """All closed trades from the last 30 days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    result = await db.execute(
        select(Trade)
        .where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= cutoff)
        .order_by(Trade.exit_time.desc())
    )
    return result.scalars().all()


@router.get("/summary/today")
async def today_summary(db: AsyncSession = Depends(get_db)):
    """Aggregate stats for today's closed trades."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(
        select(
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.pnl).label("total_pnl"),
            func.sum(case((Trade.pnl > 0, 1), else_=0)).label("winners"),
            func.sum(case((Trade.pnl <= 0, 1), else_=0)).label("losers"),
        ).where(Trade.status == TradeStatus.CLOSED, Trade.exit_time >= today_start)
    )
    row = result.one()
    return {
        "trade_count": row.trade_count or 0,
        "total_pnl": round(row.total_pnl or 0, 2),
        "winners": row.winners or 0,
        "losers": row.losers or 0,
    }
