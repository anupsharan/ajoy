from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Symbol
from app.schemas import SymbolCreate, SymbolOut, SymbolUpdate

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("", response_model=list[SymbolOut])
async def list_symbols(
    strategy: str = Query(default="S1"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Symbol).where(Symbol.strategy == strategy).order_by(Symbol.ticker)
    )
    return result.scalars().all()


@router.post("", response_model=SymbolOut, status_code=201)
async def create_symbol(payload: SymbolCreate, db: AsyncSession = Depends(get_db)):
    strategy = payload.strategy or "S1"
    existing = await db.execute(
        select(Symbol).where(
            Symbol.ticker == payload.ticker.upper(),
            Symbol.strategy == strategy,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Ticker already exists in this strategy")
    symbol = Symbol(ticker=payload.ticker.upper(), active=payload.active, strategy=strategy)
    db.add(symbol)
    await db.commit()
    await db.refresh(symbol)
    return symbol


@router.patch("/{symbol_id}", response_model=SymbolOut)
async def update_symbol(
    symbol_id: int, payload: SymbolUpdate, db: AsyncSession = Depends(get_db)
):
    sym = await db.get(Symbol, symbol_id)
    if not sym:
        raise HTTPException(status_code=404, detail="Symbol not found")
    if payload.active is not None:
        sym.active = payload.active
    await db.commit()
    await db.refresh(sym)
    return sym


@router.delete("/{symbol_id}", status_code=204)
async def delete_symbol(symbol_id: int, db: AsyncSession = Depends(get_db)):
    sym = await db.get(Symbol, symbol_id)
    if not sym:
        raise HTTPException(status_code=404, detail="Symbol not found")
    await db.delete(sym)
    await db.commit()
