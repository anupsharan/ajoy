"""
Jul 27 2026 regressions — exits fired on stale quotes, and a cooldown hole.

Incidents
---------
WMT #195 : bot stop $1.06.  Exit triggered on a quote of $1.00 and the market
           paid $1.14 two seconds later (+14.0%).  At $1.14 the trade was
           −7.3%, nowhere near its −14% stop.  WMT then rallied.
ORCL #197: S2 quick-loss (−19%).  Triggered on $3.65, filled $4.18 (+14.5%).
           At $4.18 the trade was −5.0%.
Both fired during dense `Tradier timeout` warnings.  The manage loop used to
fall back to `last` (the most recent TRADE print, which on a thin option can
be minutes stale) whenever a side of the quote was missing, and then treated
that stale number as the market.

ORCL #197 → #198: S2 quick-lossed ORCL at 14:58 and S1 opened the SAME symbol
           8 minutes later, because S1's re-entry cooldown listed only
           STOP / VWAP_BREAK / MANUAL.  S2's own cooldown already counted
           QUICK_LOSS; S1's did not.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Direction, ExitReason, Trade, TradeStatus
from app.services.tradier import Quote


def _trade(**kw) -> Trade:
    """An S1 CALL sitting well above its stop on a healthy quote."""
    defaults = dict(
        symbol="WMT", option_symbol="WMT260731C00111000",
        direction=Direction.CALL, strategy_name="vwap_pullback",
        quantity=4, remaining_qty=4,
        entry_price=1.23, entry_time=datetime.now(timezone.utc) - timedelta(minutes=30),
        stop_price=1.06, original_stop_price=1.06, tp2_price=1.45,
        status=TradeStatus.OPEN, created_at=datetime.now(timezone.utc),
        tp1_hit=False, be_stop_set=False, runner_mode=False,
    )
    defaults.update(kw)
    return Trade(**defaults)


@pytest.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/dq.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


class _QuoteClient:
    """
    Records every sell the manage loop attempts.

    `last` is deliberately far BELOW the stop — that is the stale print that
    used to trigger the exit when bid/ask were unusable.
    """

    def __init__(self, bid, ask, last):
        self._q = Quote(symbol="WMT260731C00111000", last=last, bid=bid,
                        ask=ask, volume=10)
        self.sells: list[dict] = []

    async def get_option_quote(self, _sym):
        return self._q

    async def place_option_order(self, **kw):
        self.sells.append(kw)
        from app.services.tradier import OrderResult
        return OrderResult(order_id="sell-1", status="ok")

    async def get_order_status(self, _oid):
        return {"status": "filled", "exec_quantity": 0, "avg_fill_price": 0}

    async def get_fill_price(self, _oid):
        return 1.14

    async def cancel_order(self, _oid):
        return {"status": "ok"}

    async def get_quote(self, _sym):
        # Underlying quote for the SIGNAL_FADE / thesis checks.  Must exist,
        # or the manage loop raises and the per-trade handler swallows it —
        # which would make every assertion below pass vacuously.
        return Quote(symbol="WMT", last=111.0, bid=110.99, ask=111.01,
                     volume=1_000_000)

    async def get_intraday_bars(self, *a, **k):
        return []

    async def get_positions(self):
        return []

    async def get_last_sell_fill(self, _sym):
        return None


async def _run_manage(factory, client, monkeypatch):
    from app.services import scheduler as sched
    monkeypatch.setattr(sched, "is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(sched, "is_past_cutoff", lambda *a, **k: False)
    monkeypatch.setattr(sched, "get_tradier_client", lambda *a, **k: client)
    monkeypatch.setattr(sched, "AsyncSessionLocal", factory)
    await sched.manage_open_trades()


# ===========================================================================
# The stale-quote guard
# ===========================================================================

@pytest.mark.parametrize("bid,ask,label", [
    (0.0, 1.20, "missing bid"),
    (1.00, 0.0, "missing ask"),
    (0.0, 0.0, "both sides gone"),
    (None, 1.20, "null bid"),
])
async def test_degraded_quote_never_triggers_an_exit(db_session, monkeypatch,
                                                     bid, ask, label):
    """
    WMT #195 / ORCL #197 regression.

    With a one-sided quote the loop must place NO order, whatever `last` says.
    `last=0.80` here is far below the $1.06 stop — the old code would have
    used it as the market and sold.
    """
    session, factory = db_session
    session.add(_trade())
    await session.commit()

    client = _QuoteClient(bid=bid, ask=ask, last=0.80)
    await _run_manage(factory, client, monkeypatch)

    assert client.sells == [], f"{label}: sold on a stale quote"

    from sqlalchemy import select
    async with factory() as s:
        t = (await s.execute(select(Trade))).scalars().first()
    assert t.status == TradeStatus.OPEN, f"{label}: trade was closed"


async def test_stale_last_below_stop_is_ignored_when_quote_is_one_sided(
        db_session, monkeypatch):
    """The exact WMT shape: real market $1.14, stale print $1.00, stop $1.06."""
    session, factory = db_session
    session.add(_trade())
    await session.commit()

    client = _QuoteClient(bid=0.0, ask=0.0, last=1.00)   # stale, under the stop
    await _run_manage(factory, client, monkeypatch)

    assert client.sells == []


# ── Control: the guard must not disable real stops ────────────────────────

async def test_two_sided_quote_below_stop_still_exits(db_session, monkeypatch):
    """
    The counterweight to the guard: with a healthy two-sided quote genuinely
    below the stop, the exit MUST still fire.  A guard that silently disabled
    stops would be far worse than the bug it fixes.
    """
    session, factory = db_session
    session.add(_trade())
    await session.commit()

    # mid = (0.98 + 1.02)/2 = 1.00, below the $1.06 stop
    client = _QuoteClient(bid=0.98, ask=1.02, last=1.00)
    await _run_manage(factory, client, monkeypatch)

    assert client.sells, "healthy quote below the stop did not exit"
    assert any("sell" in (o.get("side") or "") for o in client.sells)


async def test_two_sided_quote_above_stop_holds(db_session, monkeypatch):
    """Healthy quote comfortably above the stop — no exit."""
    session, factory = db_session
    session.add(_trade())
    await session.commit()

    client = _QuoteClient(bid=1.30, ask=1.34, last=1.32)
    await _run_manage(factory, client, monkeypatch)

    assert client.sells == []


# ===========================================================================
# S1 re-entry cooldown now counts QUICK_LOSS / STRUCT_EXIT
# ===========================================================================

@pytest.mark.parametrize("reason", [
    ExitReason.QUICK_LOSS,     # ORCL #197 -> #198
    ExitReason.STRUCT_EXIT,
    ExitReason.STOP,           # pre-existing, must still hold
    ExitReason.VWAP_BREAK,
    ExitReason.MANUAL,
])
async def test_s1_cooldown_covers_loss_exits(db_session, reason):
    from app.services.scheduler import _get_recent_bad_exit

    session, factory = db_session
    session.add(_trade(
        symbol="ORCL", status=TradeStatus.CLOSED,
        exit_time=datetime.now(timezone.utc) - timedelta(minutes=8),
        exit_price=4.18, pnl=-22.0, exit_reason=reason,
    ))
    await session.commit()

    async with factory() as s:
        assert await _get_recent_bad_exit(s, "ORCL") is not None, \
            f"S1 re-entered 8 min after a {reason.value} exit"


async def test_s1_cooldown_ignores_profitable_exits(db_session):
    """
    TP2 is handled by the separate tp_cooldown gate — it must NOT be swept
    into the loss cooldown, or a winner would block re-entry twice.
    """
    from app.services.scheduler import _get_recent_bad_exit

    session, factory = db_session
    session.add(_trade(
        symbol="ORCL", status=TradeStatus.CLOSED,
        exit_time=datetime.now(timezone.utc) - timedelta(minutes=8),
        exit_price=5.19, pnl=79.0, exit_reason=ExitReason.TP2,
    ))
    await session.commit()

    async with factory() as s:
        assert await _get_recent_bad_exit(s, "ORCL") is None


# ===========================================================================
# Levels editor must buffer the BROKER stop (PLTR #194)
# ===========================================================================
#
# The entry path places the broker order at _broker_stop_price(stop) — the
# bot's working stop minus broker_stop_buffer_pct — so it is a DISASTER
# backstop.  The UI levels editor used to send the raw stop, putting the
# broker order exactly ON the bot stop and promoting it to the primary exit.
# Tradier triggers on prints and fills at market, so it front-ran the bot's
# mid-based marketable-limit exit.  PLTR #194's broker stop was moved
# $3.32 -> $3.53 by an edit and it then exited via broker-side STOP @ $3.50.

async def _patch_levels(client, trade_id, stop, factory, monkeypatch):
    from app.routers import trades as tr
    monkeypatch.setattr(tr, "get_tradier_client", lambda *a, **k: client)

    class _Payload:
        stop_price = stop
        tp2_price = None

    async with factory() as s:
        from sqlalchemy import select
        t = (await s.execute(select(Trade).where(Trade.id == trade_id))).scalars().first()
        await tr.update_trade_levels(trade_id, _Payload(), s)
        await s.refresh(t)
        return t


class _BrokerStopClient:
    """Captures the stop price the broker is actually told to use."""

    def __init__(self):
        self.modified: list[float] = []
        self.placed: list[float] = []

    async def modify_order(self, _oid, stop_price=None, **kw):
        self.modified.append(stop_price)
        return {"status": "ok"}

    async def place_option_order(self, **kw):
        self.placed.append(kw.get("stop_price"))
        from app.services.tradier import OrderResult
        return OrderResult(order_id="stop-new", status="ok")

    async def cancel_order(self, _oid):
        return {"status": "ok"}


async def test_levels_edit_keeps_the_broker_stop_buffered(db_session, monkeypatch):
    """PLTR #194 regression — the edited broker stop must sit BELOW the bot stop."""
    from app.config import settings
    from app.services.scheduler import _broker_stop_price

    session, factory = db_session
    t = _trade(symbol="PLTR", option_symbol="PLTR260731C00127000",
               entry_price=4.10, stop_price=3.53, original_stop_price=3.53,
               tp2_price=4.84, quantity=1, remaining_qty=1,
               stop_order_id="138617554")
    session.add(t)
    await session.commit()

    client = _BrokerStopClient()
    monkeypatch.setattr(settings, "broker_stop_enabled", True)
    await _patch_levels(client, t.id, 3.53, factory, monkeypatch)

    assert client.modified, "broker stop was never updated"
    sent = client.modified[0]
    assert sent == _broker_stop_price(3.53), \
        f"broker got {sent}, expected buffered {_broker_stop_price(3.53)}"
    assert sent < 3.53, "broker stop must sit BELOW the bot stop, not on it"
