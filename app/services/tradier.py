"""
Tradier dual-environment client.

Market data  → Production API  (api.tradier.com)    TRADIER_API_TOKEN
Order/account → Sandbox API    (sandbox.tradier.com) TRADIER_API_TOKEN_SANDBOX

This lets the app receive real-time quotes/bars/chains while all order
execution is paper-traded in the Tradier sandbox account.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import asyncio

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float
    volume: int
    vwap: Optional[float] = None
    change_pct: Optional[float] = None
    description: Optional[str] = None


@dataclass
class OptionQuote:
    symbol: str
    underlying: str
    expiration_date: str
    option_type: str          # "call" | "put"
    strike: float
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    iv: Optional[float] = None
    mid: float = 0.0

    def __post_init__(self):
        self.mid = round((self.bid + self.ask) / 2, 2)


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Position:
    symbol: str
    quantity: int
    cost_basis: float
    date_acquired: Optional[str] = None


@dataclass
class AccountBalance:
    account_value: float
    cash: float
    buying_power: float
    option_buying_power: float


@dataclass
class OrderResult:
    order_id: str
    status: str
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TradierClient:
    """
    Async Tradier client.

    Market data always uses the production API.
    Order execution uses sandbox or production based on USE_SANDBOX in .env:
      USE_SANDBOX=1  → sandbox  (paper trading, safe default)
      USE_SANDBOX=0  → production  (LIVE REAL MONEY)
    """

    # Timeouts applied to both shared clients.
    #   connect=5s  — fail fast if Tradier's edge is unreachable
    #   read=8s     — bar/quote fetches: fail fast so a slow Tradier response
    #                 doesn't block the whole scan past its 60s interval.
    #                 (was 20s — with 21 symbols × 2 calls through a 10-conn
    #                 pool the old value could push a scan past 60s, causing
    #                 APScheduler to skip the next cycle)
    #   write=10s   — for order POSTs
    #   pool=5s     — max wait to acquire a connection from the pool
    _TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=10.0, pool=5.0)

    # Connection pool limits for the shared clients.
    # max_connections      — hard cap on simultaneous open sockets
    # max_keepalive_connections — how many idle connections to keep warm
    # keepalive_expiry     — drop idle connections after this many seconds
    _LIMITS = httpx.Limits(
        max_connections=10,
        max_keepalive_connections=5,
        keepalive_expiry=30.0,
    )

    _RETRY_DELAY = 1.5   # seconds before retrying a ReadTimeout

    def __init__(self):
        # Production: market data (always live)
        self._data_base = settings.tradier_base_url.rstrip("/")
        data_headers = {
            "Authorization": f"Bearer {settings.tradier_api_token}",
            "Accept": "application/json",
        }

        # Order execution: sandbox or production based on USE_SANDBOX flag
        if settings.use_sandbox:
            self._order_base = settings.tradier_base_url_sandbox.rstrip("/")
            order_headers = {
                "Authorization": f"Bearer {settings.tradier_api_token_sandbox}",
                "Accept": "application/json",
            }
        else:
            # ── LIVE MODE — real money ──────────────────────────────
            self._order_base = settings.tradier_base_url.rstrip("/")
            order_headers = {
                "Authorization": f"Bearer {settings.tradier_api_token}",
                "Accept": "application/json",
            }

        # Account ID: sandbox has a different account number than production
        self._account_id = (
            settings.tradier_account_id_sandbox
            if settings.use_sandbox
            else settings.tradier_account_id
        )

        # Shared persistent HTTP clients — one per API endpoint.
        # Using a single long-lived client per destination allows httpx to
        # reuse TCP connections (HTTP keep-alive) across requests instead of
        # opening a fresh connection for every API call.  This eliminates the
        # burst of simultaneous connection establishments that caused ReadTimeout
        # during busy scan cycles.
        #
        # NOTE: base_url is intentionally NOT used here.  httpx follows RFC 3986
        # URL joining — a path starting with "/" is treated as absolute and would
        # strip the "/v1" prefix from the base.  We build the full URL in each
        # helper instead, which is unambiguous.
        self._data_headers  = data_headers
        self._order_headers = order_headers
        self._data_client  = httpx.AsyncClient(
            timeout=self._TIMEOUT,
            limits=self._LIMITS,
        )
        self._order_client = httpx.AsyncClient(
            timeout=self._TIMEOUT,
            limits=self._LIMITS,
        )

    async def close(self) -> None:
        """Release the shared HTTP connections.  Call on app shutdown."""
        await self._data_client.aclose()
        await self._order_client.aclose()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _data_get(self, path: str, params: dict | None = None) -> Any:
        """GET against the production (market data) API.

        No automatic retry: market data calls (bars, quotes) are non-critical
        — a timeout is logged and the symbol is silently skipped for this scan
        cycle, then retried on the next tick (~60s).  Retrying inline doubles
        the worst-case call time and can push the whole scan past the 60s
        APScheduler interval, causing "max instances" skips.
        """
        url = f"{self._data_base}{path}"
        resp = await self._data_client.get(
            url, headers=self._data_headers, params=params or {}
        )
        resp.raise_for_status()
        return resp.json()

    async def _order_get(self, path: str, params: dict | None = None) -> Any:
        """GET against the order/account API."""
        url = f"{self._order_base}{path}"
        resp = await self._order_client.get(url, headers=self._order_headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def _order_post(self, path: str, data: dict) -> Any:
        """POST against the order API."""
        url = f"{self._order_base}{path}"
        resp = await self._order_client.post(url, headers=self._order_headers, data=data)
        resp.raise_for_status()
        return resp.json()

    async def _order_delete(self, path: str) -> Any:
        """DELETE against the order API."""
        url = f"{self._order_base}{path}"
        resp = await self._order_client.delete(url, headers=self._order_headers)
        resp.raise_for_status()
        return resp.json()

    async def _order_put(self, path: str, data: dict) -> Any:
        """PUT against the order API (order modification)."""
        url = f"{self._order_base}{path}"
        resp = await self._order_client.put(url, headers=self._order_headers, data=data)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Market data  (production API)
    # ------------------------------------------------------------------

    async def get_quote(self, symbol: str) -> Quote:
        """Fetch a real-time equity quote."""
        data = await self._data_get("/markets/quotes", {"symbols": symbol, "greeks": "false"})
        q = data["quotes"]["quote"]
        if isinstance(q, list):
            q = q[0]
        return Quote(
            symbol=q["symbol"],
            last=float(q.get("last") or q.get("close") or 0),
            bid=float(q.get("bid") or 0),
            ask=float(q.get("ask") or 0),
            volume=int(q.get("volume") or 0),
            change_pct=float(q.get("change_percentage") or 0),
            description=q.get("description"),
        )

    async def get_option_quote(self, option_symbol: str) -> Optional[Quote]:
        """Fetch a real-time quote for a single option contract."""
        try:
            data = await self._data_get(
                "/markets/quotes", {"symbols": option_symbol, "greeks": "true"}
            )
            q = data["quotes"]["quote"]
            if isinstance(q, list):
                q = q[0]
            return Quote(
                symbol=q["symbol"],
                last=float(q.get("last") or q.get("close") or 0),
                bid=float(q.get("bid") or 0),
                ask=float(q.get("ask") or 0),
                volume=int(q.get("volume") or 0),
            )
        except Exception as exc:
            logger.warning("get_option_quote(%s) failed: %s", option_symbol, exc)
            return None

    async def get_option_expirations(self, symbol: str) -> list[str]:
        """Return list of expiration date strings (YYYY-MM-DD)."""
        data = await self._data_get(
            "/markets/options/expirations", {"symbol": symbol, "includeAllRoots": "true"}
        )
        exps = data.get("expirations", {}).get("date", [])
        if isinstance(exps, str):
            exps = [exps]
        return exps

    async def get_options_chain(
        self, symbol: str, expiration: str, option_type: str = "both"
    ) -> list[OptionQuote]:
        """Fetch the full options chain for a given expiration."""
        data = await self._data_get(
            "/markets/options/chains",
            {"symbol": symbol, "expiration": expiration, "greeks": "true"},
        )
        raw_options = data.get("options", {}).get("option", []) or []
        if isinstance(raw_options, dict):
            raw_options = [raw_options]

        results: list[OptionQuote] = []
        for o in raw_options:
            otype = o.get("option_type", "").lower()
            if option_type != "both" and otype != option_type.lower():
                continue
            greeks = o.get("greeks") or {}
            results.append(
                OptionQuote(
                    symbol=o["symbol"],
                    underlying=o.get("underlying", symbol),
                    expiration_date=o.get("expiration_date", expiration),
                    option_type=otype,
                    strike=float(o.get("strike", 0)),
                    bid=float(o.get("bid") or 0),
                    ask=float(o.get("ask") or 0),
                    last=float(o.get("last") or o.get("close") or 0),
                    volume=int(o.get("volume") or 0),
                    open_interest=int(o.get("open_interest") or 0),
                    delta=float(greeks.get("delta") or 0) if greeks else None,
                    gamma=float(greeks.get("gamma") or 0) if greeks else None,
                    theta=float(greeks.get("theta") or 0) if greeks else None,
                    iv=float(greeks.get("mid_iv") or 0) if greeks else None,
                )
            )
        return results

    async def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "15min",
        start: Optional[str] = None,
        end: Optional[str] = None,
        lookback_days: int = 1,
    ) -> list[Bar]:
        """
        Fetch intraday OHLCV bars from the production API.

        interval      : '1min' | '5min' | '15min'
        start / end   : 'YYYY-MM-DD HH:MM' overrides (ignores lookback_days)
        lookback_days : how many *trading* days of history to request.
                        Pass >1 for trend indicators (EMA needs enough bars).
                        1-min VWAP bars should use lookback_days=1 (today only).
                        Add 4 extra calendar days as a weekend/holiday buffer.
        """
        today = date.today()
        if start is None:
            from datetime import timedelta
            start_date = today - timedelta(days=lookback_days + 4)
            start = start_date.strftime("%Y-%m-%d") + " 09:30"
        if end is None:
            end = today.strftime("%Y-%m-%d") + " 16:00"
        params: dict = {
            "symbol": symbol,
            "interval": interval,
            "start": start,
            "end": end,
            "session_filter": "open",
        }
        # Skip known-bad symbols without hitting the API again
        if symbol in self._bad_symbol_cache:
            return []

        try:
            data = await self._data_get("/markets/timesales", params)
            series = data.get("series", {})
            if not series:
                return []
            raw = series.get("data", [])
            if isinstance(raw, dict):
                raw = [raw]
            bars: list[Bar] = []
            for r in raw:
                bars.append(
                    Bar(
                        time=datetime.fromisoformat(r["time"]),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=int(r["volume"]),
                    )
                )
            return bars
        except httpx.TimeoutException:
            # ReadTimeout / ConnectTimeout from a slow Tradier response.
            # This is expected during heavy market hours — log a clean one-liner
            # at WARNING (no traceback) and let the next scan cycle retry.
            logger.warning("get_intraday_bars(%s): Tradier timeout — skipping this cycle", symbol)
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                # 400 = symbol invalid or delisted. Log once as a warning (no
                # traceback) then cache it so subsequent scans are silent.
                logger.warning(
                    "get_intraday_bars(%s): Tradier returned 400 — symbol is "
                    "invalid or delisted. Skipping for this session. "
                    "Remove it from your watchlist to silence this warning.",
                    symbol,
                )
                self._bad_symbol_cache.add(symbol)
            else:
                logger.error(
                    "get_intraday_bars(%s) failed: %s", symbol, exc, exc_info=True
                )
            return []
        except Exception as exc:
            logger.error(
                "get_intraday_bars(%s) failed: %s", symbol, exc, exc_info=True
            )
            return []

    # Symbols that returned 400 from /markets/timesales — invalid or delisted.
    # Cached for the lifetime of the process so we log only once and then
    # silently skip them on every subsequent scan tick.
    _bad_symbol_cache: set[str] = set()

    def get_atm_iv(
        self,
        chain: list[OptionQuote],
        direction: str,
        current_price: float,
    ) -> Optional[float]:
        """
        Return the mid-IV of the ATM option for the given direction.
        Finds the contract whose strike is closest to current_price.
        Returns None if chain is empty or IV data is unavailable.
        """
        side_chain = [o for o in chain if o.option_type == direction.lower()]
        if not side_chain:
            return None
        atm = min(side_chain, key=lambda o: abs(o.strike - current_price))
        return atm.iv if (atm.iv is not None and atm.iv > 0) else None

    # ------------------------------------------------------------------
    # Account / positions  (sandbox API)
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Return all current positions in the sandbox account."""
        data = await self._order_get(f"/accounts/{self._account_id}/positions")
        raw = data.get("positions", {})
        if not raw or raw == "null":
            return []
        positions = raw.get("position", [])
        if isinstance(positions, dict):
            positions = [positions]
        return [
            Position(
                symbol=p["symbol"],
                quantity=int(p["quantity"]),
                cost_basis=float(p["cost_basis"]),
                date_acquired=p.get("date_acquired"),
            )
            for p in positions
        ]

    async def get_account_balances(self) -> AccountBalance:
        """Return sandbox account cash/equity balances."""
        data = await self._order_get(f"/accounts/{self._account_id}/balances")
        b = data["balances"]
        return AccountBalance(
            account_value=float(b.get("total_equity") or b.get("net_value") or 0),
            cash=float(b.get("total_cash") or b.get("cash") or 0),
            buying_power=float(b.get("buying_power") or 0),
            option_buying_power=float(b.get("option_buying_power") or b.get("buying_power") or 0),
        )

    # ------------------------------------------------------------------
    # Order management  (sandbox API)
    # ------------------------------------------------------------------

    async def place_option_order(
        self,
        option_symbol: str,
        side: str,          # "buy_to_open" | "sell_to_close" | "buy_to_close" | "sell_to_open"
        quantity: int,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        duration: str = "day",
        stop_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Place a single-leg option order.

        order_type 'stop' requires stop_price — a resting broker-side stop
        order that triggers a market sell when the option trades at/below it.
        """
        payload: dict = {
            "class": "option",
            "symbol": option_symbol.split()[0] if " " in option_symbol else option_symbol[:6],
            "option_symbol": option_symbol,
            "side": side,
            "quantity": str(quantity),
            "type": order_type,
            "duration": duration,
        }
        if order_type == "limit" and limit_price is not None:
            payload["price"] = str(round(limit_price, 2))
        if order_type in ("stop", "stop_limit") and stop_price is not None:
            payload["stop"] = str(round(stop_price, 2))
            if order_type == "stop_limit" and limit_price is not None:
                payload["price"] = str(round(limit_price, 2))

        data = await self._order_post(f"/accounts/{self._account_id}/orders", payload)
        order = data.get("order", {})
        return OrderResult(
            order_id=str(order.get("id", "")),
            status=order.get("status", "unknown"),
            raw=data,
        )

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending sandbox order."""
        return await self._order_delete(f"/accounts/{self._account_id}/orders/{order_id}")

    async def modify_order(
        self,
        order_id: str,
        order_type: Optional[str] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> dict:
        """
        Modify a pending order (e.g. raise a resting stop order's trigger price).
        Only the supplied fields are changed.
        """
        payload: dict = {}
        if order_type is not None:
            payload["type"] = order_type
        if limit_price is not None:
            payload["price"] = str(round(limit_price, 2))
        if stop_price is not None:
            payload["stop"] = str(round(stop_price, 2))
        return await self._order_put(
            f"/accounts/{self._account_id}/orders/{order_id}", payload
        )

    async def get_open_orders(self) -> list[dict]:
        """Return all orders in open/pending states for the account."""
        try:
            data = await self._order_get(f"/accounts/{self._account_id}/orders")
        except Exception as exc:
            logger.warning("get_open_orders failed: %s", exc)
            return []
        raw = data.get("orders", {})
        if not raw or raw == "null":
            return []
        orders = raw.get("order", [])
        if isinstance(orders, dict):
            orders = [orders]
        open_states = {"open", "pending", "partially_filled", "submitted", "received"}
        return [o for o in orders if (o.get("status") or "").lower() in open_states]

    async def get_order_status(self, order_id: str) -> dict:
        """Return raw order status from the sandbox."""
        data = await self._order_get(f"/accounts/{self._account_id}/orders/{order_id}")
        return data.get("order", data)

    async def get_fill_price(self, order_id: str) -> Optional[float]:
        """
        Return the avg_fill_price of a filled order, or None.

        IMPORTANT: Only returns a price when order status is "filled".
        Tradier sometimes sets avg_fill_price to a non-zero value on pending
        or submitted orders (e.g. after-hours market orders, where the field
        reflects the mark price at submission time rather than an actual
        exchange execution).  Trusting that value causes the DB to record a
        stale quote instead of the real fill — e.g. INTC after-hours recorded
        $5.25 (mark at close time) instead of the actual $3.65 fill the next
        morning.  Requiring status == "filled" prevents this entirely.
        """
        if not order_id:
            return None
        try:
            status = await self.get_order_status(order_id)
            # Only trust avg_fill_price once the exchange has confirmed the fill.
            if (status.get("status") or "").lower() != "filled":
                return None
            fp = float(status.get("avg_fill_price") or 0)
            return round(fp, 2) if fp > 0 else None
        except Exception as exc:
            logger.debug("get_fill_price(%s) failed: %s", order_id, exc)
            return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[TradierClient] = None


def get_tradier_client() -> TradierClient:
    global _client
    if _client is None:
        _client = TradierClient()
    return _client


async def close_tradier_client() -> None:
    """Close the shared HTTP connections on app shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
