"""
Tests for scheduler DB guard helpers:
  _get_daily_pnl()
  _get_symbol_losses_today()
  _get_symbol_trades_today()
  _get_recent_bad_exit()
  _get_recent_tp_exit()
"""
import os, pytest, pytest_asyncio
from datetime import datetime, timezone, timedelta

# Use isolated test DB
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_guard_test.db"

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Trade, TradeStatus, Direction, ExitReason
from app.services.scheduler import (
    _get_daily_pnl,
    _get_symbol_losses_today,
    _get_symbol_trades_today,
    _get_recent_bad_exit,
    _get_recent_tp_exit,
)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_guard_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _trade(symbol="AAPL", pnl=None, status=TradeStatus.CLOSED,
           exit_reason=ExitReason.TP1, minutes_ago=2,
           entry_price=5.00, stop_price=None, quantity=1) -> Trade:
    # Use a short default offset (2 min) so trades always fall within the
    # current UTC day even when tests run near midnight UTC.
    now = datetime.now(tz=timezone.utc)
    return Trade(
        symbol=symbol,
        option_symbol="AAPL240119C00150000",
        direction=Direction.CALL,
        quantity=quantity,
        remaining_qty=quantity,
        entry_price=entry_price,
        stop_price=stop_price,
        entry_time=now - timedelta(minutes=minutes_ago + 1),
        status=status,
        exit_time=now - timedelta(minutes=minutes_ago),
        exit_reason=exit_reason,
        pnl=pnl,
    )


# ── _get_daily_pnl ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_pnl_empty_db(db_session):
    pnl = await _get_daily_pnl(db_session)
    assert pnl == 0.0

@pytest.mark.asyncio
async def test_daily_pnl_sums_closed_trades(db_session):
    db_session.add(_trade(pnl=100.0))
    db_session.add(_trade(pnl=-50.0))
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    assert abs(pnl - 50.0) < 0.01

@pytest.mark.asyncio
async def test_daily_pnl_open_trade_no_stop_contributes_zero(db_session):
    """Open trade with no stop_price set contributes nothing (guard skips it)."""
    db_session.add(_trade(pnl=None, status=TradeStatus.OPEN))
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    assert pnl == 0.0


@pytest.mark.asyncio
async def test_daily_pnl_open_trade_includes_stop_floor(db_session):
    """
    Open trade WITH a stop_price contributes its worst-case unrealized loss.
    entry=5.00, stop=3.75, qty=1 → floor = (3.75 - 5.00) * 1 * 100 = -125.00
    """
    db_session.add(_trade(
        pnl=None,
        status=TradeStatus.OPEN,
        entry_price=5.00,
        stop_price=3.75,
        quantity=1,
    ))
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    assert abs(pnl - (-125.00)) < 0.01


@pytest.mark.asyncio
async def test_daily_pnl_combines_realized_and_open_floor(db_session):
    """
    Realized: -$300 closed loss + open worst-case -$125 = -$425 total.
    This is still < $500 cap so a second entry would be allowed.
    """
    db_session.add(_trade(pnl=-300.0))
    db_session.add(_trade(
        symbol="TSLA",
        pnl=None,
        status=TradeStatus.OPEN,
        entry_price=5.00,
        stop_price=3.75,
        quantity=1,
    ))
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    assert abs(pnl - (-425.00)) < 0.01


@pytest.mark.asyncio
async def test_daily_pnl_concurrent_loophole_blocked(db_session):
    """
    Regression: two trades opened simultaneously when realized P&L was -$473.
    Each stop floor = (3.75 - 5.00) * 2 * 100 = -$250.
    First trade: realized -$473 + open floor -$250 = -$723 → cap check fires.
    This test verifies that once the first open trade is present, the reported
    daily P&L already breaches -$500, so no second trade would be admitted.
    """
    # Simulate: $473 realized loss already on the books
    db_session.add(_trade(pnl=-473.0))
    # First trade just opened (qty=2, entry=5.00, stop=3.75 → floor=-$250)
    db_session.add(_trade(
        symbol="QQQ",
        pnl=None,
        status=TradeStatus.OPEN,
        entry_price=5.00,
        stop_price=3.75,
        quantity=2,
    ))
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    # -473 realized + -250 floor = -723, well below -500 cap
    assert pnl < -500.0

@pytest.mark.asyncio
async def test_daily_pnl_ignores_yesterday(db_session):
    t = _trade(pnl=500.0)
    # Override exit_time to yesterday
    t.exit_time = datetime.now(tz=timezone.utc) - timedelta(days=1, hours=1)
    db_session.add(t)
    await db_session.commit()
    pnl = await _get_daily_pnl(db_session)
    assert pnl == 0.0


# ── _get_symbol_losses_today ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_symbol_losses_none(db_session):
    count = await _get_symbol_losses_today(db_session, "AAPL")
    assert count == 0

@pytest.mark.asyncio
async def test_symbol_losses_counts_negative_pnl(db_session):
    db_session.add(_trade("AAPL", pnl=-100.0))
    db_session.add(_trade("AAPL", pnl=-50.0))
    db_session.add(_trade("AAPL", pnl=200.0))   # winning — not counted
    await db_session.commit()
    count = await _get_symbol_losses_today(db_session, "AAPL")
    assert count == 2

@pytest.mark.asyncio
async def test_symbol_losses_different_symbols_not_mixed(db_session):
    db_session.add(_trade("AAPL", pnl=-100.0))
    db_session.add(_trade("TSLA", pnl=-100.0))
    await db_session.commit()
    assert await _get_symbol_losses_today(db_session, "AAPL") == 1
    assert await _get_symbol_losses_today(db_session, "TSLA") == 1
    assert await _get_symbol_losses_today(db_session, "NVDA") == 0


# ── _get_symbol_trades_today ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_symbol_trades_today_none(db_session):
    """No trades on symbol → count is 0."""
    count = await _get_symbol_trades_today(db_session, "AAPL")
    assert count == 0


@pytest.mark.asyncio
async def test_symbol_trades_today_counts_wins_and_losses(db_session):
    """Counts all trades regardless of PnL — wins and losses both count."""
    db_session.add(_trade("AAPL", pnl=100.0))   # win
    db_session.add(_trade("AAPL", pnl=-50.0))   # loss
    db_session.add(_trade("AAPL", pnl=200.0))   # another win
    await db_session.commit()
    count = await _get_symbol_trades_today(db_session, "AAPL")
    assert count == 3


@pytest.mark.asyncio
async def test_symbol_trades_today_counts_open_trades(db_session):
    """Open (not yet closed) trades also count toward the daily cap."""
    open_trade = _trade("AAPL", pnl=None)
    open_trade.status = TradeStatus.OPEN
    open_trade.exit_time = None
    open_trade.exit_reason = None
    db_session.add(open_trade)
    await db_session.commit()
    count = await _get_symbol_trades_today(db_session, "AAPL")
    assert count == 1


@pytest.mark.asyncio
async def test_symbol_trades_today_different_symbols_not_mixed(db_session):
    """Trade counts are per-symbol and don't bleed across symbols."""
    db_session.add(_trade("AAPL", pnl=100.0))
    db_session.add(_trade("AAPL", pnl=-50.0))
    db_session.add(_trade("TSLA", pnl=200.0))
    await db_session.commit()
    assert await _get_symbol_trades_today(db_session, "AAPL") == 2
    assert await _get_symbol_trades_today(db_session, "TSLA") == 1
    assert await _get_symbol_trades_today(db_session, "NVDA") == 0


# ── _get_recent_bad_exit ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_recent_bad_exit(db_session):
    result = await _get_recent_bad_exit(db_session, "AAPL")
    assert result is None

@pytest.mark.asyncio
async def test_recent_stop_triggers_cooldown(db_session):
    db_session.add(_trade("AAPL", pnl=-100.0, exit_reason=ExitReason.STOP, minutes_ago=30))
    await db_session.commit()
    result = await _get_recent_bad_exit(db_session, "AAPL")
    assert result is not None
    assert result.exit_reason == ExitReason.STOP

@pytest.mark.asyncio
async def test_recent_vwap_break_triggers_cooldown(db_session):
    db_session.add(_trade("AAPL", pnl=-80.0, exit_reason=ExitReason.VWAP_BREAK, minutes_ago=15))
    await db_session.commit()
    result = await _get_recent_bad_exit(db_session, "AAPL")
    assert result is not None

@pytest.mark.asyncio
async def test_tp1_exit_does_not_trigger_cooldown(db_session):
    db_session.add(_trade("AAPL", pnl=200.0, exit_reason=ExitReason.TP1, minutes_ago=5))
    await db_session.commit()
    result = await _get_recent_bad_exit(db_session, "AAPL")
    assert result is None

@pytest.mark.asyncio
async def test_old_stop_outside_cooldown_window(db_session):
    t = _trade("AAPL", pnl=-100.0, exit_reason=ExitReason.STOP, minutes_ago=5)
    # Push exit time past cooldown window (default 60 min)
    t.exit_time = datetime.now(tz=timezone.utc) - timedelta(minutes=90)
    db_session.add(t)
    await db_session.commit()
    result = await _get_recent_bad_exit(db_session, "AAPL")
    assert result is None

@pytest.mark.asyncio
async def test_cooldown_isolated_per_symbol(db_session):
    db_session.add(_trade("AAPL", pnl=-100.0, exit_reason=ExitReason.STOP, minutes_ago=5))
    await db_session.commit()
    # TSLA should not be affected by AAPL's cooldown
    result = await _get_recent_bad_exit(db_session, "TSLA")
    assert result is None


# ── _get_recent_tp_exit ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recent_tp2_triggers_tp_cooldown(db_session):
    """TP2 exit within tp_cooldown_minutes → cooldown active."""
    from app.config import settings
    from unittest.mock import patch
    db_session.add(_trade("NVDA", pnl=222.0, exit_reason=ExitReason.TP2, minutes_ago=4))
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 30):
        result = await _get_recent_tp_exit(db_session, "NVDA")
    assert result is not None
    assert result.exit_reason == ExitReason.TP2


@pytest.mark.asyncio
async def test_recent_tp1_triggers_tp_cooldown(db_session):
    """TP1 exit within tp_cooldown_minutes → cooldown active."""
    from app.config import settings
    from unittest.mock import patch
    db_session.add(_trade("NVDA", pnl=80.0, exit_reason=ExitReason.TP1, minutes_ago=10))
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 30):
        result = await _get_recent_tp_exit(db_session, "NVDA")
    assert result is not None
    assert result.exit_reason == ExitReason.TP1


@pytest.mark.asyncio
async def test_stop_exit_does_not_trigger_tp_cooldown(db_session):
    """STOP exit must not appear in _get_recent_tp_exit — different cooldown."""
    from app.config import settings
    from unittest.mock import patch
    db_session.add(_trade("NVDA", pnl=-88.0, exit_reason=ExitReason.STOP, minutes_ago=5))
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 30):
        result = await _get_recent_tp_exit(db_session, "NVDA")
    assert result is None


@pytest.mark.asyncio
async def test_tp_exit_outside_window_no_cooldown(db_session):
    """TP2 exit older than tp_cooldown_minutes → no cooldown."""
    from app.config import settings
    from unittest.mock import patch
    t = _trade("NVDA", pnl=200.0, exit_reason=ExitReason.TP2, minutes_ago=5)
    # Push it past the 30-min window
    t.exit_time = datetime.now(tz=timezone.utc) - timedelta(minutes=45)
    db_session.add(t)
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 30):
        result = await _get_recent_tp_exit(db_session, "NVDA")
    assert result is None


@pytest.mark.asyncio
async def test_tp_cooldown_disabled_when_zero(db_session):
    """tp_cooldown_minutes=0 disables the gate entirely."""
    from app.config import settings
    from unittest.mock import patch
    db_session.add(_trade("NVDA", pnl=200.0, exit_reason=ExitReason.TP2, minutes_ago=2))
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 0):
        result = await _get_recent_tp_exit(db_session, "NVDA")
    assert result is None


@pytest.mark.asyncio
async def test_tp_cooldown_isolated_per_symbol(db_session):
    """TP cooldown on NVDA must not affect AAPL entries."""
    from app.config import settings
    from unittest.mock import patch
    db_session.add(_trade("NVDA", pnl=200.0, exit_reason=ExitReason.TP2, minutes_ago=4))
    await db_session.commit()
    with patch.object(settings, "tp_cooldown_minutes", 30):
        result = await _get_recent_tp_exit(db_session, "AAPL")
    assert result is None
