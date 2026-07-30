"""
Browser smoke test of the dashboard — does the page actually still work?

OPTIONAL developer tool, not part of the pytest suite (pytest.ini restricts
collection to tests/).  Needs Playwright + Chromium:

    uv pip install playwright && playwright install chromium
    python uitest.py two     # 2 accounts: filter + ACCOUNT column must appear
    python uitest.py one     # 1 account:  both must stay hidden

Writes ui_*.png screenshots next to the script.  Two Tradier-backed calls
(reconcile / balances) will 502 unless the seeded accounts have real tokens —
that is expected and is filtered out of the pass/fail decision.

Boots the real FastAPI app against a scratch DB, seeds two accounts and a
couple of trades, then drives Chromium:
  - every tab renders (Symbols / Indicators / Trades / Accounts / Settings)
  - the Accounts tab lists accounts and its controls are present
  - the account filter + ACCOUNT column appear ONLY with >1 account
  - zero JS console errors anywhere
"""
import asyncio
import os
import pathlib
import sys
import threading
import time
from datetime import datetime, timezone

DB = "./tmp_ui.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"
os.environ["SCHEDULER_ENABLED"] = "0"
for f in [DB, DB + "-shm", DB + "-wal"]:
    pathlib.Path(f).unlink(missing_ok=True)

import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as _appdb
import app.main as _appmain
from app.main import app
from app.models import Account, Direction, Symbol, Trade, TradeStatus

_e = create_async_engine(f"sqlite+aiosqlite:///{DB}")
_s = async_sessionmaker(_e, expire_on_commit=False, class_=AsyncSession)
_appdb.engine = _e
_appdb.AsyncSessionLocal = _s
_appmain.AsyncSessionLocal = _s

PORT = 8077
ERRORS: list[str] = []


async def seed(two_accounts: bool):
    from app.database import init_db
    from app.services.accounts import invalidate_account_cache, seed_default_account

    await init_db()
    async with _s() as db:
        await seed_default_account(db)
    invalidate_account_cache()

    async with _s() as db:
        db.add(Symbol(ticker="AAPL", active=True))
        if two_accounts:
            db.add(Account(
                name="Roth IRA", broker="tradier", account_number="VA12345678",
                api_token="tok-roth-abcd", data_api_token="", use_sandbox=True,
                enabled=True, is_primary=False, sort_order=1, notes="second account",
                s1_enabled=True, s2_enabled=False, s3_enabled=False,
                put_scalp_enabled=True, risk_per_trade=40.0,
            ))
        await db.commit()

        db.add_all([
            Trade(symbol="AAPL", option_symbol="AAPL260725C00150000",
                  direction=Direction.CALL, strategy_name="vwap_pullback",
                  account_id=1, quantity=2, remaining_qty=2, entry_price=2.50,
                  entry_time=datetime.now(tz=timezone.utc),
                  status=TradeStatus.OPEN, stop_price=2.10, tp2_price=3.00),
        ])
        if two_accounts:
            db.add(Trade(
                symbol="MSFT", option_symbol="MSFT260725C00400000",
                direction=Direction.CALL, strategy_name="ema_cross",
                account_id=2, quantity=1, remaining_qty=1, entry_price=1.80,
                entry_time=datetime.now(tz=timezone.utc),
                status=TradeStatus.OPEN, stop_price=1.50, tp2_price=2.20))
        await db.commit()
    invalidate_account_cache()


def run_server():
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


async def drive(two_accounts: bool):
    from playwright.async_api import async_playwright

    label = "TWO ACCOUNTS" if two_accounts else "ONE ACCOUNT"
    print(f"\n{'='*60}\n  {label}\n{'='*60}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            args=["--no-sandbox"],
        )
        page = await browser.new_page()
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        failed: list[str] = []
        page.on("requestfailed", lambda r: failed.append(f"FAILED {r.url}"))
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)

        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        await page.wait_for_timeout(1200)

        # Tabs present?
        tabs = await page.eval_on_selector_all(
            ".tab-btn", "els => els.map(e => e.textContent.trim())")
        print(f"tabs rendered: {tabs}")
        if not tabs:
            print("CONSOLE:", console_errors[:10])
            print("NETWORK PROBLEMS:", failed[:10])
        assert "Accounts" in tabs, "Accounts tab missing"
        for t in ("Symbols", "Indicators", "Trades", "Settings"):
            assert t in tabs, f"pre-existing tab '{t}' disappeared"

        # Each tab renders without blowing up.
        for tab in ("Symbols", "Indicators", "Trades", "Accounts", "Settings"):
            await page.click(f".tab-btn:text-is('{tab}')")
            await page.wait_for_timeout(500)
            visible = await page.eval_on_selector_all(
                "section", "els => els.filter(e => e.offsetParent !== null).length")
            print(f"  {tab:12s} → {visible} visible section(s)")
            assert visible >= 1, f"{tab} rendered nothing"

        # ── Accounts tab content ────────────────────────────────────────
        await page.click(".tab-btn:text-is('Accounts')")
        await page.wait_for_timeout(600)
        body = await page.inner_text("body")

        await page.screenshot(
            path=f"./ui_accounts_{'two' if two_accounts else 'one'}.png",
            full_page=True)
        assert "Primary" in body, "primary account not listed"
        if two_accounts:
            assert "Roth IRA" in body, "second account not listed"
        # Token must be masked, never raw
        assert "tok-roth-abcd" not in body, "RAW TOKEN RENDERED IN THE PAGE"
        if two_accounts:
            assert "••••abcd" in body, "masked token not shown"
        print(f"  accounts listed, tokens masked ✓")

        # Strategy pills + controls
        for ctrl in ("+ Add account", "Test", "Edit"):
            assert ctrl in body, f"control '{ctrl}' missing from Accounts tab"
        print("  add / test / edit controls present ✓")

        # Open the edit panel and the add form — templates only fail when shown.
        await page.click("button:text-is('Edit')")
        await page.wait_for_timeout(400)
        panel = await page.inner_text("body")
        assert "SIZING & SLOTS" in panel, "edit panel did not render"
        assert "New API token" in panel, "token field missing from edit panel"
        await page.screenshot(
            path=f"./ui_accounts_edit_{'two' if two_accounts else 'one'}.png",
            full_page=True)
        print("  edit panel renders ✓")

        await page.click("button:has-text('+ Add account')")
        await page.wait_for_timeout(400)
        add_form = await page.inner_text("body")
        assert "NEW ACCOUNT" in add_form, "add-account form did not render"
        print("  add-account form renders ✓")

        # ── Trades tab: filter visibility depends on account count ──────
        await page.click(".tab-btn:text-is('Trades')")
        await page.wait_for_timeout(800)

        filter_visible = await page.evaluate("""() => {
            const sel = document.querySelector('select[x-model="accountFilter"]');
            return !!(sel && sel.offsetParent !== null);
        }""")
        headers = await page.eval_on_selector_all(
            "table.data-table thead th",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent.trim())")
        has_account_col = "ACCOUNT" in headers

        print(f"  account filter visible: {filter_visible}")
        print(f"  ACCOUNT column visible: {has_account_col}")
        print(f"  open-position headers: {headers[:6]}")

        if two_accounts:
            assert filter_visible, "filter must appear with 2 accounts"
            assert has_account_col, "ACCOUNT column must appear with 2 accounts"
            rows = await page.inner_text("body")
            assert "Roth IRA" in rows, "second account's name missing from trades view"
        else:
            assert not filter_visible, "filter must be HIDDEN with 1 account"
            assert not has_account_col, "ACCOUNT column must be HIDDEN with 1 account"

        # Trade rows still render
        n_rows = await page.eval_on_selector_all(
            "table.data-table tbody tr", "els => els.length")
        print(f"  trade rows rendered: {n_rows}")
        assert n_rows >= 1, "no trade rows rendered"

        # Separate real app errors from sandbox-environment noise.
        # Blocked cdnjs (Chart.js) and 502s from Tradier calls with fake
        # tokens are expected here and are NOT regressions.
        # 404 = /favicon.ico (no favicon in this repo); 502 = Tradier calls
        # with the sandbox's fake tokens; ERR_TUNNEL = cdnjs Chart.js blocked
        # by the sandbox proxy.  None are caused by this change.
        ENV_NOISE = ("ERR_TUNNEL_CONNECTION_FAILED", "cdnjs.cloudflare.com",
                     "502 (Bad Gateway)", "ERR_NAME_NOT_RESOLVED",
                     "404 (Not Found)")
        real = [e for e in console_errors if not any(n in e for n in ENV_NOISE)]
        print(f"  console errors (all):        {len(console_errors)}")
        for e in console_errors:
            print(f"      - {e[:110]}")
        print(f"  console errors (app-caused): {len(real)}")
        if real:
            ERRORS.extend(real)

        # Alpine expression failures surface as pageerror — any of those is a
        # genuine template bug.
        alpine = [e for e in console_errors if "pageerror" in e]
        assert not alpine, f"Alpine/template errors: {alpine}"
        print("  network problems:")
        for f in failed:
            if "404" in f or "FAILED" in f or "502" in f:
                print(f"      - {f[:130]}")

        await page.screenshot(
            path=f"./ui_{'two' if two_accounts else 'one'}.png",
            full_page=True)
        await browser.close()


async def main():
    two = sys.argv[1] == "two"
    await seed(two_accounts=two)
    threading.Thread(target=run_server, daemon=True).start()
    time.sleep(3.0)
    await drive(two_accounts=two)
    if ERRORS:
        print("\nFAILED — JS errors present")
        sys.exit(1)
    print("\nUI CHECKS PASSED")


asyncio.run(main())
