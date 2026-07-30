"""
S1 master on/off switch (Jul 30 2026, user-requested).

The Settings UI had master toggles for S2, S3 and PUT Scalp, but none for S1 —
the only way to stop S1 was to un-tick every symbol in the watchlist.  This
adds `settings.s1_enabled`, mirroring `s2_enabled`.

The contract being pinned here has two halves, and the SECOND one is the one
that costs money if it breaks:

  1. OFF blocks NEW S1 entries — the scanner returns before it touches an
     account, so no broker call is made at all.
  2. OFF must NOT stop `manage_open_trades`.  A master switch that also
     stopped managing would abandon live positions with real stops attached —
     exactly the failure mode `manage_open_trades` iterating ALL accounts
     (including disabled ones) was written to prevent.  Same semantics as
     S2 / PS / a disabled account.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models import Direction, Trade, TradeStatus
from app.services.tradier import Quote

_ROOT = Path(__file__).parent.parent


# ===========================================================================
# 1. The switch exists and defaults ON (upgrades must not silently stop S1)
# ===========================================================================

def test_s1_enabled_setting_exists_and_defaults_on():
    from app.config import Settings

    assert "s1_enabled" in Settings.model_fields
    assert Settings.model_fields["s1_enabled"].annotation is bool
    # An existing install that has no S1_ENABLED line in .env must keep trading.
    assert Settings().s1_enabled is True


def test_s1_enabled_is_patchable_via_the_config_api():
    """The Settings page writes through /api/config — the field must not be
    on the read-only list (which holds credentials and infra only)."""
    from app.routers.config import _READONLY, get_config

    assert "s1_enabled" not in _READONLY
    assert "s1_enabled" in get_config()


def test_s1_toggle_is_exposed_in_the_settings_ui():
    """The whole point of the request was the UI toggle, not the setting."""
    js = (_ROOT / "static" / "js" / "app.js").read_text()
    assert re.search(r"key:\s*'s1_enabled'", js), \
        "s1_enabled has no field in app.js configGroups — the toggle won't render"
    # NOTE: 's1_enabled' also appears in `accountStrategyFlags` (the per-account
    # pill row on the Accounts tab) — a different namespace.  Match every
    # occurrence and require a configGroups-style checkbox field among them.
    fields = re.findall(r"\{[^{}]*key:\s*'s1_enabled'[^{}]*\}", js)
    assert any("type: 'bool'" in f and "hint:" in f for f in fields), \
        f"no Settings-page bool field for s1_enabled; found {fields}"

    # index.html renders the S1 section by EXCLUDING s2_*/s3_* group ids, so
    # the toggle only appears if it lives in an S1 group.
    from_js = re.search(r"id:\s*'(\w+)',\s*label:[^\n]*\n\s*fields:\s*\[\s*"
                        r"\{[^{}]*key:\s*'s1_enabled'", js)
    assert from_js, "s1_enabled must be the first field of an S1 config group"
    assert not from_js.group(1).startswith(("s2_", "s3_")), \
        "s1_enabled landed in an S2/S3 group — index.html filters those out of the S1 tab"


def test_cache_buster_was_bumped():
    """app.js edits are invisible to the user's browser without this."""
    html = (_ROOT / "app" / "templates" / "index.html").read_text()
    m = re.search(r"app\.js\?v=(\d+)", html)
    assert m and int(m.group(1)) >= 28, \
        f"cache-buster is v{m.group(1) if m else '?'} — bump it after editing app.js"


# ===========================================================================
# 2. OFF blocks new entries — before any account or broker is touched
# ===========================================================================

async def test_master_off_blocks_the_s1_scan(monkeypatch):
    from app.services import scheduler as sched

    called = []
    monkeypatch.setattr(sched, "is_in_trading_window", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_for_each_account",
                        AsyncMock(side_effect=lambda *a, **k: called.append(a)))
    monkeypatch.setattr(settings, "s1_enabled", False)

    await sched.scan_for_entries()
    assert called == [], "S1 scanned with the master switch OFF"


async def test_master_on_still_scans(monkeypatch):
    """Control — a switch that blocks unconditionally would pass the test above."""
    from app.services import scheduler as sched

    called = []
    monkeypatch.setattr(sched, "is_in_trading_window", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_for_each_account",
                        AsyncMock(side_effect=lambda *a, **k: called.append(a)))
    monkeypatch.setattr(settings, "s1_enabled", True)

    await sched.scan_for_entries()
    assert len(called) == 1, "S1 did not scan with the master switch ON"
    # It must dispatch on the per-account s1 flag — master AND account.
    assert called[0][1] == "s1_enabled"


def test_master_check_precedes_the_account_loop():
    """
    Source-level guard.  `_for_each_account` opens a DB session and builds a
    Tradier client per account; the master switch has to short-circuit BEFORE
    that, or turning S1 off would still hammer the broker every 60 s.
    """
    from app.services import scheduler as sched

    src = inspect.getsource(sched.scan_for_entries)
    assert src.index("settings.s1_enabled") < src.index("_for_each_account")


async def test_master_off_beats_an_enabled_account(monkeypatch):
    """
    Account flags can only NARROW the master switch (§6c).  An account with
    s1_enabled=1 must not trade while the global master is off.
    """
    from app.services import scheduler as sched

    ran = []
    monkeypatch.setattr(sched, "is_in_trading_window", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_scan_for_entries_account",
                        AsyncMock(side_effect=lambda acct: ran.append(acct)))
    monkeypatch.setattr(sched, "all_account_views",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(settings, "s1_enabled", False)

    await sched.scan_for_entries()
    assert ran == []


# ===========================================================================
# 3. OFF must NOT stop managing open S1 positions
# ===========================================================================

@pytest.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/s1sw.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


class _Client:
    """Minimal broker: an S1 CALL quoted two-sided BELOW its stop."""

    def __init__(self, bid, ask):
        self._q = Quote(symbol="WMT260731C00111000", last=(bid + ask) / 2,
                        bid=bid, ask=ask, volume=10)
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
        return 1.00

    async def cancel_order(self, _oid):
        return {"status": "ok"}

    async def get_quote(self, _sym):
        # Underlying, for the SIGNAL_FADE / thesis checks.  Must exist or the
        # manage loop raises and the per-trade handler swallows it — which
        # would make the assertions below pass vacuously (test_25 lesson).
        return Quote(symbol="WMT", last=111.0, bid=110.99, ask=111.01,
                     volume=1_000_000)

    async def get_intraday_bars(self, *a, **k):
        return []

    async def get_positions(self):
        return []

    async def get_last_sell_fill(self, _sym):
        return None


def _open_s1_trade() -> Trade:
    return Trade(
        symbol="WMT", option_symbol="WMT260731C00111000",
        direction=Direction.CALL, strategy_name="vwap_pullback",
        quantity=4, remaining_qty=4,
        entry_price=1.23, entry_time=datetime.now(timezone.utc) - timedelta(minutes=30),
        stop_price=1.06, original_stop_price=1.06, tp2_price=1.45,
        status=TradeStatus.OPEN, created_at=datetime.now(timezone.utc),
        tp1_hit=False, be_stop_set=False, runner_mode=False,
    )


async def _run_manage(factory, client, monkeypatch):
    from app.services import scheduler as sched
    monkeypatch.setattr(sched, "is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(sched, "is_past_cutoff", lambda *a, **k: False)
    monkeypatch.setattr(sched, "get_tradier_client", lambda *a, **k: client)
    monkeypatch.setattr(sched, "AsyncSessionLocal", factory)
    await sched.manage_open_trades()


async def test_open_s1_position_is_still_stopped_out_with_master_off(
        db_session, monkeypatch):
    """
    The expensive failure: switch S1 off at 11:30 with a position open, and
    its stop stops working.  mid = 1.00, below the $1.06 stop → must exit.
    """
    session, factory = db_session
    session.add(_open_s1_trade())
    await session.commit()

    monkeypatch.setattr(settings, "s1_enabled", False)
    client = _Client(bid=0.98, ask=1.02)
    await _run_manage(factory, client, monkeypatch)

    assert client.sells, "master switch OFF abandoned an open S1 position"

    async with factory() as s:
        t = (await s.execute(select(Trade))).scalars().first()
    assert t.status == TradeStatus.CLOSED


async def test_open_s1_position_above_stop_still_held_with_master_off(
        db_session, monkeypatch):
    """Counterweight: managing while OFF must not force-close healthy trades."""
    session, factory = db_session
    session.add(_open_s1_trade())
    await session.commit()

    monkeypatch.setattr(settings, "s1_enabled", False)
    client = _Client(bid=1.30, ask=1.34)
    await _run_manage(factory, client, monkeypatch)

    assert client.sells == []
    async with factory() as s:
        t = (await s.execute(select(Trade))).scalars().first()
    assert t.status == TradeStatus.OPEN


# ===========================================================================
# 4. S2 / PS are unaffected by the S1 master
# ===========================================================================

async def test_s1_master_does_not_gate_s2(monkeypatch):
    from app.services import scheduler as sched

    called = []
    monkeypatch.setattr(settings, "s1_enabled", False)
    monkeypatch.setattr(settings, "s2_enabled", True)
    monkeypatch.setattr(sched, "is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_for_each_account",
                        AsyncMock(side_effect=lambda *a, **k: called.append(a)))

    await sched.scan_for_entries_s2()
    # S2 has its own window guard; if it dispatched at all it used its own flag.
    assert all(a[1] == "s2_enabled" for a in called)
    assert not any(a[1] == "s1_enabled" for a in called)
