#!/usr/bin/env python3
"""
Ajoy Strategy Backtest
======================
Replays the full L1–L5 entry gate stack on historical Tradier 1-min / 15-min
bars for every active symbol.  Option P&L is approximated:

  ATM delta   ≈ 0.45
  ATM premium ≈ 1.5 % of underlying price  (rough ~1–3 DTE estimate)
  Stop        = premium × STOP_LOSS_PCT    (default 22 %)
  TP          = premium × TAKE_PROFIT_PCT  (default 25 %)
  P&L/contract = underlying_move × ATM_DELTA × 100

Run from the ajoy/ directory:
    python3 backtest.py
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app.config import settings
from app.services.tradier import TradierClient, Bar
from app.services.strategy import (
    check_entry_signal,
    check_bounce_confirmation,
    check_momentum_candle,
    check_vwap_slope,
    ema_direction,
)

ET = ZoneInfo("America/New_York")

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOLS = [
    "AAPL", "AMD", "AMZN", "AVGO", "CRM", "EXE", "GOOGL", "HOOD",
    "INTC", "META", "MRVL", "MSFT", "MU", "NOW", "NVDA", "ORCL",
    "PLTR", "QQQ", "SPY", "TSLA",
]
LOOKBACK_TRADING_DAYS = 20   # trading days to simulate
WARMUP_EXTRA_DAYS     = 12   # calendar days added for EMA seed history
ATM_DELTA             = 0.45
ATM_PREMIUM_PCT       = 0.015   # ~1.5 % of underlying = rough ATM premium
MAX_HOLD_BARS         = 45      # max bars (minutes) a simulated trade is held

OUTPUT_CSV = "backtest_results.csv"


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_trading_days(n: int) -> list[date]:
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_scan_window(t: time) -> bool:
    return _hhmm(settings.trading_start_time) <= t <= _hhmm(settings.last_entry_time)


def in_cutoff(t: time) -> bool:
    return t >= _hhmm(settings.trading_end_time)


def bars_for_day(bars: list[Bar], d: date) -> list[Bar]:
    return [b for b in bars if b.time.date() == d]


# ── Exit simulator ────────────────────────────────────────────────────────────

def simulate_exit(
    bars_1m_day: list[Bar],
    entry_idx:   int,
    direction:   str,
    entry_price: float,
) -> dict:
    """
    Walk forward bar-by-bar from entry_idx+1 and return the first exit.
    Uses delta approximation for approximate option P&L.
    """
    premium   = entry_price * ATM_PREMIUM_PCT
    stop_move = (premium * settings.stop_loss_pct)   / ATM_DELTA
    tp_move   = (premium * settings.take_profit_pct) / ATM_DELTA

    future = bars_1m_day[entry_idx + 1 : entry_idx + 1 + MAX_HOLD_BARS]
    if not future:
        return dict(exit_reason="NO_DATA", bars_held=0, pnl=0.0, win=False,
                    exit_price=entry_price)

    for i, bar in enumerate(future):
        move = (bar.close - entry_price) if direction == "CALL" else (entry_price - bar.close)
        pnl  = round(move * ATM_DELTA * 100, 2)
        if move >= tp_move:
            return dict(exit_reason="TP",   bars_held=i + 1, pnl=pnl, win=True,
                        exit_price=bar.close)
        if move <= -stop_move:
            return dict(exit_reason="STOP", bars_held=i + 1, pnl=pnl, win=False,
                        exit_price=bar.close)

    last = future[-1]
    move = (last.close - entry_price) if direction == "CALL" else (entry_price - last.close)
    pnl  = round(move * ATM_DELTA * 100, 2)
    return dict(exit_reason="CUTOFF", bars_held=len(future), pnl=pnl,
                win=pnl >= 0, exit_price=last.close)


# ── Core backtest ─────────────────────────────────────────────────────────────

async def run_backtest() -> tuple[list[dict], dict]:
    client        = TradierClient()
    trading_days  = get_trading_days(LOOKBACK_TRADING_DAYS)
    fetch_start   = (trading_days[0] - timedelta(days=WARMUP_EXTRA_DAYS)).strftime("%Y-%m-%d 09:30")
    fetch_end     = date.today().strftime("%Y-%m-%d 16:00")

    print(f"Fetching {len(SYMBOLS)} symbols  |  {fetch_start} → {fetch_end}\n")

    all_1m:  dict[str, list[Bar]] = {}
    all_15m: dict[str, list[Bar]] = {}
    sem = asyncio.Semaphore(4)

    async def _fetch(sym: str) -> None:
        async with sem:
            print(f"  ↓ {sym}")
            all_1m[sym]  = await client.get_intraday_bars(sym, "1min",  start=fetch_start, end=fetch_end)
            all_15m[sym] = await client.get_intraday_bars(sym, "15min", start=fetch_start, end=fetch_end)

    await asyncio.gather(*[_fetch(s) for s in SYMBOLS])
    print(f"\nReady. Replaying {LOOKBACK_TRADING_DAYS} trading days…\n")

    trades:      list[dict]                  = []
    gate_blocks: dict[str, dict[str, int]]  = defaultdict(lambda: defaultdict(int))

    spy_15m_all = all_15m.get("SPY", [])

    for day in trading_days:
        day_str = day.strftime("%Y-%m-%d")

        # --- Build the time-sorted list of scan minutes for this day ----------
        # Use SPY 1-min bars to get the canonical minute grid for each day.
        spy_1m_day = bars_for_day(all_1m.get("SPY", []), day)
        scan_times = [
            b.time for b in spy_1m_day
            if in_scan_window(b.time.time())
        ]
        if not scan_times:
            continue  # market holiday or no data

        # --- Per-day state ----------------------------------------------------
        open_pos: dict[str, dict] = {}          # sym → open simulated position
        sym_trades_today: dict[str, int]   = defaultdict(int)
        sym_losses_today: dict[str, int]   = defaultdict(int)
        sym_last_stop:    dict[str, datetime] = {}

        for scan_time in scan_times:
            scan_et = datetime(day.year, day.month, day.day,
                               scan_time.hour, scan_time.minute, tzinfo=ET)

            # ── Manage open positions first (check exits at this minute) ──────
            closed_now: list[str] = []
            for sym, pos in open_pos.items():
                sym_1m_day = bars_for_day(all_1m.get(sym, []), day)
                bar_idx    = pos["entry_bar_idx"]
                # Find bar at current scan time
                cur_bars   = [b for b in sym_1m_day if b.time <= scan_time]
                if not cur_bars:
                    continue
                cur_bar  = cur_bars[-1]
                bars_held = len(cur_bars) - bar_idx

                move = ((cur_bar.close - pos["entry_price"]) if pos["direction"] == "CALL"
                        else (pos["entry_price"] - cur_bar.close))
                premium   = pos["entry_price"] * ATM_PREMIUM_PCT
                stop_move = (premium * settings.stop_loss_pct)   / ATM_DELTA
                tp_move   = (premium * settings.take_profit_pct) / ATM_DELTA

                exit_reason = None
                if move >= tp_move:
                    exit_reason = "TP"
                elif move <= -stop_move:
                    exit_reason = "STOP"
                elif bars_held >= MAX_HOLD_BARS or in_cutoff(scan_et.time()):
                    exit_reason = "CUTOFF"

                if exit_reason:
                    pnl = round(move * ATM_DELTA * 100, 2)
                    pos.update(dict(
                        exit_reason=exit_reason, exit_time=scan_et.strftime("%H:%M ET"),
                        exit_price=cur_bar.close, pnl=pnl, win=pnl >= 0, bars_held=bars_held,
                    ))
                    trades.append(pos)
                    if not pos["win"]:
                        sym_losses_today[sym] += 1
                        if exit_reason == "STOP":
                            sym_last_stop[sym] = scan_et
                    closed_now.append(sym)

            for sym in closed_now:
                del open_pos[sym]

            # ── Scan each symbol for entry at this minute ─────────────────────
            for sym in SYMBOLS:
                # G4: already open
                if sym in open_pos:
                    continue

                # G3: global max open
                if len(open_pos) >= settings.max_open_trades:
                    gate_blocks[sym]["G3_max_open"] += 1
                    continue

                # G5: per-symbol daily loss cap
                if sym_losses_today[sym] >= settings.max_losses_per_symbol_per_day:
                    gate_blocks[sym]["G5_loss_cap"] += 1
                    continue

                # G5b: per-symbol trade cap
                if sym_trades_today[sym] >= settings.max_trades_per_symbol_per_day:
                    gate_blocks[sym]["G5b_trade_cap"] += 1
                    continue

                # G6: cooldown after STOP
                if sym in sym_last_stop:
                    elapsed = (scan_et - sym_last_stop[sym]).total_seconds() / 60
                    if elapsed < settings.cooldown_minutes:
                        gate_blocks[sym]["G6_cooldown"] += 1
                        continue

                # Build bar slices available at this exact scan minute
                sym_1m_day  = bars_for_day(all_1m.get(sym, []),  day)
                sym_15m_all = [b for b in all_15m.get(sym, []) if b.time.date() <= day]
                bars_1m_now  = [b for b in sym_1m_day  if b.time <= scan_time]
                bars_15m_now = [b for b in sym_15m_all if b.time <= scan_time]

                if len(bars_1m_now) < 6 or len(bars_15m_now) < settings.ema_period + 2:
                    continue

                # ── L1 ────────────────────────────────────────────────────────
                signal = check_entry_signal(bars_1m_now, bars_15m_now)
                if not signal:
                    gate_blocks[sym]["L1_no_signal"] += 1
                    continue

                # ── L2 ────────────────────────────────────────────────────────
                if not check_bounce_confirmation(bars_1m_now, signal.direction, signal.vwap):
                    gate_blocks[sym]["L2_bounce"] += 1
                    continue

                # ── L3 ────────────────────────────────────────────────────────
                if not check_momentum_candle(bars_1m_now, signal.direction):
                    gate_blocks[sym]["L3_momentum"] += 1
                    continue

                # ── L4 ────────────────────────────────────────────────────────
                if not check_vwap_slope(bars_1m_now, signal.direction):
                    gate_blocks[sym]["L4_slope"] += 1
                    continue

                # ── L5: SPY regime ────────────────────────────────────────────
                if settings.regime_gate_enabled and sym != settings.regime_gate_symbol:
                    spy_15m_now = [b for b in spy_15m_all if b.time <= scan_time]
                    if spy_15m_now:
                        spy_regime = ema_direction(spy_15m_now, settings.ema_period)
                        if signal.direction == "CALL" and spy_regime == "bearish":
                            gate_blocks[sym]["L5_regime"] += 1
                            continue
                        if signal.direction == "PUT" and spy_regime == "bullish":
                            gate_blocks[sym]["L5_regime"] += 1
                            continue

                # ── All gates passed — record entry ───────────────────────────
                entry_bar_idx = len(bars_1m_now) - 1
                sym_trades_today[sym] += 1
                print(f"  [{day_str}] ENTRY  {sym:6s} {signal.direction:4s} "
                      f"@ {bars_1m_now[-1].close:.2f}  {scan_et.strftime('%H:%M')} ET")

                open_pos[sym] = dict(
                    date=day_str, symbol=sym, direction=signal.direction,
                    entry_time=scan_et.strftime("%H:%M ET"),
                    entry_price=bars_1m_now[-1].close,
                    entry_bar_idx=entry_bar_idx,
                    vwap_at_entry=round(signal.vwap, 2),
                    # exit fields (filled on close)
                    exit_reason=None, exit_time=None, exit_price=None,
                    pnl=None, win=None, bars_held=None,
                )

        # ── EOD: force-close any positions still open at day end ──────────────
        for sym, pos in open_pos.items():
            sym_1m_day = bars_for_day(all_1m.get(sym, []), day)
            if sym_1m_day:
                last = sym_1m_day[-1]
                move = ((last.close - pos["entry_price"]) if pos["direction"] == "CALL"
                        else (pos["entry_price"] - last.close))
                pnl = round(move * ATM_DELTA * 100, 2)
                pos.update(dict(
                    exit_reason="EOD", exit_time=last.time.strftime("%H:%M ET"),
                    exit_price=last.close, pnl=pnl, win=pnl >= 0,
                    bars_held=len(sym_1m_day) - pos["entry_bar_idx"],
                ))
            trades.append(pos)

    return trades, dict(gate_blocks)


# ── Reporting ─────────────────────────────────────────────────────────────────

def build_summary(trades: list[dict]) -> dict:
    if not trades:
        return {}
    wins   = [t for t in trades if t.get("win")]
    losses = [t for t in trades if not t.get("win")]
    total  = len(trades)
    pnl    = sum(t.get("pnl", 0) or 0 for t in trades)
    days   = sorted(set(t["date"] for t in trades))

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)

    return dict(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_pct=round(len(wins) / total * 100, 1) if total else 0,
        total_pnl=round(pnl, 2),
        trading_days=len(days),
        trades_per_day=round(total / len(days), 1) if days else 0,
        by_symbol={
            sym: dict(
                trades=len(ts),
                wins=sum(1 for t in ts if t.get("win")),
                win_pct=round(sum(1 for t in ts if t.get("win")) / len(ts) * 100, 1),
                pnl=round(sum(t.get("pnl", 0) or 0 for t in ts), 2),
                directions=dict(
                    CALL=sum(1 for t in ts if t["direction"] == "CALL"),
                    PUT=sum(1 for t in ts if t["direction"] == "PUT"),
                ),
            )
            for sym, ts in sorted(by_sym.items())
        },
    )


def print_summary(summary: dict, gate_blocks: dict) -> None:
    print(f"\n{'='*65}")
    print(f"  AJOY BACKTEST RESULTS  ({summary['trading_days']} trading days, "
          f"{LOOKBACK_TRADING_DAYS} day window)")
    print(f"  Strategy params: VWAP_BAND={settings.vwap_band_pct*100:.2f}%  "
          f"EMA={settings.ema_period}  MAX_OPEN={settings.max_open_trades}")
    print(f"{'='*65}")
    print(f"  Total trades    : {summary['total_trades']}")
    print(f"  Wins / Losses   : {summary['wins']} / {summary['losses']}")
    print(f"  Win rate        : {summary['win_pct']}%")
    print(f"  Approx P&L      : ${summary['total_pnl']:+.2f}  (1 contract/trade, delta approx)")
    print(f"  Trades / day    : {summary['trades_per_day']}")
    print()

    print(f"  {'Symbol':<8}  {'Trades':>6}  {'Win%':>6}  {'CALL':>5}  {'PUT':>5}  {'Approx P&L':>12}")
    print(f"  {'-'*57}")
    for sym, s in summary["by_symbol"].items():
        print(f"  {sym:<8}  {s['trades']:>6}  {s['win_pct']:>5.0f}%  "
              f"{s['directions']['CALL']:>5}  {s['directions']['PUT']:>5}  "
              f"${s['pnl']:>+10.2f}")

    print(f"\n  Gate Block Summary (top blockers):")
    print(f"  {'Symbol':<8}  {'L1':>7}  {'L2':>6}  {'L3':>6}  {'L4':>6}  {'L5':>6}  {'G6':>8}")
    print(f"  {'-'*57}")
    for sym in sorted(gate_blocks):
        gb = gate_blocks[sym]
        print(f"  {sym:<8}  {gb.get('L1_no_signal',0):>7}  "
              f"{gb.get('L2_bounce',0):>6}  {gb.get('L3_momentum',0):>6}  "
              f"{gb.get('L4_slope',0):>6}  {gb.get('L5_regime',0):>6}  "
              f"{gb.get('G6_cooldown',0):>8}")


def write_csv(trades: list[dict], path: str) -> None:
    if not trades:
        return
    fields = ["date", "symbol", "direction", "entry_time", "entry_price",
              "vwap_at_entry", "exit_reason", "exit_time", "exit_price",
              "pnl", "win", "bars_held"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = {k: t.get(k, "") for k in fields}
            w.writerow(row)
    print(f"\n  CSV saved → {path}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> tuple[list[dict], dict]:
    trades, gate_blocks = await run_backtest()
    if not trades:
        print("No simulated trades found in the backtest window.")
        return [], {}
    summary = build_summary(trades)
    print_summary(summary, gate_blocks)
    write_csv(trades, OUTPUT_CSV)
    return trades, gate_blocks


if __name__ == "__main__":
    asyncio.run(main())
