"""
Tests for scheduler._close_trade() — the most critical money path.

Every scenario that touches real money is covered:
  • API exception    → returns False, DB trade stays OPEN
  • Order rejected   → returns False, DB trade stays OPEN
  • Order canceled / cancelled → same as rejected
  • Exit-price priority chain:
      1. Actual Tradier fill price (most accurate — reflects bid-side execution)
      2. Quote mid-price (when no fill available)
      3. Caller-supplied trigger price (when no fill, no quote)
      4. Entry price fallback (worst-case, pnl = 0)
  • Fill price always overrides caller-supplied quote (fill reconciliation)
  • P&L arithmetic:  stop → negative, TP → positive, fallback → zero
  • Accumulated pnl from a prior partial close
  • remaining_qty=None falls back to quantity
  • exit_reason persisted correctly
  • exit_time is populated on close
"""
import os, pytest, pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/ajoy_close_trade_test.db"

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database import Base
from app.models import Trade, TradeStatus, Direction, ExitReason
from app.services.scheduler import _close_trade
from app.services.tradier import OrderResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:////tmp/ajoy_close_trade_test.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _open_trade(entry_price=5.00, qty=2, remaining_qty=2, prior_pnl=None) -> Trade:
    return Trade(
        symbol="AAPL",
        option_symbol="AAPL240119C00150000",
        direction=Direction.CALL,
        quantity=qty,
        remaining_qty=remaining_qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        status=TradeStatus.OPEN,
        pnl=prior_pnl,
    )


def _client(
    place_raises=False,
    fill_price=None,
    order_status="filled",
    quote_bid=None,
    quote_ask=None,
):
    """Build a mock TradierClient for _close_trade tests."""
    c = MagicMock()
    if place_raises:
        c.place_option_order = AsyncMock(side_effect=Exception("Tradier API error"))
    else:
        c.place_option_order = AsyncMock(
            return_value=OrderResult(order_id="sell001", status="ok")
        )
    c.get_fill_price = AsyncMock(return_value=fill_price)
    c.get_order_status = AsyncMock(return_value={"status": order_status})

    if quote_bid is not None and quote_ask is not None:
        q = MagicMock()
        q.bid  = quote_bid
        q.ask  = quote_ask
        q.last = round((quote_bid + quote_ask) / 2, 2)
        c.get_option_quote = AsyncMock(return_value=q)
    else:
        c.get_option_quote = AsyncMock(return_value=None)

    return c


# ===========================================================================
# API failure — sell order raises
# ===========================================================================

@pytest.mark.asyncio
async def test_api_exception_returns_false_and_leaves_open(db):
    """If place_option_order raises, trade must remain OPEN."""
    trade = _open_trade()
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(db, _client(place_raises=True), trade, ExitReason.STOP)

    assert result is False
    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN
    assert trade.exit_price is None
    assert trade.pnl is None


# ===========================================================================
# Order rejection / cancellation
# ===========================================================================

@pytest.mark.asyncio
async def test_rejected_returns_false_and_leaves_open(db):
    trade = _open_trade()
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(db, _client(order_status="rejected"), trade, ExitReason.STOP)

    assert result is False
    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN
    assert trade.exit_price is None


@pytest.mark.asyncio
async def test_canceled_one_l_leaves_open(db):
    """'canceled' (one l) must trigger the rejection guard."""
    trade = _open_trade()
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(db, _client(order_status="canceled"), trade, ExitReason.STOP)
    assert result is False
    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_cancelled_two_l_leaves_open(db):
    """'cancelled' (two l) must also trigger the rejection guard."""
    trade = _open_trade()
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(db, _client(order_status="cancelled"), trade, ExitReason.STOP)
    assert result is False
    await db.refresh(trade)
    assert trade.status == TradeStatus.OPEN


# ===========================================================================
# Exit-price priority chain
# ===========================================================================

@pytest.mark.asyncio
async def test_priority1_fill_overrides_caller_price(db):
    """
    Fill reconciliation: actual Tradier fill price ALWAYS wins, even when
    the caller supplies an exit_price (mid-quote at trigger time).
    Example: mid-quote = $6.75 but market order executed at bid = $6.40.
    The bot should record $6.40, not $6.75, to match Tradier gain/loss.
    """
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=6.40, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.75
    )

    assert result is True
    await db.refresh(trade)
    # Fill price wins — not the caller-supplied mid-quote
    assert trade.exit_price == 6.40
    assert trade.status == TradeStatus.CLOSED
    # P&L should reflect actual fill: (6.40 - 5.00) * 2 * 100 = +280
    assert trade.pnl == pytest.approx(280.0, abs=0.01)


@pytest.mark.asyncio
async def test_priority1_fill_wins_stop_loss(db):
    """
    At a stop loss, market order executes at the bid (below mid-quote).
    Fill = $3.60 (bid), trigger quote = $3.75 (mid). Fill wins.
    pnl = (3.60 - 5.00) * 2 * 100 = -280  (more negative than with mid-quote)
    """
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=3.60, order_status="filled"),
        trade, ExitReason.STOP, exit_price=3.75
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 3.60
    assert trade.pnl == pytest.approx(-280.0, abs=0.01)


@pytest.mark.asyncio
async def test_priority2_fill_used_when_no_caller_price(db):
    """No caller price → actual fill is used."""
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=4.20, order_status="filled"),
        trade, ExitReason.CUTOFF
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 4.20


@pytest.mark.asyncio
async def test_priority3_quote_mid_used_when_no_fill_no_caller(db):
    """No caller price, no fill → mid of bid/ask from fresh quote."""
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=None, order_status="filled", quote_bid=4.80, quote_ask=5.20),
        trade, ExitReason.CUTOFF
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == pytest.approx(5.00, abs=0.01)   # (4.80 + 5.20) / 2


@pytest.mark.asyncio
async def test_priority3_caller_price_used_when_no_fill_no_quote(db):
    """No fill available, no quote → caller-supplied trigger price is used."""
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.STOP, exit_price=3.75
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 3.75


@pytest.mark.asyncio
async def test_priority4_entry_fallback_when_all_sources_fail(db):
    """No fill, no quote, no caller price → exit_price = entry_price → pnl = 0."""
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.CUTOFF
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 5.00
    assert trade.pnl == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_fill_close_to_caller_no_discrepancy(db):
    """
    Fill and caller price agree (diff ≤ $0.01) — no log noise, fill still used.
    entry=5.00, fill=6.75, caller=6.75 → same value either way.
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=6.75, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.75
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 6.75
    assert trade.pnl == pytest.approx(175.0, abs=0.01)  # (6.75-5.00)*1*100


@pytest.mark.asyncio
async def test_fill_sanity_check_rejects_fill_above_trigger(db):
    """
    Sandbox bug: get_fill_price returns $2.86 when trigger was $2.79 (2.5% above).
    A market sell cannot fill above the bid; upper bound is +2%.
    Real-world example: GOOGL CALL 2026-06-04 — bot recorded +$114, Tradier showed -$78.

    fill=$6.17, trigger=$6.00 → 2.8% above → rejected, trigger used.
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=6.17, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 6.00   # fill (above trigger) discarded
    assert trade.pnl == pytest.approx(100.0, abs=0.01)


@pytest.mark.asyncio
async def test_fill_sanity_check_accepts_fill_just_inside_upper_bound(db):
    """Fill 1.5% above trigger — within the +2% upper bound → fill used."""
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=6.09, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00   # 6.09 is 1.5% above 6.00
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 6.09   # fill accepted (1.5% < 2% upper bound)


@pytest.mark.asyncio
async def test_fill_sanity_check_rejects_stale_sandbox_price(db):
    """
    Sandbox bug: TP2 fires at $1.60 but get_fill_price returns $1.32 (17.5% below).
    Threshold is 12% — 17.5% exceeds it, fill discarded, trigger quote used.

    Real-world example: IWM CALL 2026-06-04, tp2=$1.60, sandbox fill=$1.32.
    Without this guard: P&L recorded as +$2 instead of the correct +$58.

    entry=5.00, trigger=6.00, fill=4.44 (26% below trigger) — well over 12%.
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    # 4.44 is 26% below 6.00 — exceeds 12% lower threshold, fill discarded
    result = await _close_trade(
        db, _client(fill_price=4.44, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 6.00
    assert trade.pnl == pytest.approx(100.0, abs=0.01)


@pytest.mark.asyncio
async def test_fill_sanity_check_rejects_17pct_deviation(db):
    """
    Mirrors the actual IWM Jun-04 case: trigger=1.60, fill=1.32 (17.5% deviation).
    Must be rejected under the tightened 12% threshold.
    """
    trade = _open_trade(entry_price=1.31, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=1.32, order_status="filled"),
        trade, ExitReason.TP2, exit_price=1.60
    )

    assert result is True
    await db.refresh(trade)
    # Fill (17.5% deviation) rejected — trigger price used
    assert trade.exit_price == 1.60
    assert trade.pnl == pytest.approx(58.0, abs=0.01)  # (1.60-1.31)*2*100


@pytest.mark.asyncio
async def test_fill_sanity_check_accepts_normal_slippage(db):
    """
    Normal slippage: TP2 at $6.00 (mid), actual fill at $5.75 (bid, 4.2% below).
    Well within 12% sanity threshold → fill wins over trigger quote.
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=5.75, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )

    assert result is True
    await db.refresh(trade)
    # Fill within sanity → fill price used
    assert trade.exit_price == 5.75
    assert trade.pnl == pytest.approx(75.0, abs=0.01)  # (5.75-5.00)*1*100


@pytest.mark.asyncio
async def test_fill_sanity_check_accepts_11pct_deviation(db):
    """
    Fill at 11% below trigger — just inside the 12% threshold → fill used.
    Confirms the boundary: 11% in, 13% out.
    trigger=6.00, fill=5.34 (11% below).
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=5.34, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 5.34   # fill used (11% < 12% threshold)


@pytest.mark.asyncio
async def test_fill_sanity_check_rejects_13pct_deviation(db):
    """
    Fill at 13% below trigger — just outside the 12% threshold → trigger used.
    trigger=6.00, fill=5.22 (13% below).
    """
    trade = _open_trade(entry_price=5.00, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    result = await _close_trade(
        db, _client(fill_price=5.22, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )

    assert result is True
    await db.refresh(trade)
    assert trade.exit_price == 6.00   # trigger used (13% > 12% threshold)


# ===========================================================================
# P&L arithmetic
# ===========================================================================

@pytest.mark.asyncio
async def test_stop_loss_pnl_is_negative(db):
    """
    entry=5.00, exit=3.75, qty=2
    pnl = (3.75 - 5.00) * 2 * 100 = -250.0
    """
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.STOP, exit_price=3.75
    )
    await db.refresh(trade)
    assert trade.pnl == pytest.approx(-250.0, abs=0.01)


@pytest.mark.asyncio
async def test_tp_pnl_is_positive(db):
    """
    entry=5.00, exit=6.75, qty=2
    pnl = (6.75 - 5.00) * 2 * 100 = +350.0
    """
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.75
    )
    await db.refresh(trade)
    assert trade.pnl == pytest.approx(350.0, abs=0.01)


@pytest.mark.asyncio
async def test_pnl_accumulates_prior_partial(db):
    """
    Prior partial pnl = +50.0 (e.g. from a TP1).
    Close 2 contracts at 4.00, entry 5.00:
      close_pnl = (4.00 - 5.00) * 2 * 100 = -200
      total pnl = 50 + (-200) = -150
    """
    trade = _open_trade(entry_price=5.00, qty=2, remaining_qty=2, prior_pnl=50.0)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.STOP, exit_price=4.00
    )
    await db.refresh(trade)
    assert trade.pnl == pytest.approx(-150.0, abs=0.01)


@pytest.mark.asyncio
async def test_single_contract_pnl(db):
    """
    entry=2.50, exit=3.50, qty=1
    pnl = (3.50 - 2.50) * 1 * 100 = +100.0
    """
    trade = _open_trade(entry_price=2.50, qty=1, remaining_qty=1)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.TP2, exit_price=3.50
    )
    await db.refresh(trade)
    assert trade.pnl == pytest.approx(100.0, abs=0.01)


# ===========================================================================
# Quantity / state edge cases
# ===========================================================================

@pytest.mark.asyncio
async def test_remaining_qty_none_falls_back_to_quantity(db):
    """When remaining_qty is None, trade.quantity is used for P&L math."""
    # qty=3, remaining_qty=None → should use qty=3
    # pnl = (6.75 - 5.00) * 3 * 100 = +525
    trade = _open_trade(entry_price=5.00, qty=3, remaining_qty=None)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.75
    )
    await db.refresh(trade)
    assert trade.pnl == pytest.approx(525.0, abs=0.01)


@pytest.mark.asyncio
async def test_exit_reason_persisted(db):
    """exit_reason field on the trade record must match what was passed."""
    for reason in [ExitReason.STOP, ExitReason.TP2, ExitReason.CUTOFF,
                   ExitReason.VWAP_BREAK, ExitReason.TREND_REVERSAL]:
        trade = _open_trade()
        db.add(trade)
        await db.commit()
        await db.refresh(trade)

        await _close_trade(
            db, _client(fill_price=None, order_status="filled"),
            trade, reason, exit_price=5.00
        )
        await db.refresh(trade)
        assert trade.exit_reason == reason, f"exit_reason mismatch for {reason}"
        assert trade.status == TradeStatus.CLOSED


@pytest.mark.asyncio
async def test_exit_time_is_populated(db):
    """exit_time must be set to a non-None UTC datetime after close."""
    trade = _open_trade()
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    assert trade.exit_time is None
    before = datetime.now(tz=timezone.utc)
    await _close_trade(
        db, _client(fill_price=None, order_status="filled"),
        trade, ExitReason.TP2, exit_price=6.00
    )
    after = datetime.now(tz=timezone.utc)
    await db.refresh(trade)
    assert trade.exit_time is not None
    # exit_time must be between 'before' and 'after' (within a few seconds)
    et = trade.exit_time
    if et.tzinfo is None:
        from datetime import timezone as tz
        et = et.replace(tzinfo=tz.utc)
    assert before <= et <= after
