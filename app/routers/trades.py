import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Direction, ExitReason, Trade, TradeStatus
from app.schemas import CloseTradeRequest, TradeOut, TradeWithLivePnL
from app.services.tradier import get_tradier_client
from app.services.strategy import (
    calculate_ema, completed_bars, compute_trade_levels, ema_direction, session_bars,
)

router = APIRouter(prefix="/api/trades", tags=["trades"])


import logging as _logging
_enrich_logger = _logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# S2 EMA-based trend + thesis helper (cache-free, display only)
# ---------------------------------------------------------------------------

def _s2_ema_thesis(
    bars_5m: list,
    direction: str,
    ema_fast: int = 9,
    ema_slow: int = 21,
) -> tuple[str, str]:
    """
    Derive TREND and THESIS for an S2 (EMA Pullback) trade from 5-min bars.

    TREND:
      "bullish"  — EMA9 > EMA21 and EMA9 slope rising
      "bearish"  — EMA9 < EMA21 and EMA9 slope falling
      "neutral"  — anything else (crossing, flat, insufficient data)

    THESIS — "is the S2 setup still alive?":
      "intact"   — EMA9/21 still crossed in trade direction AND gap stable/growing
      "at_risk"  — EMA9/21 still crossed in direction BUT gap narrowing (converging)
      "broken"   — EMA9/21 have crossed to the OPPOSITE direction

    Deliberately does NOT touch the scheduler's _trend_cache so display
    reads and scanner writes remain independent.
    """
    bars = completed_bars(bars_5m, interval_minutes=5)
    if len(bars) < ema_slow + 2:
        return "neutral", "intact"   # not enough history — don't alarm

    closes = [b.close for b in bars]

    ema9_vals  = calculate_ema(closes, ema_fast)
    ema21_vals = calculate_ema(closes, ema_slow)

    valid9  = [v for v in ema9_vals  if v == v]   # strip NaN
    valid21 = [v for v in ema21_vals if v == v]

    if len(valid9) < 2 or len(valid21) < 2:
        return "neutral", "intact"

    ema9_prev,  ema9_now  = valid9[-2],  valid9[-1]
    ema21_prev, ema21_now = valid21[-2], valid21[-1]

    # ── TREND ─────────────────────────────────────────────────────────────────
    if ema9_now > ema21_now and ema9_now > ema9_prev:
        trend = "bullish"
    elif ema9_now < ema21_now and ema9_now < ema9_prev:
        trend = "bearish"
    else:
        trend = "neutral"

    # ── THESIS ────────────────────────────────────────────────────────────────
    # Mirrors the S2 entry condition: the entry requires EMA9/21 cross AND slopes
    # both aligned.  Thesis uses the same slope check so "intact" == entry still valid.
    #
    # gap_now > 0 → EMA9 above EMA21 (CALL-aligned); < 0 → EMA9 below (PUT-aligned)
    gap_now = ema9_now - ema21_now

    ema9_slope_up   = ema9_now  > ema9_prev
    ema21_slope_up  = ema21_now > ema21_prev
    ema9_slope_down  = ema9_now  < ema9_prev
    ema21_slope_down = ema21_now < ema21_prev

    if direction == "CALL":
        if gap_now <= 0:
            thesis = "broken"    # EMA cross flipped — trade direction gone
        elif ema9_slope_up or ema21_slope_up:
            thesis = "intact"    # cross alive, at least one EMA still rising
        else:
            thesis = "at_risk"   # cross alive but both EMAs losing upward momentum
    else:  # PUT
        if gap_now >= 0:
            thesis = "broken"    # EMA cross flipped — trade direction gone
        elif ema9_slope_down or ema21_slope_down:
            thesis = "intact"    # cross alive, at least one EMA still falling
        else:
            thesis = "at_risk"   # cross alive but both EMAs losing downward momentum

    return trend, thesis


async def _enrich_with_live_pnl(trade: Trade) -> TradeWithLivePnL:
    """
    Fetch current option price, underlying quote, and 15-min trend from Tradier.

    Each of the three enrichment sections (option P&L, underlying price, trend)
    is wrapped in its own try/except so that a failure in one section does NOT
    prevent the others from running.  All errors are logged so they are visible
    in the server logs instead of being silently swallowed.
    """
    from app.config import settings as _cfg

    base = TradeOut.model_validate(trade)
    enriched = TradeWithLivePnL(**base.model_dump())
    client = get_tradier_client()

    # ── Section 1: option quote → current_price, live_pnl, live_pnl_pct ─────
    try:
        opt_quote = await client.get_option_quote(trade.option_symbol)
        if opt_quote:
            bid, ask, last = opt_quote.bid, opt_quote.ask, opt_quote.last
            if bid and ask:
                current = (bid + ask) / 2
            elif last:
                current = last
            else:
                current = None   # sandbox sometimes returns all-zero quotes

            if current is not None:
                enriched.current_price = round(current, 2)
                qty = trade.remaining_qty or trade.quantity
                # S3 rows are STOCKS (option_symbol holds the ticker, so the
                # quote above is the equity quote): per-share P&L, no ×100
                # options contract multiplier.
                mult = 1 if trade.strategy_name == "S3" else 100
                unrealized = (current - trade.entry_price) * qty * mult
                realized = trade.pnl or 0.0
                enriched.live_pnl = round(realized + unrealized, 2)
                if trade.entry_price:
                    original_cost = trade.entry_price * trade.quantity * mult
                    if original_cost:
                        enriched.live_pnl_pct = round(
                            enriched.live_pnl / original_cost * 100, 2
                        )
    except Exception as exc:
        _enrich_logger.warning(
            "live P&L enrichment failed for %s (%s): %s",
            trade.symbol, trade.option_symbol, exc, exc_info=True,
        )

    # ── Section 2: underlying quote → underlying_price ────────────────────
    underlying_last: Optional[float] = None
    try:
        underlying_quote = await client.get_quote(trade.symbol)
        if underlying_quote and underlying_quote.last:
            enriched.underlying_price = underlying_quote.last
            underlying_last = underlying_quote.last
    except Exception as exc:
        _enrich_logger.warning(
            "underlying quote failed for %s: %s", trade.symbol, exc, exc_info=True,
        )

    # ── Pre-compute strategy identity and direction once ─────────────────
    strategy_name = (trade.strategy_name or "").lower()
    is_s2 = strategy_name == "ema_cross"
    direction_val = (
        trade.direction.value
        if hasattr(trade.direction, "value")
        else str(trade.direction)
    )

    # ── Section 3: stock trend ────────────────────────────────────────────
    # S1 (vwap_pullback): 15-min EMA direction — same timeframe the entry uses.
    # S2 (ema_cross):     5-min EMA9/21 relationship — the timeframe S2 actually
    #                     trades on.  Signals pre-computed here so Section 4 can
    #                     reuse them without a second bar fetch.
    _s2_signals: tuple[str, str] | None = None   # (trend, thesis) cached for Section 4
    _bars_5m: list = []

    try:
        if is_s2:
            _bars_5m = await client.get_intraday_bars(
                trade.symbol, interval="5min", lookback_days=3,
            )
            if _bars_5m:
                _s2_signals = _s2_ema_thesis(
                    _bars_5m, direction_val,
                    ema_fast=_cfg.s2_ema_fast,
                    ema_slow=_cfg.s2_ema_slow,
                )
                enriched.trend = _s2_signals[0]
        else:
            bars_15m = await client.get_intraday_bars(
                trade.symbol, interval="15min",
                lookback_days=_cfg.trend_lookback_days,
            )
            if bars_15m:
                enriched.trend = ema_direction(
                    bars_15m,
                    period=_cfg.ema_period,
                    live_price=underlying_last,
                )
    except Exception as exc:
        _enrich_logger.warning(
            "trend enrichment failed for %s: %s", trade.symbol, exc, exc_info=True,
        )

    # ── Section 4: thesis status ──────────────────────────────────────────
    #
    # S1 (VWAP Pullback) — VWAP-based:
    #   intact  = stock on correct side of VWAP (any margin → entry assumption holds)
    #   at_risk = stock crossed VWAP but within the exit band (0.3%) — bot hasn't
    #             fired VWAP_BREAK yet but it could at any tick → watch closely
    #   broken  = stock is beyond the exit band on the wrong side — bot exit should
    #             already be firing → consider manual close
    #
    # S2 (EMA Pullback) — EMA-based (VWAP is irrelevant for this strategy):
    #   intact  = EMA9/21 still crossed in trade direction AND gap stable or widening
    #   at_risk = EMA9/21 still crossed in direction BUT gap narrowing (trend weakening)
    #   broken  = EMA9/21 have crossed to the OPPOSITE direction — S2 exit should fire
    try:
        if is_s2:
            # Thesis already computed above — apply it directly.
            if _s2_signals is not None:
                enriched.thesis_status = _s2_signals[1]
            # vwap_current intentionally left None for S2 (not relevant)
        else:
            # S1: VWAP thesis with exit-band-aligned thresholds
            bars_1m = await client.get_intraday_bars(
                trade.symbol, interval="1min", lookback_days=1,
            )
            vwap_now: Optional[float] = None
            if bars_1m:
                # Session bars only — the raw fetch spans multiple days and a
                # blended VWAP misstates the thesis (NFLX #134 showed INTACT
                # on a PUT while trading ABOVE its true session VWAP).
                _sess = session_bars(bars_1m)
                total_vol = sum(b.volume for b in _sess if b.volume and b.volume > 0)
                if total_vol > 0:
                    tp_vol = sum(
                        (b.high + b.low + b.close) / 3.0 * b.volume
                        for b in _sess if b.volume and b.volume > 0
                    )
                    vwap_now = round(tp_vol / total_vol, 2)

            # Fall back to VWAP stored at entry time if live bars unavailable
            if not vwap_now and trade.vwap_at_entry:
                vwap_now = trade.vwap_at_entry

            enriched.vwap_current = vwap_now

            if vwap_now and underlying_last:
                diff_pct = (underlying_last - vwap_now) / vwap_now  # + = above, − = below
                exit_band = _cfg.vwap_exit_band_pct   # 0.003 = 0.3%

                if direction_val == "CALL":
                    if diff_pct >= 0:
                        enriched.thesis_status = "intact"    # above VWAP — all good
                    elif diff_pct >= -exit_band:
                        enriched.thesis_status = "at_risk"   # crossed VWAP, within exit band
                    else:
                        enriched.thesis_status = "broken"    # past exit band — bot exit imminent
                else:  # PUT
                    if diff_pct <= 0:
                        enriched.thesis_status = "intact"    # below VWAP — all good
                    elif diff_pct <= exit_band:
                        enriched.thesis_status = "at_risk"   # crossed VWAP, within exit band
                    else:
                        enriched.thesis_status = "broken"    # past exit band — bot exit imminent
    except Exception as exc:
        _enrich_logger.warning(
            "thesis enrichment failed for %s: %s", trade.symbol, exc, exc_info=True,
        )

    return enriched


# ---------------------------------------------------------------------------
# Live trades (open positions)
# ---------------------------------------------------------------------------

@router.get("/live", response_model=list[TradeWithLivePnL])
async def get_live_trades(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Trade).where(Trade.status == TradeStatus.OPEN).order_by(Trade.entry_time.desc())
    )
    trades = result.scalars().all()
    enriched = []
    for t in trades:
        enriched.append(await _enrich_with_live_pnl(t))
    return enriched


# ---------------------------------------------------------------------------
# Manual close
# ---------------------------------------------------------------------------

@router.post("/close", response_model=TradeOut)
async def manual_close_trade(
    payload: CloseTradeRequest, db: AsyncSession = Depends(get_db)
):
    trade = await db.get(Trade, payload.trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != TradeStatus.OPEN:
        raise HTTPException(status_code=400, detail="Trade is already closed")
    if trade.strategy_name == "S3":
        # S3 positions are stocks owned by the S3 engine's own OMS — the
        # option sell path below would mis-route the order and desync the
        # engine's state.  Flatten via the S3 kill switch (Settings → S3),
        # or use /mark-closed for a bookkeeping-only correction.
        raise HTTPException(
            status_code=400,
            detail="S3 trades are managed by the S3 engine. Use the S3 kill "
                   "switch to flatten, or /mark-closed for bookkeeping.",
        )

    client = get_tradier_client()
    qty = trade.remaining_qty or trade.quantity
    exit_price: Optional[float] = None

    # ── Step 0: cancel the broker-side resting stop first ─────────────────────
    # The resting sell order reserves the contracts; the manual sell would be
    # rejected (or double-sell) while it is live.  If the stop filled in the
    # race, the position is already flat — record that close instead.
    if trade.stop_order_id:
        try:
            await client.cancel_order(trade.stop_order_id)
        except Exception:
            pass
        try:
            st = await client.get_order_status(trade.stop_order_id)
            st_str = (st.get("status") or "").lower()
        except Exception:
            st_str = "unknown"
        if st_str == "filled":
            fill = await client.get_fill_price(trade.stop_order_id)
            exit_price        = round(fill or trade.stop_price or trade.entry_price, 2)
            trade.status      = TradeStatus.CLOSED
            trade.exit_price  = exit_price
            trade.exit_time   = datetime.now(tz=timezone.utc)
            trade.exit_reason = ExitReason.STOP
            trade.pnl         = round(
                (trade.pnl or 0) + (exit_price - trade.entry_price) * qty * 100, 2
            )
            await db.commit()
            await db.refresh(trade)
            return trade
        if st_str not in ("canceled", "cancelled", "expired", "rejected"):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Broker stop order {trade.stop_order_id} is still {st_str.upper()} "
                    "after cancel attempt — retry in a few seconds."
                ),
            )
        trade.stop_order_id = None
        await db.commit()

    # ── Step 1: place the sell order ──────────────────────────────────────────
    try:
        order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=qty,
            order_type="market",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tradier sell_to_close API call failed: {exc}",
        )

    # ── Step 2: confirm the sell order actually filled ────────────────────────
    # Always fetch both fill price and order status.  Only proceed to close the
    # DB record when the order is confirmed as filled — otherwise a pending or
    # partially-filled order would leave a real live Tradier position while Ajoy
    # believes it is flat.
    exit_price = await client.get_fill_price(order.order_id)

    try:
        order_status_data = await client.get_order_status(order.order_id)
        order_status_str  = (order_status_data.get("status") or "").lower()
    except Exception:
        order_status_str  = "unknown"

    if order_status_str in ("rejected", "canceled", "cancelled"):
        # Rejected sells usually mean the position was already closed outside
        # Ajoy (manual Tradier sell / disaster-stop fill).  Verify and
        # reconcile with the actual external fill instead of erroring out —
        # this is what made the Close button appear "broken" on SOFI #137.
        from app.services.scheduler import _reconcile_external_close
        if await _reconcile_external_close(db, client, trade):
            await db.refresh(trade)
            return trade
        raise HTTPException(
            status_code=502,
            detail=(
                f"Sell order {order.order_id} was {order_status_str.upper()} by Tradier. "
                "Position is still open. Check Tradier for details."
            ),
        )

    if order_status_str != "filled" and not exit_price:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Sell order {order.order_id} status={order_status_str.upper()} — "
                "not yet confirmed as filled. Please retry in a few seconds."
            ),
        )

    # ── Step 3: fallback if still no fill price (sandbox fill → production quote → entry) ──

    if not exit_price:
        try:
            q = await client.get_option_quote(trade.option_symbol)
            if q:
                exit_price = round((q.bid + q.ask) / 2, 2) if q.bid and q.ask else q.last
        except Exception:
            pass
    if not exit_price:
        exit_price = trade.entry_price  # worst case: record at cost

    # ── Step 4: persist the close ─────────────────────────────────────────────
    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = round(exit_price, 2)
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = ExitReason.MANUAL
    realized_so_far   = trade.pnl or 0.0
    close_pnl         = (exit_price - trade.entry_price) * qty * 100
    trade.pnl         = round(realized_so_far + close_pnl, 2)

    await db.commit()
    await db.refresh(trade)
    return trade


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class MarkClosedRequest(BaseModel):
    exit_price: Optional[float] = None   # omit to use current market bid; 0 = use entry price


@router.post("/{trade_id}/mark-closed", response_model=TradeOut)
async def mark_trade_closed(
    trade_id: int,
    payload: MarkClosedRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark an open trade as CLOSED in the Ajoy DB **without** placing any
    Tradier order.  Use this when the position was already closed outside
    of Ajoy (manual close in Tradier UI, guardian script, broker action)
    and the bot DB has not caught up yet.

    exit_price — the price at which the position was actually closed.
                 Omit (or pass null) to fetch the current market bid.
                 Pass 0 to fall back to entry price (zero-P&L close).
    """
    trade = await db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != TradeStatus.OPEN:
        raise HTTPException(status_code=400, detail="Trade is already closed")

    client = get_tradier_client()

    # ── Cancel any resting broker orders first ────────────────────────────
    # The position is already flat at Tradier; an orphaned disaster stop or
    # TP limit would otherwise sit there as a zombie order (SOFI #137 left
    # stop order 136295991 resting after a manual close).
    for _oid_attr in ("stop_order_id", "tp_order_id"):
        _oid = getattr(trade, _oid_attr)
        if _oid:
            try:
                await client.cancel_order(_oid)
                _enrich_logger.info(
                    "[%s] mark-closed: cancelled resting broker order %s for trade %d",
                    trade.symbol, _oid, trade.id,
                )
            except Exception as _exc:
                _enrich_logger.warning(
                    "[%s] mark-closed: could not cancel broker order %s: %s — "
                    "check Tradier for zombie orders",
                    trade.symbol, _oid, _exc,
                )
            setattr(trade, _oid_attr, None)

    # Resolve exit price
    exit_price: Optional[float] = payload.exit_price

    if exit_price is None:
        # 1) The ACTUAL closing fill from the broker's order history — the
        #    position was closed outside Ajoy, so the truth lives at Tradier,
        #    not in the current quote.
        try:
            exit_price = await client.get_last_sell_fill(trade.option_symbol)
            if exit_price:
                _enrich_logger.info(
                    "[%s] mark-closed: using actual Tradier fill $%.2f for trade %d",
                    trade.symbol, exit_price, trade.id,
                )
        except Exception:
            exit_price = None
        # 2) Fallback: current bid — an ESTIMATE, not a fill.  Warn loudly so
        #    a mis-stated P&L is traceable (NFLX #134 was recorded +$30 from a
        #    bounced quote while the real fill was −$50).
        if exit_price is None:
            try:
                q = await client.get_option_quote(trade.option_symbol)
                if q and q.bid and q.bid > 0:
                    exit_price = round(q.bid, 2)
                elif q and q.last and q.last > 0:
                    exit_price = round(q.last, 2)
                if exit_price:
                    _enrich_logger.warning(
                        "[%s] mark-closed: no fill found in order history — "
                        "recording ESTIMATED exit $%.2f from live quote for trade %d. "
                        "Verify against Tradier order history.",
                        trade.symbol, exit_price, trade.id,
                    )
            except Exception:
                pass

    if not exit_price:
        exit_price = trade.entry_price   # last-resort: record at cost

    # Persist
    qty = trade.remaining_qty or trade.quantity
    realized_so_far = trade.pnl or 0.0
    close_pnl = (exit_price - trade.entry_price) * qty * 100

    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = round(exit_price, 2)
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = ExitReason.MANUAL
    trade.pnl         = round(realized_so_far + close_pnl, 2)
    trade.remaining_qty = 0

    await db.commit()
    await db.refresh(trade)
    return trade


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconcileResult(BaseModel):
    orphaned_in_tradier: list[dict]   # positions in Tradier with no open Ajoy trade
    ghost_in_ajoy: list[dict]         # open Ajoy trades with no matching Tradier position
    matched: list[dict]               # positions reconciled OK


def _parse_option_symbol(option_sym: str) -> tuple[str, str, str, float]:
    """
    Parse an OCC-format option symbol such as 'IWM260601P00290000'.

    Format: {UNDERLYING}{YYMMDD}{C|P}{8-digit-strike×1000}

    Returns (underlying, expiry_str, direction, strike).
    Raises ValueError if the symbol cannot be parsed.
    """
    m = re.match(r'^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$', option_sym)
    if not m:
        raise ValueError(f"Cannot parse option symbol: {option_sym!r}")
    underlying  = m.group(1)
    year        = 2000 + int(m.group(2))
    month       = int(m.group(3))
    day         = int(m.group(4))
    cp          = m.group(5)
    strike      = int(m.group(6)) / 1000.0
    expiry_str  = f"{year:04d}-{month:02d}-{day:02d}"
    direction   = "CALL" if cp == "C" else "PUT"
    return underlying, expiry_str, direction, strike


class OrphanCloseRequest(BaseModel):
    option_symbol: str
    quantity: int


class OrphanAdoptRequest(BaseModel):
    option_symbol: str
    quantity: int
    cost_per_unit: float   # per-contract price (cost_basis_total / qty / 100)


@router.get("/reconcile", response_model=ReconcileResult)
async def reconcile_positions(db: AsyncSession = Depends(get_db)):
    """
    Compare Tradier sandbox open positions against Ajoy open trades.
    Enriches orphaned positions with current live price and estimated P&L.
    """
    from app.config import settings as _cfg
    _excluded_tickers: set[str] = {
        s.strip().upper()
        for s in _cfg.orphan_stop_excluded_symbols.split(",")
        if s.strip()
    }

    client = get_tradier_client()

    try:
        positions = await client.get_positions()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch Tradier positions: {exc}")

    result = await db.execute(select(Trade).where(Trade.status == TradeStatus.OPEN))
    open_trades = result.scalars().all()

    ajoy_by_symbol: dict[str, Trade] = {t.option_symbol: t for t in open_trades}
    tradier_by_symbol = {p.symbol: p for p in positions}

    orphaned: list[dict] = []
    ghosts:   list[dict] = []
    matched:  list[dict] = []

    for sym, pos in tradier_by_symbol.items():
        if sym in ajoy_by_symbol:
            t = ajoy_by_symbol[sym]
            matched.append({
                "symbol": sym,
                "tradier_qty": pos.quantity,
                "ajoy_trade_id": t.id,
                "ajoy_qty": t.remaining_qty or t.quantity,
                "ajoy_entry": t.entry_price,
                "tradier_cost_basis": pos.cost_basis,
            })
        else:
            # cost_basis from Tradier is the total (qty × price × 100)
            # derive per-unit cost so we can compute P&L
            qty = pos.quantity
            cost_basis_total = pos.cost_basis or 0.0
            cost_per_unit = round(cost_basis_total / (qty * 100), 4) if qty else 0.0

            # Fetch live option quote for current price + estimated P&L
            current_price: Optional[float] = None
            live_pnl: Optional[float] = None
            try:
                q = await client.get_option_quote(sym)
                if q:
                    current_price = round(
                        (q.bid + q.ask) / 2 if q.bid and q.ask else q.last, 2
                    )
                    live_pnl = round((current_price - cost_per_unit) * qty * 100, 2)
            except Exception:
                pass

            # Determine if this orphan is excluded from auto-stop
            _underlying = re.match(r'^([A-Z]+)', sym)
            _ticker = _underlying.group(1) if _underlying else ""
            _excluded = _ticker in _excluded_tickers

            orphaned.append({
                "symbol": sym,
                "qty": qty,
                "cost_per_unit": cost_per_unit,
                "cost_basis_total": cost_basis_total,
                "current_price": current_price,
                "live_pnl": live_pnl,
                "auto_stop_excluded": _excluded,
            })

    for sym, trade in ajoy_by_symbol.items():
        if sym not in tradier_by_symbol:
            ghosts.append({
                "symbol": sym,
                "ajoy_trade_id": trade.id,
                "ajoy_qty": trade.remaining_qty or trade.quantity,
                "ajoy_entry": trade.entry_price,
                "note": "Ajoy shows this as open but Tradier has no position. "
                        "The option may have expired or been closed outside Ajoy.",
            })

    return ReconcileResult(
        orphaned_in_tradier=orphaned,
        ghost_in_ajoy=ghosts,
        matched=matched,
    )


@router.post("/orphan/close")
async def close_orphan_position(payload: OrphanCloseRequest):
    """
    Place a market sell_to_close order for an orphaned Tradier position
    (one that has no Ajoy DB record).  Does NOT create a DB trade record —
    it simply executes the sell in the sandbox account.
    """
    client = get_tradier_client()

    try:
        order = await client.place_option_order(
            option_symbol=payload.option_symbol,
            side="sell_to_close",
            quantity=payload.quantity,
            order_type="market",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Tradier API error: {exc}")

    # Check the order wasn't rejected
    fill_price = await client.get_fill_price(order.order_id)
    if not fill_price:
        try:
            status_data = await client.get_order_status(order.order_id)
            status_str  = (status_data.get("status") or "").lower()
        except Exception:
            status_str = "unknown"
        if status_str in ("rejected", "canceled", "cancelled"):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Sell order {order.order_id} was {status_str.upper()} by Tradier. "
                    "Position may still be open."
                ),
            )

    return {
        "order_id": order.order_id,
        "fill_price": fill_price,
        "message": f"Sell order submitted for {payload.quantity} × {payload.option_symbol}",
    }


# ---------------------------------------------------------------------------
# Per-trade level overrides (stop loss / take profit)
# ---------------------------------------------------------------------------

class UpdateLevelsRequest(BaseModel):
    stop_price: Optional[float] = None
    tp2_price:  Optional[float] = None


@router.patch("/{trade_id}/levels", response_model=TradeOut)
async def update_trade_levels(
    trade_id: int,
    payload: UpdateLevelsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Override stop loss and/or take profit for an individual open trade.

    Stop price change → modifies the resting broker stop order at Tradier
    (or replaces it if the modify fails).  Take profit change is DB-only
    (the manage loop handles TP exits, there is no Tradier TP order).

    Raises 400 if both fields are omitted.
    Raises 422 if the values are logically invalid (e.g. stop > entry).
    """
    if payload.stop_price is None and payload.tp2_price is None:
        raise HTTPException(status_code=400, detail="Provide at least one of stop_price or tp2_price.")

    trade = await db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != TradeStatus.OPEN:
        raise HTTPException(status_code=400, detail="Trade is already closed")

    entry = trade.entry_price
    direction = trade.direction.value if hasattr(trade.direction, "value") else str(trade.direction)

    # ── Validate new levels ───────────────────────────────────────────────
    # NOTE (Jul 9 2026): stops legitimately sit ABOVE entry on winning trades
    # (breakeven lock, trailing, runner mode) and a TP below entry is a valid
    # "get me out on a bounce" order.  The old entry-anchored rules made every
    # profit-locked trade uneditable (SOFI #137: 422 because the runner trail
    # held the stop at $0.18 vs $0.14 entry).  Validate the trade's CURRENT
    # geometry instead: both levels positive, stop below TP.
    if payload.stop_price is not None:
        stop = round(payload.stop_price, 2)
        if stop <= 0:
            raise HTTPException(status_code=422, detail="stop_price must be > 0")
    else:
        stop = None

    if payload.tp2_price is not None:
        tp = round(payload.tp2_price, 2)
        if tp <= 0:
            raise HTTPException(status_code=422, detail="tp2_price must be > 0")
    else:
        tp = None

    # Cross-field sanity: effective stop must stay below effective TP.
    eff_stop = stop if stop is not None else (trade.stop_price or 0)
    eff_tp   = tp   if tp   is not None else trade.tp2_price
    if eff_tp is not None and eff_stop and eff_stop >= eff_tp:
        raise HTTPException(
            status_code=422,
            detail=(
                f"stop ${eff_stop:.2f} must be below take-profit ${eff_tp:.2f} "
                "(adjust both together if needed)"
            ),
        )

    # ── Update Tradier broker stop order (if stop changed) ───────────────
    from app.config import settings as _cfg

    if stop is not None and _cfg.broker_stop_enabled:
        client = get_tradier_client()

        if trade.stop_order_id:
            # Existing order — try to modify in place; fall back to cancel + replace
            modified = False
            try:
                await client.modify_order(trade.stop_order_id, stop_price=stop)
                modified = True
                _enrich_logger.info(
                    "[%s] Trade %d broker stop modified to $%.2f (order %s)",
                    trade.symbol, trade.id, stop, trade.stop_order_id,
                )
            except Exception as exc:
                _enrich_logger.warning(
                    "[%s] Trade %d modify_order failed (%s) — canceling and re-placing stop",
                    trade.symbol, trade.id, exc,
                )

            if not modified:
                try:
                    await client.cancel_order(trade.stop_order_id)
                except Exception:
                    pass
                trade.stop_order_id = None
                await db.commit()

        if not trade.stop_order_id:
            # No existing stop (either never placed, or just cancelled above) — place fresh
            try:
                stop_order = await client.place_option_order(
                    option_symbol=trade.option_symbol,
                    side="sell_to_close",
                    quantity=trade.remaining_qty or trade.quantity,
                    order_type="stop",
                    stop_price=stop,
                )
                if stop_order.order_id:
                    trade.stop_order_id = stop_order.order_id
                    _enrich_logger.info(
                        "[%s] Trade %d broker stop placed: order %s @ $%.2f",
                        trade.symbol, trade.id, stop_order.order_id, stop,
                    )
                else:
                    _enrich_logger.error(
                        "[%s] Trade %d broker stop returned no order_id — bot-side stop only",
                        trade.symbol, trade.id,
                    )
            except Exception as exc2:
                _enrich_logger.error(
                    "[%s] Trade %d failed to place broker stop at $%.2f: %s",
                    trade.symbol, trade.id, stop, exc2,
                )

    # ── Update broker-side TP limit order (if TP changed) ────────────────
    # Tradier rejects/cancels one of two resting sells on the same contracts
    # (GOOGL #140: stop placed, TP placed, Tradier CANCELED the stop 5 min
    # later).  When a broker stop is resting, skip the broker TP — the
    # bot-side TP check enforces the new target on every management tick.
    if tp is not None and _cfg.broker_tp_enabled and trade.stop_order_id:
        _enrich_logger.info(
            "[%s] Trade %d: broker TP skipped — resting stop %s reserves the "
            "contracts (bot-side TP enforces the new target)",
            trade.symbol, trade.id, trade.stop_order_id,
        )
    elif tp is not None and _cfg.broker_tp_enabled:
        tp_client = get_tradier_client()
        # Cancel the existing TP order first (if any)
        if trade.tp_order_id:
            try:
                await tp_client.cancel_order(trade.tp_order_id)
                _enrich_logger.info(
                    "[%s] Trade %d: cancelled old broker TP order %s",
                    trade.symbol, trade.id, trade.tp_order_id,
                )
            except Exception as exc:
                _enrich_logger.warning(
                    "[%s] Trade %d: failed to cancel old broker TP %s: %s",
                    trade.symbol, trade.id, trade.tp_order_id, exc,
                )
            trade.tp_order_id = None

        # Place a fresh TP limit order at the new price
        try:
            new_tp_order = await tp_client.place_option_order(
                option_symbol=trade.option_symbol,
                side="sell_to_close",
                quantity=trade.remaining_qty or trade.quantity,
                order_type="limit",
                limit_price=tp,
            )
            if new_tp_order.order_id:
                trade.tp_order_id = new_tp_order.order_id
                _enrich_logger.info(
                    "[%s] Trade %d: new broker TP placed: order %s @ $%.2f",
                    trade.symbol, trade.id, new_tp_order.order_id, tp,
                )
            else:
                _enrich_logger.error(
                    "[%s] Trade %d: broker TP replace returned no order_id",
                    trade.symbol, trade.id,
                )
        except Exception as exc:
            _enrich_logger.error(
                "[%s] Trade %d: failed to place new broker TP at $%.2f: %s",
                trade.symbol, trade.id, tp, exc,
            )

    # ── Persist DB changes ────────────────────────────────────────────────
    if stop is not None:
        trade.stop_price = stop
    if tp is not None:
        trade.tp1_price = tp   # keep tp1 in sync (v1: both are the same target)
        trade.tp2_price = tp
        trade.tp_manual = True   # human-set target — runner mode must respect it

    _enrich_logger.info(
        "[%s] Trade %d levels updated — stop=%s  tp=%s",
        trade.symbol, trade.id,
        f"${stop:.2f}" if stop is not None else "unchanged",
        f"${tp:.2f}"   if tp  is not None else "unchanged",
    )

    await db.commit()
    await db.refresh(trade)
    return trade


@router.post("/orphan/adopt", response_model=TradeOut)
async def adopt_orphan_position(
    payload: OrphanAdoptRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Adopt an orphaned Tradier position into Ajoy so the scheduler manages it.

    Creates a Trade DB record using cost_per_unit as entry_price, computes
    stop / take-profit levels from current settings, and sets entry_time to
    the current time (we don't know when the original fill happened).

    After adoption, the position will appear in Open Positions and will be
    subject to trailing stop, TREND_REVERSAL, and end-of-day cutoff exits.
    """
    # ── 1. Guard: already adopted? ────────────────────────────────────────
    existing = await db.execute(
        select(Trade).where(
            Trade.option_symbol == payload.option_symbol,
            Trade.status == TradeStatus.OPEN,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"{payload.option_symbol} is already tracked as an open trade in Ajoy.",
        )

    # ── 2. Parse the option symbol → underlying, direction ────────────────
    try:
        underlying, expiry_str, direction_str, strike = _parse_option_symbol(
            payload.option_symbol
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    entry_price = round(payload.cost_per_unit, 4)
    if entry_price <= 0:
        raise HTTPException(status_code=422, detail="cost_per_unit must be > 0")

    # ── 3. Compute stop / TP levels ───────────────────────────────────────
    levels = compute_trade_levels(entry_price, direction_str)

    # ── 4. Fetch current underlying price + VWAP for display ─────────────
    client       = get_tradier_client()
    underlying_price: Optional[float] = None
    vwap_at_entry: Optional[float]    = None
    try:
        q = await client.get_quote(underlying)
        if q and q.last:
            underlying_price = q.last
    except Exception:
        pass

    # ── 5. Create the DB record ───────────────────────────────────────────
    trade = Trade(
        symbol          = underlying,
        option_symbol   = payload.option_symbol,
        direction       = Direction[direction_str],
        strategy_name   = "adopted_orphan",
        tradier_order_id= None,
        quantity        = payload.quantity,
        remaining_qty   = payload.quantity,
        entry_price     = entry_price,
        entry_time      = datetime.now(tz=timezone.utc),
        underlying_entry= underlying_price,
        vwap_at_entry   = vwap_at_entry,
        **levels,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    return trade
