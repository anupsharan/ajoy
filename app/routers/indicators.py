from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Indicator, IndicatorGroup, IndicatorGroupMember, Strategy
from app.schemas import (
    IndicatorCreate,
    IndicatorGroupCreate,
    IndicatorGroupOut,
    IndicatorOut,
    IndicatorUpdate,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
)
from app.services.indicators import evaluate_indicator
from app.services.tradier import get_tradier_client

router = APIRouter(prefix="/api/indicators", tags=["indicators"])


# ---------------------------------------------------------------------------
# Live evaluation endpoint — fetches Tradier data and fires all indicators
# ---------------------------------------------------------------------------

@router.get("/evaluate/{symbol}")
async def evaluate_for_symbol(
    symbol: str,
    direction: str = "CALL",
    db: AsyncSession = Depends(get_db),
):
    """
    Full entry-stack diagnostic for a symbol.

    Runs all 13 gates in the same order the scheduler uses:
      G1–G6  Pre-entry guards  (trading window, P&L cap, open-trade limits, cooldown)
      L1–L6  Signal layers     (UI indicators, bounce, momentum, VWAP slope, regime, IV)

    Returns indicator results (for the Indicators tab table) PLUS a
    `gate_stack` list and `first_blocker` field so you can see exactly
    which gate is preventing an entry.
    """
    import asyncio
    from datetime import date as _date
    from app.config import settings as _cfg
    from app.models import Trade as _Trade, TradeStatus as _TS, ExitReason as _ER
    from app.services.strategy import (
        is_in_trading_window, calculate_vwap,
        check_bounce_confirmation, check_momentum_candle,
        check_vwap_slope, get_market_regime, ema_direction,
    )
    from app.services.scheduler import (
        _get_daily_pnl, _get_symbol_losses_today, _get_recent_bad_exit,
    )
    from sqlalchemy import select as _sel, func as _func

    client = get_tradier_client()
    symbol = symbol.upper()

    # ── Fetch all market data up front ────────────────────────────────────
    bars_1m, bars_15m, expirations = await asyncio.gather(
        client.get_intraday_bars(symbol, interval="1min",  lookback_days=1),
        client.get_intraday_bars(symbol, interval="15min",
                                 lookback_days=_cfg.trend_lookback_days),
        client.get_option_expirations(symbol),
        return_exceptions=True,
    )
    if isinstance(bars_1m,      Exception): bars_1m      = []
    if isinstance(bars_15m,     Exception): bars_15m     = []
    if isinstance(expirations,  Exception): expirations  = []

    # Options chain (for PCR indicators + L6 IV)
    full_chain, calls, puts = [], [], []
    if expirations:
        today_str = _date.today().isoformat()
        non_0dte  = [e for e in expirations if e > today_str]
        exp_for_chain = non_0dte[0] if non_0dte else expirations[0]
        try:
            full_chain = await client.get_options_chain(symbol, exp_for_chain)
            calls = [o for o in full_chain if o.option_type == "call"]
            puts  = [o for o in full_chain if o.option_type == "put"]
        except Exception:
            pass

    current_price = bars_1m[-1].close if bars_1m else None
    vwap          = calculate_vwap(bars_1m) if bars_1m else 0.0

    # ── Helper ────────────────────────────────────────────────────────────
    def _gate(gid, name, ok, reason):
        return {"id": gid, "name": name, "pass": ok, "reason": reason}

    gate_stack = []

    # ── G1: Trading window ────────────────────────────────────────────────
    in_window = is_in_trading_window()
    from app.services.strategy import is_in_trading_window as _itw
    gate_stack.append(_gate(
        "G1", "Trading window", in_window,
        f"{_cfg.trading_start_time}–{_cfg.trading_end_time} ET"
        + (f"  (lunch {_cfg.lunch_break_start}–{_cfg.lunch_break_end} blocked)"
           if _cfg.lunch_break_enabled else "")
        if in_window else
        f"Outside window — entries allowed {_cfg.trading_start_time}–{_cfg.trading_end_time} ET"
        + (f"  (or blocked by lunch {_cfg.lunch_break_start}–{_cfg.lunch_break_end})"
           if _cfg.lunch_break_enabled else ""),
    ))

    # ── G2: Daily P&L loss cap ────────────────────────────────────────────
    daily_pnl  = await _get_daily_pnl(db)
    pnl_ok     = daily_pnl > -abs(_cfg.max_daily_loss)
    gate_stack.append(_gate(
        "G2", "Daily P&L cap", pnl_ok,
        f"Today P&L ${daily_pnl:+.2f} (realized + open worst-case) / limit −${_cfg.max_daily_loss:.0f}",
    ))

    # ── G3: Max concurrent open trades ───────────────────────────────────
    open_res   = await db.execute(_sel(_Trade).where(_Trade.status == _TS.OPEN))
    open_count = len(open_res.scalars().all())
    trades_ok  = open_count < _cfg.max_open_trades
    gate_stack.append(_gate(
        "G3", "Max open trades", trades_ok,
        f"{open_count}/{_cfg.max_open_trades} open positions",
    ))

    # ── G4: Per-symbol open trade ─────────────────────────────────────────
    sym_open_res = await db.execute(
        _sel(_Trade).where(_Trade.symbol == symbol, _Trade.status == _TS.OPEN)
    )
    sym_open_ok = sym_open_res.scalar_one_or_none() is None
    gate_stack.append(_gate(
        "G4", f"No open {symbol} trade", sym_open_ok,
        "No existing open position" if sym_open_ok
        else f"Already have an open {symbol} position — one trade per symbol",
    ))

    # ── G5: Per-symbol daily loss cap ─────────────────────────────────────
    sym_losses  = await _get_symbol_losses_today(db, symbol)
    losses_ok   = sym_losses < _cfg.max_losses_per_symbol_per_day
    gate_stack.append(_gate(
        "G5", "Per-symbol loss cap", losses_ok,
        f"{sym_losses}/{_cfg.max_losses_per_symbol_per_day} losing trades on {symbol} today",
    ))

    # ── G6: Cooldown after STOP / VWAP_BREAK ─────────────────────────────
    bad_exit    = await _get_recent_bad_exit(db, symbol)
    cooldown_ok = bad_exit is None
    gate_stack.append(_gate(
        "G6", "Cooldown", cooldown_ok,
        "No recent bad exit" if cooldown_ok
        else (f"Cooldown active — last {bad_exit.exit_reason.value} at "
              f"{bad_exit.exit_time.strftime('%H:%M UTC')} "
              f"({_cfg.cooldown_minutes}-min window)"),
    ))

    # ── L1: UI Indicators (existing logic) ───────────────────────────────
    ind_result = await db.execute(
        select(Indicator).where(Indicator.active == True)  # noqa: E712
    )
    active_inds = ind_result.scalars().all()

    ind_results = []
    for ind in active_inds:
        r = evaluate_indicator(
            key=ind.key, direction=direction,
            bars_1m=bars_1m, bars_15m=bars_15m,
            option_calls=calls, option_puts=puts,
        )
        ind_results.append({
            "key": ind.key, "name": ind.name,
            "fires": r.fires, "value": r.value, "reason": r.reason,
        })

    l1_ok = all(r["fires"] for r in ind_results) if ind_results else False
    gate_stack.append(_gate(
        "L1", "Entry signal (L1)",  l1_ok,
        "Trend + VWAP pullback confirmed" if l1_ok
        else "One or more L1 indicators not firing — see table below",
    ))

    # ── L2: Multi-bar bounce confirmation ────────────────────────────────
    l2_ok = False
    l2_reason = "Insufficient bars"
    if bars_1m and vwap:
        l2_ok = check_bounce_confirmation(bars_1m, direction, vwap)
        n     = _cfg.bounce_bars_required
        l2_reason = (
            f"Last {n} completed bars all {'above' if direction=='CALL' else 'below'} VWAP {vwap:.2f}"
            if l2_ok
            else f"Need {n} consecutive bars {'above' if direction=='CALL' else 'below'} VWAP {vwap:.2f} — not confirmed"
        )
    gate_stack.append(_gate("L2", "Bounce confirmation (L2)", l2_ok, l2_reason))

    # ── L3: Momentum candle ───────────────────────────────────────────────
    l3_ok = False
    l3_reason = "Insufficient bars"
    if bars_1m and len(bars_1m) >= 3:
        l3_ok = check_momentum_candle(bars_1m, direction)
        last  = bars_1m[-2]
        prev  = bars_1m[-3]
        l3_reason = (
            f"Last bar: close={last.close:.2f} open={last.open:.2f} prev_close={prev.close:.2f} — momentum confirmed"
            if l3_ok
            else f"Last bar: close={last.close:.2f} open={last.open:.2f} prev_close={prev.close:.2f} — not a {'green rising' if direction=='CALL' else 'red falling'} candle"
        )
    gate_stack.append(_gate("L3", "Momentum candle (L3)", l3_ok, l3_reason))

    # ── L4: Intraday VWAP slope ───────────────────────────────────────────
    l4_ok = True
    l4_reason = "Too few bars for slope"
    if bars_1m and len(bars_1m) >= _cfg.vwap_slope_lookback_bars + 5:
        l4_ok = check_vwap_slope(bars_1m, direction)
        vwap_now  = calculate_vwap(bars_1m)
        vwap_then = calculate_vwap(bars_1m[:-_cfg.vwap_slope_lookback_bars])
        slope_pct = (vwap_now - vwap_then) / vwap_then * 100 if vwap_then else 0
        l4_reason = (
            f"VWAP slope {slope_pct:+.3f}% over {_cfg.vwap_slope_lookback_bars} bars — OK"
            if l4_ok
            else f"VWAP slope {slope_pct:+.3f}% opposes {direction} (threshold ±{_cfg.vwap_slope_threshold_pct}%)"
        )
    gate_stack.append(_gate("L4", "VWAP slope (L4)", l4_ok, l4_reason))

    # ── L5: Market regime gate ────────────────────────────────────────────
    # Logic matches _attempt_entry() in scheduler.py exactly.
    # SPY alone blocks the trade — the old "both SPY and stock must align"
    # rule was too permissive and caused losses on strongly trending days.
    # Special case: skip L5 when the symbol IS the regime proxy (e.g. SPY).
    l5_ok     = True
    l5_reason = "Regime gate disabled"
    if _cfg.regime_gate_enabled:
        try:
            regime = await get_market_regime(client)
            if symbol.upper() == _cfg.regime_gate_symbol.upper():
                l5_reason = (f"Regime gate skipped — {symbol} is the regime proxy itself "
                             f"(circular check avoided)")
            elif direction == "CALL" and regime == "bearish":
                l5_ok     = False
                l5_reason = f"SPY BEARISH — CALL blocked"
            elif direction == "PUT" and regime == "bullish":
                l5_ok     = False
                l5_reason = f"SPY BULLISH — PUT blocked"
            else:
                l5_reason = f"SPY={regime.upper()} — {direction} allowed"
        except Exception as e:
            l5_reason = f"Regime check failed: {e}"
    gate_stack.append(_gate("L5", f"Regime gate — {_cfg.regime_gate_symbol} (L5)", l5_ok, l5_reason))

    # ── L6: IV filter ────────────────────────────────────────────────────
    l6_ok     = True
    l6_reason = "IV data unavailable — entry allowed"
    if full_chain and current_price:
        try:
            atm_iv = client.get_atm_iv(full_chain, direction, current_price)
            if atm_iv is not None:
                l6_ok     = atm_iv <= _cfg.iv_max_threshold
                l6_reason = (
                    f"ATM IV {atm_iv*100:.1f}% ≤ {_cfg.iv_max_threshold*100:.0f}% threshold — OK"
                    if l6_ok
                    else f"ATM IV {atm_iv*100:.1f}% > {_cfg.iv_max_threshold*100:.0f}% threshold — premium too expensive"
                )
        except Exception as e:
            l6_reason = f"IV check failed: {e}"
    gate_stack.append(_gate("L6", "IV filter (L6)", l6_ok, l6_reason))

    # ── First blocker ────────────────────────────────────────────────────
    first_blocker = next((g for g in gate_stack if not g["pass"]), None)
    full_pass     = first_blocker is None

    return {
        "symbol":         symbol,
        "direction":      direction,
        "all_pass":       l1_ok,          # backward-compat: L1 indicator table badge
        "full_pass":      full_pass,      # true only when ALL 13 gates pass
        "first_blocker":  first_blocker,
        "bars_1m":        len(bars_1m),
        "bars_15m":       len(bars_15m),
        "results":        ind_results,    # L1 indicator rows (existing table)
        "gate_stack":     gate_stack,     # full G1–L6 list for the new panel
    }


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

@router.get("", response_model=list[IndicatorOut])
async def list_indicators(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Indicator).order_by(Indicator.name))
    return result.scalars().all()


@router.post("", response_model=IndicatorOut, status_code=201)
async def create_indicator(payload: IndicatorCreate, db: AsyncSession = Depends(get_db)):
    # Auto-generate key from name if not explicitly provided or empty
    key = payload.key.strip() or payload.name.lower().replace(" ", "_")
    ind = Indicator(**{**payload.model_dump(), "key": key})
    db.add(ind)
    await db.commit()
    await db.refresh(ind)
    return ind


@router.patch("/{indicator_id}", response_model=IndicatorOut)
async def update_indicator(
    indicator_id: int, payload: IndicatorUpdate, db: AsyncSession = Depends(get_db)
):
    ind = await db.get(Indicator, indicator_id)
    if not ind:
        raise HTTPException(status_code=404, detail="Indicator not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(ind, field, val)
    await db.commit()
    await db.refresh(ind)
    return ind


@router.delete("/{indicator_id}", status_code=204)
async def delete_indicator(indicator_id: int, db: AsyncSession = Depends(get_db)):
    ind = await db.get(Indicator, indicator_id)
    if not ind:
        raise HTTPException(status_code=404, detail="Indicator not found")
    await db.delete(ind)
    await db.commit()


# ---------------------------------------------------------------------------
# Indicator Groups
# ---------------------------------------------------------------------------

@router.get("/groups", response_model=list[IndicatorGroupOut])
async def list_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(IndicatorGroup)
        .options(selectinload(IndicatorGroup.members))
        .order_by(IndicatorGroup.name)
    )
    groups = result.scalars().all()
    return [IndicatorGroupOut.from_orm_with_members(g) for g in groups]


@router.post("/groups", response_model=IndicatorGroupOut, status_code=201)
async def create_group(payload: IndicatorGroupCreate, db: AsyncSession = Depends(get_db)):
    group = IndicatorGroup(name=payload.name, logic_type=payload.logic_type)
    db.add(group)
    await db.flush()
    for ind_id in payload.indicator_ids:
        db.add(IndicatorGroupMember(group_id=group.id, indicator_id=ind_id))
    await db.commit()
    await db.refresh(group)
    result = await db.execute(
        select(IndicatorGroup)
        .options(selectinload(IndicatorGroup.members))
        .where(IndicatorGroup.id == group.id)
    )
    g = result.scalar_one()
    return IndicatorGroupOut.from_orm_with_members(g)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    group = await db.get(IndicatorGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).order_by(Strategy.name))
    return result.scalars().all()


@router.post("/strategies", response_model=StrategyOut, status_code=201)
async def create_strategy(payload: StrategyCreate, db: AsyncSession = Depends(get_db)):
    strat = Strategy(**payload.model_dump())
    db.add(strat)
    await db.commit()
    await db.refresh(strat)
    return strat


@router.patch("/strategies/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: int, payload: StrategyUpdate, db: AsyncSession = Depends(get_db)
):
    strat = await db.get(Strategy, strategy_id)
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(strat, field, val)
    await db.commit()
    await db.refresh(strat)
    return strat


@router.delete("/strategies/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    strat = await db.get(Strategy, strategy_id)
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await db.delete(strat)
    await db.commit()
