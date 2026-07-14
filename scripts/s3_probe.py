"""S3 pre-flight probe — verifies Moomoo OpenD connectivity and data rights.

Run on the machine where OpenD is running:

    uv run python scripts/s3_probe.py

Checks, in order:
  1. moomoo/futu SDK importable
  2. OpenQuoteContext connects to S3_OPEND_HOST:S3_OPEND_PORT
  3. global state / market status
  4. subscription to ORDER_BOOK + TICKER + K_1M + K_5M for one S1 symbol
     (this is where missing US LV2 rights fail loudly)
  5. synchronous order-book + snapshot pulls (work even when market closed)
  6. counts push events for ~8 s (expect >0 only during market hours)

Writes a full report to s3_probe_result.txt in the project root and prints
it, so results can be reviewed after the fact.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

LINES: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True)
    LINES.append(msg)


def finish(code: int) -> None:
    (ROOT / "s3_probe_result.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    say(f"\nReport written to s3_probe_result.txt — exit {code}")
    sys.exit(code)


def main() -> None:
    say(f"S3 OpenD probe — {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"Target: {settings.s3_opend_host}:{settings.s3_opend_port} "
        f"| broker={settings.s3_broker} | s3_enabled={settings.s3_enabled}")

    # 1 — SDK
    try:
        import moomoo as sdk  # type: ignore
    except ImportError:
        try:
            import futu as sdk  # type: ignore
        except ImportError:
            say("FAIL [1] moomoo SDK not installed — run: uv sync")
            finish(1)
    say(f"OK   [1] SDK: {sdk.__name__} {getattr(sdk, '__version__', '?')}")

    # 2 — connect
    try:
        quote = sdk.OpenQuoteContext(host=settings.s3_opend_host,
                                     port=settings.s3_opend_port)
    except Exception as exc:  # noqa: BLE001
        say(f"FAIL [2] cannot connect to OpenD: {exc}")
        say("        Is OpenD running and listening on the port above?")
        finish(2)
    say("OK   [2] OpenQuoteContext connected")

    try:
        # 3 — global state
        ret, state = quote.get_global_state()
        if ret != sdk.RET_OK:
            say(f"FAIL [3] get_global_state: {state}")
            finish(3)
        us_market = state.get("market_us", "?") if isinstance(state, dict) else state
        say(f"OK   [3] global state (US market: {us_market})")

        # pick a probe symbol from the S1 watchlist if possible
        symbol = "US.AAPL"
        try:
            import sqlite3
            db = sqlite3.connect(ROOT / "ajoy.db")
            row = db.execute(
                "SELECT ticker FROM symbols WHERE active=1 AND s1_enabled=1 LIMIT 1"
            ).fetchone()
            db.close()
            if row:
                symbol = f"US.{row[0]}"
        except Exception:  # noqa: BLE001
            pass
        say(f"     probe symbol: {symbol}")

        # 4 — subscribe (LV2 rights check happens here)
        counts = {"ticker": 0, "book": 0, "kline": 0}

        class _T(sdk.TickerHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret_, data = super().on_recv_rsp(rsp_pb)
                if ret_ == sdk.RET_OK:
                    counts["ticker"] += len(data)
                return sdk.RET_OK, data

        class _B(sdk.OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret_, data = super().on_recv_rsp(rsp_pb)
                if ret_ == sdk.RET_OK:
                    counts["book"] += 1
                return sdk.RET_OK, data

        class _K(sdk.CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret_, data = super().on_recv_rsp(rsp_pb)
                if ret_ == sdk.RET_OK:
                    counts["kline"] += len(data)
                return sdk.RET_OK, data

        quote.set_handler(_T())
        quote.set_handler(_B())
        quote.set_handler(_K())

        subs = [sdk.SubType.ORDER_BOOK, sdk.SubType.TICKER,
                sdk.SubType.K_1M, sdk.SubType.K_5M]
        ret, err = quote.subscribe([symbol], subs, subscribe_push=True)
        if ret != sdk.RET_OK:
            say(f"FAIL [4] subscribe rejected: {err}")
            say("        Typical cause: no US LV2 (order book) quote rights on")
            say("        this moomoo account, or quota exhausted. Check the")
            say("        entitlements shown in the moomoo app / OpenD console.")
            finish(4)
        say(f"OK   [4] subscribed {symbol} → ORDER_BOOK + TICKER + K_1M + K_5M")

        # 5 — synchronous pulls (validate rights even while market closed)
        ret, book = quote.get_order_book(symbol, num=10)
        if ret == sdk.RET_OK:
            bids = book.get("Bid", []) if isinstance(book, dict) else []
            asks = book.get("Ask", []) if isinstance(book, dict) else []
            say(f"OK   [5] get_order_book: {len(bids)} bid / {len(asks)} ask levels"
                f"{' (empty book is normal while closed)' if not bids and not asks else ''}")
            if bids and asks:
                say(f"         best bid {bids[0][0]} × {bids[0][1]} | "
                    f"best ask {asks[0][0]} × {asks[0][1]}")
        else:
            say(f"WARN [5] get_order_book failed: {book} — LV2 rights may be missing")

        ret, snap = quote.get_market_snapshot([symbol])
        if ret == sdk.RET_OK and len(snap):
            r = snap.iloc[0]
            say(f"OK   [5] snapshot: last={r.get('last_price')} "
                f"volume={r.get('volume')} time={r.get('update_time')}")
        else:
            say(f"WARN [5] snapshot failed: {snap}")

        # 6 — push activity
        say("     [6] listening for pushes for 8 s "
            "(expect >0 only during US market hours)…")
        time.sleep(8)
        say(f"OK   [6] pushes: ticker={counts['ticker']} "
            f"book={counts['book']} kline={counts['kline']}")

        weekday = datetime.now().weekday() < 5
        if not weekday:
            say("     NOTE: market is closed today (weekend) — zero pushes is")
            say("     expected. Rights are verified by steps 4–5 above.")

        say("\nRESULT: OpenD reachable, subscriptions accepted — S3 data path OK.")
        if not settings.s3_enabled:
            say("REMINDER: S3_ENABLED=0 — set it to 1 and restart the app to run S3.")
        finish(0)
    finally:
        try:
            quote.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
