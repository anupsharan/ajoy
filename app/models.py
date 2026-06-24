"""SQLAlchemy ORM models for a-joy."""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Enum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class LogicType(str, PyEnum):
    AND = "AND"
    OR = "OR"


class Direction(str, PyEnum):
    CALL = "CALL"
    PUT = "PUT"


class TradeStatus(str, PyEnum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, PyEnum):
    TP1 = "TP1"
    TP2 = "TP2"
    STOP = "STOP"
    TRAILING_STOP = "TRAILING_STOP"  # stop was raised above the original level before firing (profit lock)
    QUICK_LOSS = "QUICK_LOSS"       # option lost too much too fast — wrong-direction entry
    VWAP_BREAK = "VWAP_BREAK"
    TREND_REVERSAL = "TREND_REVERSAL"
    EMA_CROSS = "EMA_CROSS"         # S2: opposite EMA crossover on 1-min → signal-based exit
    CUTOFF = "CUTOFF"
    MANUAL = "MANUAL"


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(20), default="S1", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

class Indicator(Base):
    __tablename__ = "indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)   # display label
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="general")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    group_members: Mapped[list[IndicatorGroupMember]] = relationship(
        back_populates="indicator", cascade="all, delete-orphan"
    )


class IndicatorGroup(Base):
    __tablename__ = "indicator_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    logic_type: Mapped[LogicType] = mapped_column(
        Enum(LogicType), default=LogicType.AND, nullable=False
    )

    members: Mapped[list[IndicatorGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    strategies: Mapped[list[Strategy]] = relationship(back_populates="indicator_group")


class IndicatorGroupMember(Base):
    __tablename__ = "indicator_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("indicator_groups.id"), nullable=False)
    indicator_id: Mapped[int] = mapped_column(ForeignKey("indicators.id"), nullable=False)

    group: Mapped[IndicatorGroup] = relationship(back_populates="members")
    indicator: Mapped[Indicator] = relationship(back_populates="group_members")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    indicator_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("indicator_groups.id"), nullable=True
    )

    indicator_group: Mapped[IndicatorGroup | None] = relationship(back_populates="strategies")


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Identifiers
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    option_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[Direction] = mapped_column(Enum(Direction), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), default="vwap_pullback")

    # Order / execution details
    tradier_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stop_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)  # broker-side resting stop order
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Levels (option premium prices)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Underlying price at entry (for VWAP / EMA reference)
    underlying_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    vwap_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)

    # State flags
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus), default=TradeStatus.OPEN)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    be_stop_set: Mapped[bool] = mapped_column(Boolean, default=False)
    remaining_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Exit
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[ExitReason | None] = mapped_column(Enum(ExitReason), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
