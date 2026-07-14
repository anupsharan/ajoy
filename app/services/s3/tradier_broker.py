"""TradierEquityBroker — S3 execution adapter for Tradier (stocks).

S3's division of labor:  Moomoo OpenD supplies MARKET DATA only (tick + L2);
this adapter routes every ORDER to Tradier, reusing the app's existing
credentials.  `USE_SANDBOX` picks the endpoint exactly as it does for S1/S2:

    USE_SANDBOX=1 → sandbox orders (paper)
    USE_SANDBOX=0 → LIVE production orders

Runs synchronously (httpx.Client) because the S3 engine owns its own thread —
the app's async TradierClient belongs to the FastAPI event loop.

Tradier has no order push stream in this app, so fills are discovered by
POLLING open orders (engine housekeeping, ~1 Hz): each snapshot goes through
`OrderManager.on_order_snapshot`, which synthesizes idempotent incremental
FillEvents from monotonic `exec_quantity` deltas.
"""
from __future__ import annotations

import logging
import threading

import httpx

from app.config import settings
from app.services.s3.types import OrderUpdate, now

logger = logging.getLogger(__name__)

# Tradier order statuses → the OMS _STATUS_MAP keys (see order_manager.py).
_TRADIER_STATUS = {
    "pending": "SUBMITTED",
    "open": "SUBMITTED",
    "partially_filled": "FILLED_PART",
    "filled": "FILLED_ALL",
    "canceled": "CANCELLED_ALL",
    "expired": "CANCELLED_ALL",
    "rejected": "REJECTED",
    "error": "FAILED",
}


class TradierEquityBroker:
    """Implements the OMS Broker protocol + the fetch/poll surface the
    S3 engine needs.  Thread-safe: one lock around the HTTP client."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # S3-only sandbox override — decoupled from the global USE_SANDBOX
        # so S1/S2 keep their own routing untouched.
        override = str(settings.s3_use_sandbox).strip().lower()
        if override in ("1", "true", "yes"):
            sandbox = True
        elif override in ("0", "false", "no"):
            sandbox = False
        else:  # "inherit"
            sandbox = settings.use_sandbox
        if sandbox:
            base = settings.tradier_base_url_sandbox
            token = settings.tradier_api_token_sandbox
            self._account = settings.tradier_account_id_sandbox
            self.mode = "SANDBOX"
        else:
            base = settings.tradier_base_url
            token = settings.tradier_api_token
            self._account = settings.tradier_account_id
            self.mode = "LIVE"
        self._base = base.rstrip("/")
        self._client: httpx.Client | None = None
        try:
            self._client = httpx.Client(
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001 — never crash app startup
            logger.error("[S3] Tradier HTTP client init failed: %s", exc)
        self.available = bool(token and self._account and self._client is not None)
        if not self.available:
            logger.error("[S3] Tradier broker unavailable (%s mode) — "
                         "S3 will run signal-only", self.mode)

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def describe(self) -> str:
        return f"tradier/{self.mode} acct={self._account or 'MISSING'}"

    # ── HTTP helpers ─────────────────────────────────────────────
    def _request(self, method: str, path: str, **kwargs) -> dict | None:
        if self._client is None:
            return None
        url = f"{self._base}{path}"
        try:
            with self._lock:
                resp = self._client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[S3][TRADIER] %s %s → %s: %s", method, path,
                         exc.response.status_code, exc.response.text[:300])
        except httpx.HTTPError as exc:
            logger.error("[S3][TRADIER] %s %s failed: %s", method, path, exc)
        return None

    # ── Broker protocol (OMS) ────────────────────────────────────
    def place_limit(self, symbol: str, side: str, qty: int, price: float) -> str | None:
        if not self.available:
            logger.error("[S3][TRADIER] order refused — broker unavailable")
            return None
        payload = {
            "class": "equity",
            "symbol": symbol,
            "side": "buy" if side == "BUY" else "sell",
            "quantity": str(qty),
            "type": "limit",
            "duration": "day",
            "price": f"{price:.2f}",
        }
        data = self._request("POST", f"/accounts/{self._account}/orders", data=payload)
        if data is None:
            return None
        order = data.get("order") or {}
        oid = order.get("id")
        if oid is None or str(order.get("status", "")).lower() == "error":
            logger.error("[S3][TRADIER] order rejected: %s", data)
            return None
        return str(oid)

    def cancel_order(self, order_id: str) -> bool:
        data = self._request("DELETE", f"/accounts/{self._account}/orders/{order_id}")
        return data is not None

    def modify_order_supported(self) -> bool:
        return True  # PUT /orders/{id} exists; S3 only uses cancel + re-place

    # ── Polling / reconciliation ─────────────────────────────────
    @staticmethod
    def _row_to_update(row: dict) -> OrderUpdate:
        status = _TRADIER_STATUS.get(str(row.get("status", "")).lower(), "SUBMITTED")
        return OrderUpdate(
            order_id=str(row.get("id", "")),
            symbol=str(row.get("symbol", "")).upper(),
            ts=now(),
            status=status,
            side="BUY" if "buy" in str(row.get("side", "")).lower() else "SELL",
            price=float(row.get("price") or 0.0),
            qty=int(float(row.get("quantity") or 0)),
            filled_qty=int(float(row.get("exec_quantity") or 0)),
            filled_avg_price=float(row.get("avg_fill_price") or 0.0),
        )

    def poll_order(self, order_id: str) -> OrderUpdate | None:
        data = self._request("GET", f"/accounts/{self._account}/orders/{order_id}")
        if data is None:
            return None
        row = data.get("order")
        return self._row_to_update(row) if row else None

    def fetch_open_orders(self) -> list[OrderUpdate]:
        data = self._request("GET", f"/accounts/{self._account}/orders")
        if data is None:
            return []
        orders = (data.get("orders") or {})
        if orders in ("null", None):
            return []
        rows = orders.get("order", [])
        if isinstance(rows, dict):
            rows = [rows]
        return [self._row_to_update(r) for r in rows
                if str(r.get("class", "equity")).lower() == "equity"]

    def fetch_positions(self) -> dict[str, int]:
        data = self._request("GET", f"/accounts/{self._account}/positions")
        if data is None:
            return {}
        positions = data.get("positions")
        if not positions or positions == "null":
            return {}
        rows = positions.get("position", [])
        if isinstance(rows, dict):
            rows = [rows]
        return {str(r.get("symbol", "")).upper(): int(float(r.get("quantity", 0)))
                for r in rows}

    def fetch_buying_power(self) -> float:
        data = self._request("GET", f"/accounts/{self._account}/balances")
        if data is None:
            return 0.0
        bal = data.get("balances") or {}
        # Margin accounts expose stock_buying_power; cash accounts cash_available.
        margin = bal.get("margin") or {}
        cash = bal.get("cash") or {}
        for source, key in ((margin, "stock_buying_power"),
                            (cash, "cash_available"),
                            (bal, "total_cash")):
            val = source.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 0.0
