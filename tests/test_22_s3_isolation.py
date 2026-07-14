"""S3 ↔ S1/S2 isolation — the seams where the stock strategy meets the
options infrastructure.

Covers the four fixes made when S3 trades joined the shared `trades` table:
  1. manage_open_trades must SKIP S3 rows (options logic never touches them)
  2. live-P&L enrichment must use ×1 for S3 stocks, not the ×100 contract mult
  3. the manual Close endpoint must refuse S3 trades with a clear error
  4. startup orphan-closer must ignore S3 rows
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+aiosqlite:////sessions/tmp/ajoy_s3_isolation_test.db"
    if os.path.isdir("/sessions/tmp") else "sqlite+aiosqlite:///./test_s3_isolation.db",
)

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Direction, Trade, TradeStatus
from app.services.tradier import Quote


def _s3_trade(**kw) -> Trade:
    defaults = dict(
        symbol="SOFI", option_symbol="SOFI", direction=Direction.CALL,
        strategy_name="S3", quantity=100, remaining_qty=100,
        entry_price=12.00, entry_time=datetime.now(timezone.utc),
        stop_price=11.90, status=TradeStatus.OPEN,
        created_at=datetime.now(timezone.utc),
        tp1_hit=False, be_stop_set=False, runner_mode=False,
    )
    defaults.update(kw)
    return Trade(**defaults)


@pytest.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/iso.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


class _TripwireClient:
    """Any options-path call on an S3 trade trips the wire."""

    def __init__(self):
        self.calls: list[str] = []

    async def get_positions(self):          # allowed (orphan sweep)
        self.calls.append("get_positions")
        return []

    def __getattr__(self, name):
        raise AssertionError(
            f"options client method '{name}' must not be called for S3 trades"
        )


class TestManageLoopSkipsS3:
    async def test_s3_trade_untouched_by_manage_loop(self, db_session, monkeypatch):
        session, factory = db_session
        trade = _s3_trade()
        session.add(trade)
        await session.commit()

        from app.services import scheduler as sched
        client = _TripwireClient()
        monkeypatch.setattr(sched, "is_market_open", lambda *a, **k: True)
        monkeypatch.setattr(sched, "is_past_cutoff", lambda *a, **k: True)  # worst case
        monkeypatch.setattr(sched, "get_tradier_client", lambda: client)
        monkeypatch.setattr(sched, "AsyncSessionLocal", factory)

        await sched.manage_open_trades()

        await session.refresh(trade)
        assert trade.status == TradeStatus.OPEN, "S3 trade must survive the cutoff sweep"
        assert client.calls in ([], ["get_positions"]), (
            f"only the orphan position sweep may touch the client, got {client.calls}"
        )

    async def test_orphan_closer_ignores_s3(self, db_session, monkeypatch):
        session, factory = db_session
        trade = _s3_trade()
        session.add(trade)
        await session.commit()

        from app.services import scheduler as sched
        client = _TripwireClient()
        monkeypatch.setattr(sched, "get_tradier_client", lambda: client)
        monkeypatch.setattr(sched, "AsyncSessionLocal", factory)

        await sched.close_orphaned_open_trades()

        await session.refresh(trade)
        assert trade.status == TradeStatus.OPEN
        assert client.calls == [], "orphan closer must not touch S3 rows at all"


class TestS3PnLDisplay:
    async def test_stock_pnl_not_multiplied_by_100(self, monkeypatch):
        trade = _s3_trade(id=1)

        class _QuoteClient:
            async def get_option_quote(self, sym):
                return Quote(symbol=sym, last=12.50, bid=12.49, ask=12.51, volume=1000)

            def __getattr__(self, name):
                async def _fail(*a, **k):
                    raise RuntimeError("section isolated")
                return _fail

        import app.routers.trades as tr
        monkeypatch.setattr(tr, "get_tradier_client", lambda: _QuoteClient())
        enriched = await tr._enrich_with_live_pnl(trade)
        # 100 shares × $0.50/share = $50 — NOT $5,000 (options ×100 mult)
        assert enriched.live_pnl == pytest.approx(50.0), enriched.live_pnl
        assert enriched.live_pnl_pct == pytest.approx(50.0 / 1200.0 * 100, abs=0.05)


class TestManualCloseGuard:
    async def test_manual_close_refuses_s3(self, db_session):
        session, _ = db_session
        trade = _s3_trade()
        session.add(trade)
        await session.commit()

        from app.routers.trades import manual_close_trade
        from app.schemas import CloseTradeRequest
        with pytest.raises(HTTPException) as exc:
            await manual_close_trade(CloseTradeRequest(trade_id=trade.id), session)
        assert exc.value.status_code == 400
        assert "S3" in exc.value.detail
