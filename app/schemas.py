"""Pydantic schemas for request/response validation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_serializer


def _as_utc_iso(dt: datetime | None) -> str | None:
    """
    Serialize a datetime to ISO-8601 with explicit UTC offset (+00:00).

    SQLite returns naive datetimes (no tzinfo), even though we store UTC.
    Without the offset, JavaScript's Date() treats the string as *local* time,
    which shifts every timestamp by the browser's UTC offset.
    Adding '+00:00' forces correct UTC→ET conversion in the front-end.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

from app.models import Direction, ExitReason, LogicType, TradeStatus


# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------

class SymbolBase(BaseModel):
    ticker: str
    active: bool = True
    strategy: str = "S1"


class SymbolCreate(SymbolBase):
    pass


class SymbolUpdate(BaseModel):
    active: Optional[bool] = None


class SymbolOut(SymbolBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime

    @field_serializer('created_at', when_used='json')
    def _ser_dt(self, v: datetime | None) -> str | None:
        return _as_utc_iso(v)


# ---------------------------------------------------------------------------
# Indicator
# ---------------------------------------------------------------------------

class IndicatorBase(BaseModel):
    key: str
    name: str           # display label
    description: str = ""
    category: str = "general"
    active: bool = True


class IndicatorCreate(IndicatorBase):
    pass


class IndicatorUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None


class IndicatorOut(IndicatorBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ---------------------------------------------------------------------------
# Indicator Group
# ---------------------------------------------------------------------------

class IndicatorGroupBase(BaseModel):
    name: str
    logic_type: LogicType = LogicType.AND


class IndicatorGroupCreate(IndicatorGroupBase):
    indicator_ids: list[int] = []


class IndicatorGroupOut(IndicatorGroupBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    indicator_ids: list[int] = []

    @classmethod
    def from_orm_with_members(cls, group):
        return cls(
            id=group.id,
            name=group.name,
            logic_type=group.logic_type,
            indicator_ids=[m.indicator_id for m in group.members],
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class StrategyBase(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    indicator_group_id: Optional[int] = None


class StrategyCreate(StrategyBase):
    pass


class StrategyUpdate(BaseModel):
    enabled: Optional[bool] = None
    indicator_group_id: Optional[int] = None


class StrategyOut(StrategyBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class TradeCreate(BaseModel):
    symbol: str
    option_symbol: str
    direction: Direction
    strategy_name: str = "vwap_pullback"
    tradier_order_id: Optional[str] = None
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_price: Optional[float] = None
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
    underlying_entry: Optional[float] = None
    vwap_at_entry: Optional[float] = None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    option_symbol: str
    direction: Direction
    strategy_name: str
    tradier_order_id: Optional[str]
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_price: Optional[float]
    tp1_price: Optional[float]
    tp2_price: Optional[float]
    underlying_entry: Optional[float]
    vwap_at_entry: Optional[float]
    status: TradeStatus
    tp1_hit: bool
    be_stop_set: bool
    remaining_qty: Optional[int]
    exit_price: Optional[float]
    exit_time: Optional[datetime]
    exit_reason: Optional[ExitReason]
    pnl: Optional[float]
    created_at: datetime

    @field_serializer('entry_time', 'exit_time', 'created_at', when_used='json')
    def _ser_dt(self, v: datetime | None) -> str | None:
        return _as_utc_iso(v)


class TradeWithLivePnL(TradeOut):
    """Trade enriched with real-time option price from Tradier."""
    current_price: Optional[float] = None
    live_pnl: Optional[float] = None
    live_pnl_pct: Optional[float] = None
    underlying_price: Optional[float] = None
    trend: Optional[str] = None           # "bullish" | "bearish" | "neutral"
    vwap_current: Optional[float] = None  # live intraday VWAP of underlying
    # "intact"  — stock firmly on correct side of VWAP  → hold, let bot manage
    # "at_risk" — stock within 0.2% of VWAP             → watch closely
    # "broken"  — stock crossed to wrong VWAP side       → consider closing
    thesis_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Manual close request
# ---------------------------------------------------------------------------

class CloseTradeRequest(BaseModel):
    trade_id: int
