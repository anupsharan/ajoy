"""MoomooClient — the ONLY module that touches the Moomoo/Futu SDK.

Everything broker-specific lives behind this adapter: SDK import, OpenD
connection, subscriptions, push handlers (converted to plain dataclasses
and enqueued — callbacks never block), order entry, reconciliation queries
and the startup capability probe.

Fail-safe rules
---------------
* SDK missing / OpenD unreachable → `available` is False; the engine logs
  the capability report and idles instead of crashing the app.
* TRD_ENV=REAL without a successful trade unlock → signal-only mode
  (orders are refused locally, loudly).
* Native bracket/OCO is probed, NOT assumed: the moomoo OpenAPI exposes no
  server-side OCO/bracket for US equities, so `supports_bracket` is False
  unless a future SDK exposes one — the OMS runs software OCO either way.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import settings
from app.services.s3.types import (
    Bar,
    BookLevel,
    BookSnapshot,
    Event,
    FillEvent,
    OrderUpdate,
    Tick,
    now,
)

logger = logging.getLogger(__name__)

# ── Guarded SDK import ───────────────────────────────────────────
_sdk: Any = None
_sdk_error: str | None = None
try:  # official package name
    import moomoo as _sdk  # type: ignore
except ImportError:
    try:  # legacy name
        import futu as _sdk  # type: ignore
    except ImportError as exc:
        _sdk_error = f"moomoo/futu SDK not installed: {exc}"


@dataclass
class Capabilities:
    sdk_available: bool = False
    sdk_name: str = ""
    quote_connected: bool = False
    trade_connected: bool = False
    trade_unlocked: bool = False
    lv2_order_book: bool = False
    ticker_push: bool = False
    supports_modify_order: bool = False
    supports_bracket: bool = False        # no native OCO/bracket on US equities
    notes: list[str] = field(default_factory=list)

    def report(self) -> str:
        return (
            f"S3 capabilities: sdk={self.sdk_name or 'MISSING'} "
            f"quote={'OK' if self.quote_connected else 'NO'} "
            f"trade={'OK' if self.trade_connected else 'NO'} "
            f"unlock={'OK' if self.trade_unlocked else 'NO'} "
            f"LV2={'OK' if self.lv2_order_book else 'NO'} "
            f"ticker={'OK' if self.ticker_push else 'NO'} "
            f"modify_order={self.supports_modify_order} "
            f"bracket/OCO={self.supports_bracket} "
            f"notes={'; '.join(self.notes) or '-'}"
        )


def to_moomoo_code(ticker: str) -> str:
    return ticker if ticker.startswith("US.") else f"US.{ticker}"


def from_moomoo_code(code: str) -> str:
    return code.split(".", 1)[1] if "." in code else code


class MoomooClient:
    """Thread-safe adapter.  All SDK callbacks enqueue Events onto
    `event_queue`; the engine thread is the sole consumer."""

    def __init__(self, event_queue: "queue.Queue[Event]", data_only: bool = False) -> None:
        self.q = event_queue
        self.data_only = data_only  # True → quotes only; execution lives elsewhere (Tradier)
        self.caps = Capabilities()
        self.connected = threading.Event()
        self.dropped_events = 0
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._trd_env: Any = None
        self._subscribed: list[str] = []
        self._lock = threading.Lock()
        self._on_disconnect: Callable[[], None] | None = None

    # ── Enqueue (never block an SDK callback) ────────────────────
    def _put(self, kind: str, payload: object) -> None:
        try:
            self.q.put_nowait(Event(kind=kind, payload=payload))
        except queue.Full:
            self.dropped_events += 1
            try:  # drop-oldest so fresh data wins
                self.q.get_nowait()
                self.q.put_nowait(Event(kind=kind, payload=payload))
            except queue.Empty:
                pass

    # ── Connection ───────────────────────────────────────────────
    def connect(self) -> bool:
        if _sdk is None:
            self.caps.notes.append(_sdk_error or "SDK missing")
            logger.error("[S3] %s", self.caps.report())
            return False
        self.caps.sdk_available = True
        self.caps.sdk_name = _sdk.__name__

        try:
            self._quote_ctx = _sdk.OpenQuoteContext(
                host=settings.s3_opend_host, port=settings.s3_opend_port
            )
        except Exception as exc:  # OpenD down
            self.caps.notes.append(f"OpenQuoteContext failed: {exc}")
            logger.error("[S3] %s", self.caps.report())
            return False
        self.caps.quote_connected = True

        if self.data_only:
            # Execution is routed to another broker (S3_BROKER=tradier):
            # never open a trade context, never unlock — quotes only.
            self.caps.notes.append("data-only mode — execution via Tradier")
            self.caps.ticker_push = hasattr(_sdk.SubType, "TICKER")
            self.caps.lv2_order_book = hasattr(_sdk.SubType, "ORDER_BOOK")
            self._install_handlers()
            self.connected.set()
            logger.info("[S3] %s", self.caps.report())
            return True

        # US-equities trade context (SDK-supported class per docs).
        try:
            kwargs: dict[str, Any] = {
                "host": settings.s3_opend_host, "port": settings.s3_opend_port,
            }
            if hasattr(_sdk, "TrdMarket"):
                kwargs["filter_trdmarket"] = _sdk.TrdMarket.US
            if hasattr(_sdk, "SecurityFirm"):
                kwargs["security_firm"] = _sdk.SecurityFirm.FUTUINC
            self._trade_ctx = _sdk.OpenSecTradeContext(**kwargs)
            self.caps.trade_connected = True
        except Exception as exc:
            self.caps.notes.append(f"OpenSecTradeContext failed: {exc}")
            logger.error("[S3] trade context unavailable: %s", exc)

        env_name = settings.s3_trd_env.upper()
        self._trd_env = getattr(_sdk.TrdEnv, env_name, _sdk.TrdEnv.SIMULATE)

        # Trade unlock (REAL only).
        if self._trade_ctx is not None:
            if env_name == "REAL":
                if settings.s3_trade_pwd:
                    try:
                        ret, data = self._trade_ctx.unlock_trade(settings.s3_trade_pwd)
                        self.caps.trade_unlocked = ret == _sdk.RET_OK
                        if not self.caps.trade_unlocked:
                            self.caps.notes.append(f"unlock_trade rejected: {data}")
                    except Exception as exc:
                        self.caps.notes.append(f"unlock_trade error: {exc}")
                else:
                    self.caps.notes.append("REAL env but S3_TRADE_PWD empty → signal-only")
            else:
                self.caps.trade_unlocked = True  # SIMULATE needs no unlock

        # Capability probe — never invent unsupported features.
        self.caps.supports_modify_order = hasattr(self._trade_ctx, "modify_order")
        self.caps.supports_bracket = False  # no native OCO/bracket for US equities
        self.caps.ticker_push = hasattr(_sdk.SubType, "TICKER")
        self.caps.lv2_order_book = hasattr(_sdk.SubType, "ORDER_BOOK")

        self._install_handlers()
        self.connected.set()
        logger.info("[S3] %s", self.caps.report())
        return True

    def close(self) -> None:
        self.connected.clear()
        for ctx in (self._quote_ctx, self._trade_ctx):
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass
        self._quote_ctx = self._trade_ctx = None

    @property
    def can_trade(self) -> bool:
        return (
            self.caps.trade_connected
            and self.caps.trade_unlocked
            and self.connected.is_set()
        )

    # ── Push handlers ────────────────────────────────────────────
    def _install_handlers(self) -> None:
        client = self

        class _TickerHandler(_sdk.TickerHandlerBase):
            def on_recv_rsp(self, rsp_pb):  # noqa: N802 (SDK signature)
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != _sdk.RET_OK:
                    return ret, data
                recv = now()
                for row in data.to_dict("records"):
                    client._put("tick", Tick(
                        symbol=from_moomoo_code(row.get("code", "")),
                        ts=recv,  # SDK time is a string in exchange tz; recv is authoritative
                        recv_ts=recv,
                        price=float(row.get("price", 0.0)),
                        volume=int(row.get("volume", 0)),
                        seq=int(row.get("sequence", 0) or 0),
                    ))
                return _sdk.RET_OK, data

        class _BookHandler(_sdk.OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):  # noqa: N802
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != _sdk.RET_OK:
                    return ret, data
                recv = now()
                bids = [BookLevel(price=float(b[0]), size=int(b[1]),
                                  orders=int(b[2]) if len(b) > 2 else 0)
                        for b in data.get("Bid", [])]
                asks = [BookLevel(price=float(a[0]), size=int(a[1]),
                                  orders=int(a[2]) if len(a) > 2 else 0)
                        for a in data.get("Ask", [])]
                client._put("book", BookSnapshot(
                    symbol=from_moomoo_code(data.get("code", "")),
                    ts=recv, recv_ts=recv, bids=bids, asks=asks,
                    seq=int(data.get("svr_recv_time_bid", 0) or 0),
                ))
                return _sdk.RET_OK, data

        class _KlineHandler(_sdk.CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_pb):  # noqa: N802
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != _sdk.RET_OK:
                    return ret, data
                recv = now()
                for row in data.to_dict("records"):
                    k_type = str(row.get("k_type", ""))
                    interval = "1m" if "1M" in k_type.upper() else \
                               "5m" if "5M" in k_type.upper() else None
                    if interval is None:
                        continue
                    client._put("bar", Bar(
                        symbol=from_moomoo_code(row.get("code", "")),
                        interval=interval,
                        start_ts=recv,
                        open=float(row.get("open", 0)),
                        high=float(row.get("high", 0)),
                        low=float(row.get("low", 0)),
                        close=float(row.get("close", 0)),
                        volume=int(row.get("volume", 0)),
                        turnover=float(row.get("turnover", 0.0)),
                        complete=False,
                    ))
                return _sdk.RET_OK, data

        self._quote_ctx.set_handler(_TickerHandler())
        self._quote_ctx.set_handler(_BookHandler())
        self._quote_ctx.set_handler(_KlineHandler())

        if self._trade_ctx is None:
            return

        class _OrderHandler(_sdk.TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb):  # noqa: N802
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != _sdk.RET_OK:
                    return ret, data
                for row in data.to_dict("records"):
                    client._put("order", client._row_to_order(row))
                return _sdk.RET_OK, data

        class _DealHandler(_sdk.TradeDealHandlerBase):
            def on_recv_rsp(self, rsp_pb):  # noqa: N802
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != _sdk.RET_OK:
                    return ret, data
                for row in data.to_dict("records"):
                    client._put("fill", client._row_to_fill(row))
                return _sdk.RET_OK, data

        self._trade_ctx.set_handler(_OrderHandler())
        self._trade_ctx.set_handler(_DealHandler())

    @staticmethod
    def _row_to_order(row: dict) -> OrderUpdate:
        return OrderUpdate(
            order_id=str(row.get("order_id", "")),
            symbol=from_moomoo_code(str(row.get("code", ""))),
            ts=now(),
            status=str(row.get("order_status", "")).upper(),
            side="BUY" if "BUY" in str(row.get("trd_side", "")).upper() else "SELL",
            price=float(row.get("price", 0) or 0),
            qty=int(float(row.get("qty", 0) or 0)),
            filled_qty=int(float(row.get("dealt_qty", 0) or 0)),
            filled_avg_price=float(row.get("dealt_avg_price", 0) or 0),
        )

    @staticmethod
    def _row_to_fill(row: dict) -> FillEvent:
        return FillEvent(
            order_id=str(row.get("order_id", "")),
            deal_id=str(row.get("deal_id", "")),
            symbol=from_moomoo_code(str(row.get("code", ""))),
            ts=now(),
            side="BUY" if "BUY" in str(row.get("trd_side", "")).upper() else "SELL",
            price=float(row.get("price", 0) or 0),
            qty=int(float(row.get("qty", 0) or 0)),
        )

    # ── Subscriptions ────────────────────────────────────────────
    def subscribe(self, tickers: list[str]) -> bool:
        codes = [to_moomoo_code(t) for t in tickers]
        subs = [_sdk.SubType.ORDER_BOOK, _sdk.SubType.TICKER,
                _sdk.SubType.K_1M, _sdk.SubType.K_5M]
        ret, data = self._quote_ctx.subscribe(codes, subs, subscribe_push=True)
        if ret != _sdk.RET_OK:
            logger.error("[S3] subscribe failed: %s", data)
            return False
        self._subscribed = codes
        logger.info("[S3] subscribed %s (%s)", codes,
                    [str(s) for s in subs])
        return True

    def resubscribe(self) -> bool:
        return self.subscribe([from_moomoo_code(c) for c in self._subscribed]) \
            if self._subscribed else True

    # ── Order entry (Broker protocol for the OMS) ────────────────
    def place_limit(self, symbol: str, side: str, qty: int, price: float) -> str | None:
        if not self.can_trade:
            logger.error("[S3] order refused — trading unavailable (%s)",
                         self.caps.report())
            return None
        with self._lock:
            try:
                trd_side = _sdk.TrdSide.BUY if side == "BUY" else _sdk.TrdSide.SELL
                ret, data = self._trade_ctx.place_order(
                    price=round(price, 2), qty=qty, code=to_moomoo_code(symbol),
                    trd_side=trd_side, order_type=_sdk.OrderType.NORMAL,
                    trd_env=self._trd_env,
                )
                if ret != _sdk.RET_OK:
                    logger.error("[S3] place_order rejected: %s", data)
                    return None
                return str(data["order_id"].iloc[0])
            except Exception as exc:  # noqa: BLE001
                logger.exception("[S3] place_order error: %s", exc)
                return None

    def cancel_order(self, order_id: str) -> bool:
        if not self.can_trade or not self.caps.supports_modify_order:
            return False
        with self._lock:
            try:
                ret, data = self._trade_ctx.modify_order(
                    _sdk.ModifyOrderOp.CANCEL, order_id, 0, 0,
                    trd_env=self._trd_env,
                )
                if ret != _sdk.RET_OK:
                    logger.warning("[S3] cancel %s rejected: %s", order_id, data)
                    return False
                return True
            except Exception as exc:  # noqa: BLE001
                logger.exception("[S3] cancel error: %s", exc)
                return False

    def modify_order_supported(self) -> bool:
        return self.caps.supports_modify_order

    # ── Reconciliation queries ───────────────────────────────────
    def fetch_open_orders(self) -> list[OrderUpdate]:
        try:
            ret, data = self._trade_ctx.order_list_query(trd_env=self._trd_env)
            if ret != _sdk.RET_OK:
                return []
            return [self._row_to_order(r) for r in data.to_dict("records")]
        except Exception:  # noqa: BLE001
            return []

    def fetch_fills(self) -> list[FillEvent]:
        try:
            ret, data = self._trade_ctx.deal_list_query(trd_env=self._trd_env)
            if ret != _sdk.RET_OK:
                return []
            return [self._row_to_fill(r) for r in data.to_dict("records")]
        except Exception:  # noqa: BLE001
            return []

    def fetch_positions(self) -> dict[str, int]:
        try:
            ret, data = self._trade_ctx.position_list_query(trd_env=self._trd_env)
            if ret != _sdk.RET_OK:
                return {}
            out: dict[str, int] = {}
            for r in data.to_dict("records"):
                out[from_moomoo_code(str(r.get("code", "")))] = int(float(r.get("qty", 0)))
            return out
        except Exception:  # noqa: BLE001
            return {}

    def fetch_buying_power(self) -> float:
        try:
            ret, data = self._trade_ctx.accinfo_query(trd_env=self._trd_env)
            if ret != _sdk.RET_OK or data.empty:
                return 0.0
            for col in ("us_power", "power", "cash"):
                if col in data.columns:
                    return float(data[col].iloc[0] or 0.0)
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    # ── Health ───────────────────────────────────────────────────
    def ping(self) -> bool:
        """Cheap liveness probe against OpenD."""
        try:
            ret, _ = self._quote_ctx.get_global_state()
            alive = ret == _sdk.RET_OK
        except Exception:  # noqa: BLE001
            alive = False
        if not alive and self.connected.is_set():
            self.connected.clear()
            self._put("control", {"type": "DISCONNECTED", "ts": time.time()})
        return alive
