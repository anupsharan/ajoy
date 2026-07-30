"""
APScheduler background tasks.

Two jobs run while the market is open:
  scan_for_entries   : every 60 s — run the full 8-gate entry stack for each symbol
  manage_open_trades : every 30 s — check exit conditions for open trades

Entry gate order
----------------
Pre-entry guards (DB / time checks — fast, no API calls):
  G1  Trading hours window
  G2  Daily P&L loss limit
  G3  Max concurrent open trades
  G4  Per-symbol open trade already exists
  G5  Per-symbol daily loss cap        (new)
  G6  Cooldown after STOP / VWAP_BREAK (new)

Signal layers (require market data):
  L1  check_entry_signal()   — trend_15min AND price_vs_vwap AND pullback_to_vwap
  L2  check_bounce_confirmation() — last N bars close on correct VWAP side
  L3  check_momentum_candle() — last completed bar is a momentum candle
  L4  check_vwap_slope()      — intraday VWAP slope must agree with direction
  L5  get_market_regime()     — SPY 15-min trend gate (cached, config-driven)

Post-selection filter:
  L6  IV filter               — skip if ATM IV > iv_max_threshold
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func as sqlfunc

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Direction, ExitReason, Symbol, Trade, TradeStatus
from app.services.strategy import (
    completed_bars,
    check_entry_signal,
    check_bounce_confirmation,
    check_momentum_candle,
    check_vwap_slope,
    get_regime_from_vwap,
    get_adaptive_vwap_band,
    check_exit_conditions,
    compute_trailing_stop,
    compute_trade_levels,
    compute_structural_levels,
    get_structural_stop_target,
    calculate_atr,
    calculate_vwap,
    check_chop_regime,
    session_bars,
    should_activate_runner,
    is_market_open,
    is_past_cutoff,
    is_in_trading_window,
    ema_direction,
)
from app.services.tradier import get_tradier_client
from app.services.accounts import (
    AccountView,
    account_of,
    active_account_views,
    all_account_views,
    scope as _scope,
)
from app.services.strategy_ema import (
    check_5min_trend_filter,
    check_ema_cross_freshness,
    get_5min_ema9,
    check_1min_pullback,
    check_1min_confirmation,
    check_option_spread,
    check_volume_filter,
    check_s2_exit_conditions,
)
from zoneinfo import ZoneInfo as _ZoneInfo

_ET = _ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="America/New_York")


# ---------------------------------------------------------------------------
# Multi-account helpers (Jul 25 2026)
#
# Every scanner/manager runs once PER ENABLED ACCOUNT.  Rather than threading
# an `acct` argument through ~40 nested functions, the account travels ON THE
# CLIENT — one Tradier client per account, each carrying its AccountView.
# These three helpers are the whole interface:
#
#   _acct(client)      → the AccountView this client trades for
#   _s(client, key)    → a setting, resolved against that account's overrides
#   _aid(client)       → the account id to stamp on a new Trade row
#
# For a mock client (the entire existing test-suite) `_acct` returns the
# legacy `.env` view, whose id is None → no account filtering, global
# settings, exactly the pre-multi-account behaviour.
# ---------------------------------------------------------------------------

def _acct(client) -> AccountView:
    """The account a client belongs to (legacy .env view when it has none)."""
    return account_of(client)


def _s(client, key: str):
    """Resolve a setting for this client's account (override → global)."""
    return account_of(client).setting(key)


def _aid(client) -> int | None:
    """Account id to stamp on trades opened through this client."""
    return account_of(client).id


def _tag(client) -> str:
    """
    Log prefix identifying the account, e.g. "[Roth#2] ".

    Empty for the single/legacy account so existing log greps and the funnel
    lines documented in CLAUDE.md §8 are unchanged when only one account
    exists — multi-account logging must not break log forensics.
    """
    acct = account_of(client)
    return f"[{acct.name}] " if acct.id is not None else ""


async def _for_each_account(job_name: str, flag: str | None, fn) -> None:
    """
    Run `fn(acct)` once per enabled account, isolating failures.

    One account throwing (bad token, revoked access, Tradier outage on that
    account) must never stop the others from being scanned or — far more
    important — from having their open trades MANAGED.

    `flag` is the per-account strategy toggle to require (e.g. "s2_enabled");
    None means the job applies to every account.
    """
    try:
        accounts = await active_account_views()
    except Exception as exc:
        logger.error("%s: could not load accounts: %s", job_name, exc, exc_info=True)
        return

    if not accounts:
        logger.debug("%s: no enabled accounts — nothing to do", job_name)
        return

    for acct in accounts:
        if flag is not None and not acct.strategy_enabled(flag):
            logger.debug("%s: account '%s' not enrolled in %s — skipping",
                         job_name, acct.name, flag)
            continue
        try:
            await fn(acct)
        except Exception as exc:
            logger.error(
                "%s failed for account '%s': %s", job_name, acct.name, exc,
                exc_info=True,
            )

# ---------------------------------------------------------------------------
# Shared bar cache — avoids duplicate Tradier API calls when S1 and S2 both
# scan the same symbol in the same cycle.
#
# Key  : (ticker, interval)  e.g. ("AMZN", "1min")
# Value: (monotonic_timestamp, bars_list)
# TTL  : 25 s — shorter than the fastest scan interval (30 s S2) so each
#         cycle sees data no more than 25 s stale, but overlapping S1/S2 scans
#         within the same 30-second window share the same fetched bars.
# ---------------------------------------------------------------------------
_bar_cache: dict[tuple[str, str], tuple[float, list]] = {}
_BAR_CACHE_TTL: float = 25.0


async def _get_bars(client, ticker: str, interval: str, lookback_days: int) -> list:
    """
    Return intraday bars with an in-memory TTL cache.
    Both S1 and S2 scanners call this; the first fetch wins and the result is
    reused by any subsequent call within the TTL window.
    """
    key = (ticker, interval)
    entry = _bar_cache.get(key)
    if entry and (_time.monotonic() - entry[0]) < _BAR_CACHE_TTL:
        return entry[1]
    bars = await client.get_intraday_bars(ticker, interval=interval, lookback_days=lookback_days)
    if bars:
        _bar_cache[key] = (_time.monotonic(), bars)
        return bars
    return []


# ---------------------------------------------------------------------------
# Chop-day regime gate — QQQ session range vs daily ATR
#
# One shared gate for both strategies.  The QQQ daily ATR is cached for an
# hour (it barely changes intraday); the verdict is cached for 60 s so the
# S1 (60 s) and S2 (30 s) scan loops don't double the API traffic.
# ---------------------------------------------------------------------------
_atr_cache: dict[str, tuple[float, float, float]] = {}  # symbol -> (ts, atr, prev_close)
_ATR_CACHE_TTL: float = 3600.0
_chop_cache: tuple[float, bool, float] | None = None  # (monotonic_ts, is_chop, ratio)
_CHOP_CACHE_TTL: float = 60.0


async def _get_daily_atr(client, symbol: str) -> tuple[float, float]:
    """
    Return (daily ATR, previous session's close).

    Both are computed on daily bars strictly BEFORE today: today's partial
    bar must not skew the ATR baseline, and prev_close is needed so the chop
    gate can measure today's TRUE range (gap included) instead of the plain
    intraday high−low.
    """
    entry = _atr_cache.get(symbol)
    if entry and (_time.monotonic() - entry[0]) < _ATR_CACHE_TTL:
        return entry[1], entry[2]
    daily_bars = await client.get_daily_bars(symbol, days=40)
    today = date.today()
    prior = [b for b in daily_bars if b.time.date() < today]
    atr = calculate_atr(prior, settings.chop_atr_period)
    prev_close = prior[-1].close if prior else 0.0
    if atr > 0:
        _atr_cache[symbol] = (_time.monotonic(), atr, prev_close)
    return atr, prev_close


async def _chop_gate_blocks(client, qqq_bars_1m: list | None = None) -> bool:
    """
    Return True when new entries should be blocked because today is a chop day
    (QQQ session range < chop_min_range_ratio × daily ATR).

    Never blocks before chop_filter_start_time ET or on missing data.
    """
    global _chop_cache
    if not settings.chop_filter_enabled:
        return False

    # Session-progress guard: range is naturally small right after the open
    now_et = datetime.now(tz=_ET)
    h, m = map(int, settings.chop_filter_start_time.split(":"))
    if (now_et.hour, now_et.minute) < (h, m):
        return False

    if _chop_cache and (_time.monotonic() - _chop_cache[0]) < _CHOP_CACHE_TTL:
        return _chop_cache[1]

    if qqq_bars_1m is None:
        qqq_bars_1m = await _get_bars(
            client, settings.adaptive_band_symbol, interval="1min", lookback_days=1
        )
    atr, prev_close = await _get_daily_atr(client, settings.adaptive_band_symbol)
    is_chop, ratio = check_chop_regime(qqq_bars_1m, atr, prev_close=prev_close)
    _chop_cache = (_time.monotonic(), is_chop, ratio)

    if is_chop:
        logger.info(
            "[chop-gate] CHOP day — QQQ true range (incl. gap) is %.0f%% of ATR(%d) "
            "(min %.0f%%) — blocking new entries",
            ratio * 100, settings.chop_atr_period,
            settings.chop_min_range_ratio * 100,
        )
    else:
        logger.debug(
            "[chop-gate] true-range/ATR ratio %.2f ≥ %.2f — trend day, entries allowed",
            ratio, settings.chop_min_range_ratio,
        )
    return is_chop


async def _symbol_energy_blocks(
    client, ticker: str, bars_1m: list,
    check_floor: bool = True, check_ceiling: bool = False,
) -> bool:
    """
    Per-symbol range gates — both computed from the same ratio:

      ratio = symbol's session TRUE range (incl. gap) / its own daily ATR(14)

    FLOOR   (check_floor):   ratio < energy_min_range_ratio → not "in play",
              a range-less symbol has no fuel for continuation (SHOP/SMCI/F).
    CEILING (check_ceiling): ratio > energy_max_range_ratio → "too hot",
              post-event names at 3-4× ATR whip option premium ±25% per
              candle (HOOD, Jul 16-17: −$153 across two days).

    Never blocks on missing data.
    """
    try:
        atr, prev_close = await _get_daily_atr(client, ticker)
    except Exception:
        return False
    if atr <= 0 or not bars_1m:
        return False
    is_flat, ratio = check_chop_regime(
        bars_1m, atr,
        min_ratio=settings.energy_min_range_ratio,
        prev_close=prev_close,
    )
    if check_floor and is_flat:
        logger.info(
            "[energy-gate][%s] Symbol not in play — true range %.0f%% of its "
            "own ATR(14) (min %.0f%%) — blocking entry",
            ticker, ratio * 100, settings.energy_min_range_ratio * 100,
        )
        return True
    if (
        check_ceiling
        and settings.energy_max_range_ratio > 0
        and ratio > settings.energy_max_range_ratio
    ):
        logger.info(
            "[vol-ceiling][%s] Symbol too hot — true range %.0f%% of its own "
            "ATR(14) (max %.0f%%) — post-event whip risk, blocking entry",
            ticker, ratio * 100, settings.energy_max_range_ratio * 100,
        )
        return True
    return False


# ── Entry placement lock (Fix #1: MAX_OPEN_TRADES race) ──────────────────
# Serialises the final "re-check count → place buy order → commit" step so
# that concurrent per-symbol scans cannot all slip through the global cap
# check simultaneously.  The lock is held for only ~1 API round-trip per
# entry, so it does not meaningfully slow down the scan.
_entry_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

async def _get_daily_pnl(db, acct: AccountView | None = None) -> float:
    """
    Return today's combined P&L for the MAX_DAILY_LOSS gate (G2).

    Scoped to `acct` when one is given: each account carries its own daily
    loss budget, so a bad day in one account cannot halt trading in another.
    `acct=None` (or the legacy .env view) counts every trade, unchanged.

    = realized P&L from closed trades today
    + worst-case unrealized from open trades today
      (treating each open trade as if it exits at its configured stop price)

    Using the stop price as the unrealized floor is conservative but correct:
    it prevents new entries while existing positions are already so underwater
    that letting them run to their stops would breach the daily cap.  This
    closed the loophole where two trades could enter simultaneously, each
    seeing only realized P&L and both bypassing the cap.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )

    # ── Realized P&L (closed trades) ────────────────────────────────────────
    closed_result = await db.execute(_scope(
        select(sqlfunc.sum(Trade.pnl)).where(
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_time >= today_start,
        ), acct)
    )
    realized = float(closed_result.scalar() or 0)

    # ── Worst-case unrealized (open trades today at stop level) ─────────────
    open_result = await db.execute(_scope(
        select(Trade).where(
            Trade.status == TradeStatus.OPEN,
            Trade.entry_time >= today_start,
        ), acct)
    )
    open_trades = open_result.scalars().all()
    unrealized_floor = sum(
        (t.stop_price - t.entry_price) * (t.remaining_qty or t.quantity) * 100
        for t in open_trades
        if t.stop_price and t.entry_price
    )

    return realized + unrealized_floor


async def _get_symbol_losses_today(db, ticker: str, acct: AccountView | None = None) -> int:
    """Count today's losing (PnL < 0) closed trades for a symbol, per account."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_scope(
        select(sqlfunc.count(Trade.id)).where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.pnl < 0,
            Trade.exit_time >= today_start,
        ), acct)
    )
    return int(result.scalar() or 0)


async def _get_symbol_trades_today(db, ticker: str, acct: AccountView | None = None) -> int:
    """Count all entries (open + closed) on a symbol today, per account."""
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_scope(
        select(sqlfunc.count(Trade.id)).where(
            Trade.symbol == ticker,
            Trade.entry_time >= today_start,
        ), acct)
    )
    return int(result.scalar() or 0)


async def _get_recent_bad_exit(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    Return the most recent STOP or VWAP_BREAK exit on this symbol within
    the cooldown window, or None if there is none.
    """
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.cooldown_minutes
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            # QUICK_LOSS + STRUCT_EXIT added Jul 27 2026: S2 quick-lossed ORCL
            # at 14:58 and S1 re-entered the SAME symbol 8 minutes later
            # (#197 -> #198) because this list ignored both reasons.  S2's
            # own cooldown already counted them; S1's did not.
            Trade.exit_reason.in_([
                ExitReason.STOP, ExitReason.VWAP_BREAK, ExitReason.MANUAL,
                ExitReason.QUICK_LOSS, ExitReason.STRUCT_EXIT,
            ]),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_recent_trend_reversal(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    Return the most recent TREND_REVERSAL exit on this symbol within the
    trend_reversal_cooldown_minutes window, or None if there is none.

    Separate from _get_recent_bad_exit so TREND_REVERSAL can have its own
    (typically shorter) cooldown without affecting STOP/VWAP_BREAK logic.
    """
    if settings.trend_reversal_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.trend_reversal_cooldown_minutes
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason == ExitReason.TREND_REVERSAL,
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_recent_tp_exit(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    Return the most recent TP1, TP2 or TRAILING_STOP exit on this symbol
    within the tp_cooldown_minutes window, or None if there is none.
    (TRAILING_STOP is a profitable exit — same "move is exhausted" logic.)

    After a profitable exit the move is typically exhausted — immediately
    re-entering the same direction is chasing momentum that has already
    played out.  This gate enforces a short pause before the next entry.
    Set tp_cooldown_minutes=0 to disable.
    """
    if settings.tp_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.tp_cooldown_minutes
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_(
                [ExitReason.TP1, ExitReason.TP2, ExitReason.TRAILING_STOP]
            ),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_tp_exit_for_chase_check(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    Return the most recent TP1/TP2/TRAILING_STOP exit for this symbol today,
    used to enforce the same-direction price-chase guard.

    Unlike _get_recent_tp_exit (which uses a rolling 30-min window), this
    looks at the full trading session so that a trade entered 90 min after a
    TP is still blocked if it's chasing the same extended move.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_(
                [ExitReason.TP1, ExitReason.TP2, ExitReason.TRAILING_STOP]
            ),
            Trade.exit_time >= today_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_s2_tp_exit_for_chase_check(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    Return the most recent PROFITABLE S2 exit for this symbol today.

    Used to enforce the TP price-chase guard: if the option has appreciated
    significantly since the last profitable exit, the move is likely extended
    and re-entering at the higher price offers poor R/R.

    Only profitable exits qualify (exit_price > entry_price).  A TRAILING_STOP
    that closed below entry (breakeven stop hit at a loss) does NOT trigger the
    guard — that trade failed and the price may have reset to a fair level.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.strategy_name == "ema_cross",
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_reason.in_([ExitReason.TP2, ExitReason.TRAILING_STOP]),
            Trade.exit_price > Trade.entry_price,   # profitable exits only
            Trade.exit_time >= today_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_s2_recent_bad_exit(db, ticker: str, acct: AccountView | None = None) -> Trade | None:
    """
    S2 cooldown: return the most recent STOP or EMA_CROSS exit on this symbol
    within s2_cooldown_minutes, or None.
    EMA_CROSS is included because a signal reversal on a stock that just crossed
    back usually means the setup is no longer clean.
    """
    if settings.s2_cooldown_minutes <= 0:
        return None
    cooldown_start = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.s2_cooldown_minutes
    )
    result = await db.execute(_scope(
        select(Trade)
        .where(
            Trade.symbol == ticker,
            Trade.strategy_name == "ema_cross",
            Trade.status == TradeStatus.CLOSED,
            # STRUCT_EXIT + QUICK_LOSS added Jul 14 2026: both are loss exits
            # and belong in the cooldown — SMCI re-entered 8 min after a
            # STRUCT_EXIT loss because this list predated those reasons.
            Trade.exit_reason.in_([
                ExitReason.STOP, ExitReason.EMA_CROSS, ExitReason.TRAILING_STOP,
                ExitReason.STRUCT_EXIT, ExitReason.QUICK_LOSS,
            ]),
            Trade.exit_time >= cooldown_start,
        )
        .order_by(Trade.exit_time.desc())
        .limit(1), acct)
    )
    return result.scalar_one_or_none()


async def _get_s2_daily_pnl(db, acct: AccountView | None = None) -> float:
    """
    Sum of realized S2 P&L for the current UTC day.
    Only counts closed ema_cross trades with a non-null pnl — open positions
    are excluded (they haven't locked in a loss yet).
    Used by the S2-specific daily loss circuit breaker (s2_max_daily_loss).
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    result = await db.execute(_scope(
        select(sqlfunc.sum(Trade.pnl)).where(
            Trade.strategy_name == "ema_cross",
            Trade.status == TradeStatus.CLOSED,
            Trade.pnl.is_not(None),
            Trade.exit_time >= today_start,
        ), acct)
    )
    total = result.scalar()
    return float(total) if total is not None else 0.0


# ---------------------------------------------------------------------------
# Entry scanner (S1 — VWAP pullback)
# ---------------------------------------------------------------------------

async def scan_for_entries() -> None:
    """S1 entry scan — runs once per enabled account enrolled in S1."""
    # Global master switch first — an account toggle can only narrow it.
    # OFF stops NEW entries only; manage_open_trades still runs for open S1
    # positions (same semantics as S2 / PS / a disabled account).
    if not settings.s1_enabled:
        return
    if not is_in_trading_window():
        return
    await _for_each_account("scan_for_entries", "s1_enabled", _scan_for_entries_account)


async def _scan_for_entries_account(acct: AccountView) -> None:
    client = get_tradier_client(acct)
    _t = _tag(client)

    async with AsyncSessionLocal() as db:
        # ── G2: Daily P&L guard (this account's own budget) ──────────────────
        daily_pnl = await _get_daily_pnl(db, acct)
        if daily_pnl <= -abs(_s(client, "max_daily_loss")):
            logger.info(
                "%sDaily loss limit reached ($%.2f) — no new entries today",
                _t, daily_pnl,
            )
            return

        # ── G3: Max concurrent open trades ───────────────────────────────────
        open_count_result = await db.execute(_scope(
            # S3 (stocks, Moomoo-data engine) manages its own slots —
            # it must not consume S1/S2 option-trade capacity.
            select(Trade).where(Trade.status == TradeStatus.OPEN,
                                Trade.strategy_name != "S3"), acct)
        )
        open_trades_all = open_count_result.scalars().all()
        if len(open_trades_all) >= _s(client, "max_open_trades"):
            logger.debug(
                "%sMax open trades (%d) reached — skipping scan",
                _t, _s(client, "max_open_trades"),
            )
            return

        # Active symbols enrolled in S1
        sym_result = await db.execute(
            select(Symbol).where(Symbol.active == True, Symbol.s1_enabled == True)  # noqa: E712
        )
        symbols = sym_result.scalars().all()

    # ── Fetch QQQ 1-min bars once — used for both adaptive band AND regime gate ──
    # Single API call serves two purposes:
    #   1. Adaptive VWAP band: how extended is QQQ from VWAP? → widens entry band
    #   2. L5 regime gate: which side of VWAP is QQQ on? → blocks counter-trend entries
    # This replaces the old SPY 15-min EMA regime fetch (separate API call, 5-min lag).
    qqq_bars_1m: list = []
    try:
        qqq_bars_1m = await _get_bars(
            client, settings.adaptive_band_symbol, interval="1min", lookback_days=1
        )
    except Exception as exc:
        logger.warning(
            "[QQQ] Could not fetch bars: %s — adaptive band uses normal, regime neutral",
            exc,
        )

    # Adaptive VWAP band
    band_pct = settings.vwap_band_pct
    band_label = "normal (adaptive off)"
    qqq_dist_signed = 0.0
    if settings.adaptive_band_enabled and qqq_bars_1m:
        band_pct, band_label, qqq_dist_signed = get_adaptive_vwap_band(qqq_bars_1m)

    # L5 regime — QQQ VWAP position (real-time, no extra API call)
    regime = get_regime_from_vwap(qqq_bars_1m)

    logger.info(
        "%s[adaptive-band] %s → band=%.2f%% (%s) | regime=%s",
        _t, settings.adaptive_band_symbol, band_pct * 100, band_label, regime.upper(),
    )

    # ── Chop-day gate — skip the whole scan cycle on range-less days ────────
    # Pullback-continuation setups need follow-through; when QQQ has covered
    # less than chop_min_range_ratio of its normal daily range there is none.
    if await _chop_gate_blocks(client, qqq_bars_1m):
        return

    # Run all symbol scans concurrently — each gets its own DB session so
    # there is no SQLAlchemy session-sharing across coroutines.
    # Semaphore caps at 3 concurrent scans — reduces simultaneous TCP connections
    # to Tradier, which helps avoid ReadTimeout bursts during early-session load.
    sem = asyncio.Semaphore(3)

    async def _scan_one(ticker: str) -> None:
        async with sem:
            async with AsyncSessionLocal() as sym_db:
                try:
                    await _attempt_entry(sym_db, client, ticker, regime, band_pct, qqq_dist_signed)
                except Exception as exc:
                    logger.error(
                        "%sscan_for_entries error for %s: %s",
                        _t, ticker, exc, exc_info=True,
                    )

    await asyncio.gather(*[_scan_one(sym.ticker) for sym in symbols])


async def _await_entry_fill(
    client, ticker: str, order, order_type_str: str,
    order_price: float, ask_price: float,
    option_symbol: str, qty: int,
) -> float | None:
    """
    Wait for a placed buy order to fill and return the actual entry price.

    Shared by S1 and PUT-scalp entries so the safety-critical fill logic has
    exactly ONE implementation:
      - limit orders: poll to timeout, cancel on timeout, then re-check the
        final status — a fill can RACE the cancel (ghost-trade guard: an
        untracked live position must never be left behind)
      - market orders: verify not rejected before the caller writes to the DB
    Returns None when no position was (safely) opened.
    """
    if order_type_str == "limit":
        logger.info(
            "[%s] Limit order %s placed: %s x%d @ $%.2f (ask $%.2f, saving $%.2f/contract)",
            ticker, order.order_id, option_symbol, qty, order_price, ask_price,
            ask_price - order_price,
        )
        # Poll for fill — cancel and skip if not filled within timeout.
        # We check every 2 seconds so the total wait is at most
        # limit_order_timeout_seconds (default 15 s).
        filled   = False
        deadline = datetime.now(tz=timezone.utc) + timedelta(
            seconds=settings.limit_order_timeout_seconds
        )
        while datetime.now(tz=timezone.utc) < deadline:
            await asyncio.sleep(2)
            try:
                status_data = await client.get_order_status(order.order_id)
                status_str  = (status_data.get("status") or "").lower()
            except Exception:
                status_str  = "unknown"
            if status_str == "filled":
                filled = True
                break
            if status_str in ("rejected", "canceled", "cancelled"):
                logger.info(
                    "[%s] Limit order %s was %s — aborting entry",
                    ticker, order.order_id, status_str.upper(),
                )
                return None
        if not filled:
            logger.info(
                "[%s] Limit order %s not filled within %ds — canceling",
                ticker, order.order_id, settings.limit_order_timeout_seconds,
            )
            try:
                await client.cancel_order(order.order_id)
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to cancel limit order %s: %s", ticker, order.order_id, exc
                )

            # ── Post-cancel race check (Fix #3) ──────────────────────────────
            # A fill can race with the cancel: Tradier may process the fill
            # before the cancel, leaving a real live position with no Trade record.
            # Check the final order status to catch this window.
            try:
                post_status_data = await client.get_order_status(order.order_id)
                post_status_str  = (post_status_data.get("status") or "").lower()
            except Exception:
                post_status_str  = "unknown"

            if post_status_str == "filled":
                # Fill won the race — record the trade normally below.
                logger.info(
                    "[%s] Limit order %s filled during cancel window — "
                    "recording trade to avoid untracked live position.",
                    ticker, order.order_id,
                )
                filled = True
            else:
                logger.info(
                    "[%s] Limit order %s confirmed %s — skipping entry.",
                    ticker, order.order_id, post_status_str.upper(),
                )
                return None

        # Use the actual fill price — the limit may have been improved by the
        # market-maker (common when placing at the mid).  Stop/TP levels will
        # be recalculated from this value so they reflect real risk.
        actual_fill = await client.get_fill_price(order.order_id)
        if actual_fill and actual_fill > 0:
            if abs(actual_fill - order_price) > 0.005:
                logger.info(
                    "[%s] Limit filled at $%.2f (limit was $%.2f, diff %+.2f)",
                    ticker, actual_fill, order_price, actual_fill - order_price,
                )
            return actual_fill
        return order_price   # fallback: fill not yet available

    # Market order — verify it was not rejected before the caller writes to the DB.
    try:
        order_status_data = await client.get_order_status(order.order_id)
        order_status_str  = (order_status_data.get("status") or "").lower()
    except Exception:
        order_status_str  = "unknown"
    if order_status_str in ("rejected", "canceled", "cancelled"):
        logger.error(
            "[%s] Buy order %s was %s — aborting entry, no DB record created.",
            ticker, order.order_id, order_status_str.upper(),
        )
        return None
    return ask_price


async def _attempt_entry(
    db, client, ticker: str, regime: str, band_pct: float,
    qqq_dist_signed: float = 0.0,
) -> None:
    """
    Run the full 8-gate entry stack for one symbol.
    Returns early (no trade) at the first failed gate.

    Every DB gate below is scoped to the client's account: two accounts are
    independent books, so an open AMZN position (or a cooldown) in one must
    not block the same signal in another.
    """
    acct = _acct(client)
    # ── G4: Per-symbol open trade (in THIS account) ──────────────────────
    existing = await db.execute(_scope(
        select(Trade).where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.OPEN,
        ), acct)
    )
    if existing.scalars().first():
        return

    # ── G5: Per-symbol daily loss cap ────────────────────────────────────
    if settings.max_losses_per_symbol_per_day > 0:
        sym_losses = await _get_symbol_losses_today(db, ticker, acct)
        if sym_losses >= settings.max_losses_per_symbol_per_day:
            logger.info(
                "[%s] Per-symbol loss cap reached (%d/%d losing trades today) — "
                "no more entries on this symbol today",
                ticker, sym_losses, settings.max_losses_per_symbol_per_day,
            )
            return

    # ── G5b: Per-symbol total daily trade cap ────────────────────────────
    # Prevents the same 2-3 symbols from monopolising all MAX_OPEN_TRADES slots
    # all day while 14 other scanned symbols never get a look.
    # Once a symbol has had its quota, it is skipped for the rest of the day
    # and the slot opens up for a fresh symbol.
    if settings.max_trades_per_symbol_per_day > 0:
        sym_trades = await _get_symbol_trades_today(db, ticker, acct)
        if sym_trades >= settings.max_trades_per_symbol_per_day:
            logger.debug(
                "[%s] Daily trade cap reached (%d/%d trades today) — "
                "skipping to allow other symbols a turn",
                ticker, sym_trades, settings.max_trades_per_symbol_per_day,
            )
            return

    # ── G6: Cooldown after STOP / VWAP_BREAK ────────────────────────────
    recent_bad = await _get_recent_bad_exit(db, ticker, acct)
    if recent_bad:
        logger.info(
            "[%s] Cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_bad.exit_reason.value,
            recent_bad.exit_time.strftime("%H:%M UTC"),
            settings.cooldown_minutes,
        )
        return

    # ── G6b: Cooldown after TREND_REVERSAL ───────────────────────────────
    recent_tr = await _get_recent_trend_reversal(db, ticker, acct)
    if recent_tr:
        logger.info(
            "[%s] TREND_REVERSAL cooldown active — last exit at %s (%d-min window)",
            ticker,
            recent_tr.exit_time.strftime("%H:%M UTC"),
            settings.trend_reversal_cooldown_minutes,
        )
        return

    # ── G6c: Cooldown after TP1 / TP2 ───────────────────────────────────
    # After a profitable exit the move is typically exhausted.  Re-entering
    # immediately risks chasing a spent momentum spike (e.g. NVDA TP2 at
    # 1:53 PM → new CALL at 1:57 PM right at the spike top).
    recent_tp = await _get_recent_tp_exit(db, ticker, acct)
    if recent_tp:
        logger.info(
            "[%s] TP cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_tp.exit_reason.value,
            recent_tp.exit_time.strftime("%H:%M UTC"),
            settings.tp_cooldown_minutes,
        )
        return

    # ── Fetch market data (needed for Layers 1–5) ────────────────────────
    # 1-min bars: today only — VWAP resets each session
    # 15-min bars: multi-day lookback so EMA always has enough history
    bars_1m  = await _get_bars(client, ticker, interval="1min",  lookback_days=1)
    bars_15m = await _get_bars(client, ticker, interval="15min",
                               lookback_days=settings.trend_lookback_days)

    # ── Energy floor + volatility ceiling (S1 toggles) ───────────────────
    if (settings.energy_gate_s1_enabled or settings.vol_ceiling_s1_enabled) \
            and await _symbol_energy_blocks(
        client, ticker, bars_1m,
        check_floor=settings.energy_gate_s1_enabled,
        check_ceiling=settings.vol_ceiling_s1_enabled,
    ):
        return
    # Drop the in-progress 15-min bar — EMA trend / consecutive-bar confirmation
    # must only see completed bars.  (1-min bars are left as-is: VWAP wants the
    # partial bar, and L2/L3 already exclude bars_1m[-1] explicitly.)
    bars_15m = completed_bars(bars_15m, 15)

    if not bars_1m or not bars_15m:
        missing = []
        if not bars_1m:  missing.append("1-min")
        if not bars_15m: missing.append("15-min")
        logger.warning(
            "[%s] No %s bars returned from Tradier — skipping entry (likely timeout or market closed)",
            ticker, " + ".join(missing),
        )
        return

    if len(bars_1m) < settings.bounce_bars_required + 3:
        return   # too few bars to run any layer

    # ── L1: Indicator signal (trend + VWAP pullback) ─────────────────────
    signal = check_entry_signal(bars_1m, bars_15m, ticker=ticker, band_pct=band_pct)
    if not signal:
        return

    direction = signal.direction
    vwap      = signal.vwap

    # ── L1.5: Direction-aware band gate ───────────────────────────────────
    # The adaptive band widens on gap-up days (QQQ above VWAP) to let CALL
    # entries find pullbacks on strongly trending days.  That widening must
    # NOT benefit PUT entries on the same days — a stock that only qualifies
    # for a PUT because the gap-up band stretched from 0.9% to 1.8% is
    # fighting the overall market direction, and these trades reliably lose
    # (NVDA PUT, SPY PUT on gap-up day are the canonical examples).
    #
    # Rule: if direction is PUT and QQQ is above VWAP (gap-up, qqq_dist_signed > 0),
    # re-verify the signal against the NORMAL band.  If it only passes the
    # wider band but not the normal band → block the entry.
    # Same logic inverted for CALL on gap-down days.
    if band_pct > settings.vwap_band_pct:   # wider/relaxed band is active
        if direction == "PUT" and qqq_dist_signed > 0:
            # Gap-up day — re-check PUT against normal band
            strict_signal = check_entry_signal(
                bars_1m, bars_15m, ticker=ticker,
                band_pct=settings.vwap_band_pct,
            )
            if not strict_signal:
                logger.info(
                    "[%s] [L1.5] PUT blocked — only qualifies under wider gap-up band "
                    "(%.2f%%), not normal band (%.2f%%). QQQ is %.2f%% above VWAP. "
                    "Fighting the trend on a gap-up day.",
                    ticker, band_pct * 100, settings.vwap_band_pct * 100,
                    qqq_dist_signed * 100,
                )
                return

        elif direction == "CALL" and qqq_dist_signed < 0:
            # Gap-down day — re-check CALL against normal band
            strict_signal = check_entry_signal(
                bars_1m, bars_15m, ticker=ticker,
                band_pct=settings.vwap_band_pct,
            )
            if not strict_signal:
                logger.info(
                    "[%s] [L1.5] CALL blocked — only qualifies under wider gap-down band "
                    "(%.2f%%), not normal band (%.2f%%). QQQ is %.2f%% below VWAP. "
                    "Fighting the trend on a gap-down day.",
                    ticker, band_pct * 100, settings.vwap_band_pct * 100,
                    abs(qqq_dist_signed) * 100,
                )
                return

    # ── L2: Multi-bar bounce confirmation ────────────────────────────────
    if not check_bounce_confirmation(bars_1m, direction, vwap):
        return

    # ── L3: Momentum candle ───────────────────────────────────────────────
    if not check_momentum_candle(bars_1m, direction, ticker=ticker):
        return

    # ── L4: Intraday VWAP slope ───────────────────────────────────────────
    if not check_vwap_slope(bars_1m, direction):
        return

    # ── L5: Market regime gate ────────────────────────────────────────────
    # SPY alone is sufficient to block a trade.  The old "double-confirmation"
    # logic (requiring BOTH SPY bearish AND stock bearish) was too permissive:
    # a brief divergence where the stock EMA ticked bullish while SPY was
    # falling allowed CALL entries on bear days — every one of which lost.
    #
    # Regime is now derived from QQQ's VWAP position (real-time, 1-min resolution)
    # rather than SPY's 15-min EMA.  No circular reference — QQQ is always the
    # proxy regardless of which ticker is being scanned, including SPY itself.
    if settings.regime_gate_enabled:
        if direction == "CALL" and regime == "bearish":
            logger.info(
                "[%s] [L5] Regime gate: QQQ BEARISH (below VWAP) — blocking CALL",
                ticker,
            )
            return
        if direction == "PUT" and regime == "bullish":
            logger.info(
                "[%s] [L5] Regime gate: QQQ BULLISH (above VWAP) — blocking PUT",
                ticker,
            )
            return
        if regime != "neutral":
            logger.debug(
                "[%s] [L5] QQQ %s — %s allowed (regime not opposing)",
                ticker, regime.upper(), direction,
            )

    logger.info(
        "[%s] All Layers 1–5 passed: %s  price=%.2f  vwap=%.2f",
        ticker, direction, signal.current_price, vwap,
    )

    # ── Contract selection ────────────────────────────────────────────────
    expirations = await client.get_option_expirations(ticker)
    if not expirations:
        return

    # Prefer next-week (or later) expiry over 0DTE to avoid same-day theta crush.
    # If today is the only available expiry we fall back to it rather than skip.
    today_str  = date.today().isoformat()
    non_0dte   = [e for e in expirations if e > today_str]
    expiration = non_0dte[0] if non_0dte else expirations[0]
    logger.debug(
        "[%s] Expiry selected: %s (0DTE available: %s, non-0DTE options: %s)",
        ticker, expiration, today_str in expirations, non_0dte,
    )

    full_chain = await client.get_options_chain(ticker, expiration)
    calls = [o for o in full_chain if o.option_type == "call"]
    puts  = [o for o in full_chain if o.option_type == "put"]
    side_chain = calls if direction == "CALL" else puts

    if not side_chain:
        return

    # Primary filter: delta range + liquidity + positive ask
    eligible = [
        o for o in side_chain
        if (o.volume or 0) >= settings.option_min_volume
        and o.ask > 0
        and settings.option_min_delta <= abs(o.delta or 0) <= settings.option_max_delta
    ]

    # Fallback 1: relax delta range, keep liquidity + positive ask
    if not eligible:
        logger.debug(
            "[%s] No contracts passed delta filter (%.2f–%.2f); relaxing delta.",
            ticker, settings.option_min_delta, settings.option_max_delta,
        )
        eligible = [
            o for o in side_chain
            if (o.volume or 0) >= settings.option_min_volume and o.ask > 0
        ]

    # Fallback 2: drop volume requirement too — at least something tradeable
    if not eligible:
        eligible = [o for o in side_chain if o.ask > 0]
    if not eligible:
        return

    price    = signal.current_price
    selected = min(eligible, key=lambda o: abs(o.strike - price))

    # ── Contract quality floors (Jul 14 2026) ─────────────────────────────
    # Sub-$1 contracts quantize in 1-cent ticks (a structural stop can be
    # 2 ticks wide) and their spreads run 10-20% of mid.  S1 previously had
    # NO spread check at all (SOFI #137 entered a $0.14 contract).
    _sel_mid = selected.mid if (selected.mid and selected.mid > 0) else selected.ask
    if settings.option_min_premium > 0 and _sel_mid < settings.option_min_premium:
        logger.info(
            "[%s] Contract too cheap — mid $%.2f < min premium $%.2f "
            "(penny quantization / spread noise) — skipping",
            ticker, _sel_mid, settings.option_min_premium,
        )
        return
    if not check_option_spread(selected.bid, selected.ask,
                               settings.s2_max_spread_pct, ticker=ticker):
        return  # spread too wide — friction eats the edge (logged inside)

    # ── L6: IV filter ─────────────────────────────────────────────────────
    atm_iv = client.get_atm_iv(full_chain, direction, price)
    if atm_iv is not None:
        if atm_iv > settings.iv_max_threshold:
            logger.info(
                "[%s] [L6] IV filter: ATM IV %.1f%% exceeds threshold %.1f%% — "
                "premium too expensive, skipping",
                ticker, atm_iv * 100, settings.iv_max_threshold * 100,
            )
            return
        logger.debug(
            "[%s] [L6] ATM IV %.1f%% — within threshold (%.1f%%), OK",
            ticker, atm_iv * 100, settings.iv_max_threshold * 100,
        )

    # ── Position sizing ───────────────────────────────────────────────────
    ask_price = round(selected.ask, 2)
    mid_price = round(selected.mid, 2) if selected.mid and selected.mid > 0 else ask_price

    # When limit orders are enabled, size and enter at the mid-price.
    # This saves half the spread on every entry (e.g. bid $2.40 / ask $2.50
    # → limit at $2.45 saves $0.05/contract = $1 on a 20-contract position).
    if settings.use_limit_orders and mid_price > 0:
        order_price    = mid_price
        order_type_str = "limit"
    else:
        order_price    = ask_price
        order_type_str = "market"

    # ── G_chase: TP price-chase guard ────────────────────────────────────────
    # If this symbol had a profitable TP exit today in the same direction,
    # block re-entry when the new option price is significantly higher than
    # the previous entry price.  Prevents chasing a move that has already
    # played out (e.g. SOFI PUT $0.17 → TP2 → re-enter PUT at $0.36 = +112%).
    # Different direction is always allowed (fresh setup, no chase concern).
    if settings.tp_chase_pct > 0:
        tp_chase_trade = await _get_tp_exit_for_chase_check(db, ticker, acct)
        if (
            tp_chase_trade
            and tp_chase_trade.direction.value == direction
            and tp_chase_trade.entry_price > 0
        ):
            price_ratio = order_price / tp_chase_trade.entry_price
            if price_ratio > (1 + settings.tp_chase_pct):
                logger.info(
                    "[%s] TP chase guard — %s new entry $%.2f is %.0f%% above last TP "
                    "entry $%.2f (max %.0f%%) — chasing an extended move, skipping",
                    ticker, direction,
                    order_price, (price_ratio - 1) * 100,
                    tp_chase_trade.entry_price, settings.tp_chase_pct * 100,
                )
                return

    cost_per_contract = order_price * 100
    if cost_per_contract <= 0:
        return

    # ── Structural (chart-based) levels ───────────────────────────────────
    # Stop anchored to the setup's invalidation point (below pullback low /
    # VWAP for a CALL), target at the session swing high/low, translated to
    # option prices via the contract's delta.  Enforces a minimum underlying
    # reward/risk — setups with no room to the target are skipped entirely.
    struct = None
    if settings.structural_levels_enabled:
        stop_u, target_u = get_structural_stop_target(
            bars_1m, direction, signal.vwap,
            buffer_pct=settings.struct_stop_buffer_pct,
            pullback_lookback=settings.struct_pullback_lookback,
        )
        struct = compute_structural_levels(
            direction=direction,
            option_entry=order_price,
            delta=selected.delta,
            underlying_entry=signal.current_price,
            stop_underlying=stop_u,
            target_underlying=target_u,
        )
        if not struct.ok:
            if struct.skip_reason == "fallback":
                logger.info(
                    "[%s] Structural levels unavailable (no delta) — "
                    "using percentage levels", ticker,
                )
                struct = None
            else:
                logger.info(
                    "[%s] Structural gate blocked %s entry — %s",
                    ticker, direction, struct.skip_reason,
                )
                return
        else:
            logger.info(
                "[%s] Structural levels: stop_u=%.2f target_u=%.2f R/R=%.2f "
                "→ option SL=%.2f (−%.0f%%) TP=%.2f",
                ticker, struct.stop_underlying, struct.target_underlying,
                struct.reward_risk, struct.stop_price, struct.risk_pct * 100,
                struct.tp_price,
            )

    # ── Fixed-dollar risk sizing + premium budget cap ─────────────────────
    # Risk-based qty:   risk_per_trade / (premium lost if the stop fires)
    # Budget-based qty: amount_per_trade / cost of one contract
    # Final qty is the smaller of the two.  If even 1 contract exceeds either
    # limit, SKIP the trade — never "round up" past the configured risk.
    # With structural levels the risk fraction is the ACTUAL stop distance,
    # so every trade risks ~risk_per_trade dollars at its structural stop.
    risk_frac = struct.risk_pct if struct else settings.stop_loss_pct
    _amount_per_trade = _s(client, "amount_per_trade")
    _risk_per_trade   = _s(client, "risk_per_trade")
    budget_qty = int(_amount_per_trade / cost_per_contract)
    if _risk_per_trade > 0 and risk_frac > 0:
        risk_per_contract = cost_per_contract * risk_frac
        risk_qty = int(_risk_per_trade / risk_per_contract)
        qty = min(risk_qty, budget_qty)
    else:
        qty = budget_qty

    if qty < 1:
        logger.info(
            "%s[%s] Skipping — 1 contract @ $%.2f would exceed limits "
            "(premium $%.0f > budget $%.0f, or risk $%.0f > risk/trade $%.0f)",
            _tag(client), ticker, order_price,
            cost_per_contract, _amount_per_trade,
            cost_per_contract * risk_frac, _risk_per_trade,
        )
        return

    # ── Place order (inside lock to prevent MAX_OPEN_TRADES race) ────────
    # Re-check the global open count AND place the buy order while holding
    # the process-wide entry lock.  asyncio.gather() launches all per-symbol
    # scans concurrently; without this lock, multiple scans can simultaneously
    # pass the initial G3 check and each open a trade, violating the cap.
    # The lock is held only for the API call (~100-200 ms), then released.
    order: object = None
    async with _entry_lock:
        open_recheck = await db.execute(_scope(
            select(sqlfunc.count(Trade.id)).where(Trade.status == TradeStatus.OPEN,
                                                  Trade.strategy_name != "S3"), acct)
        )
        if int(open_recheck.scalar() or 0) >= _s(client, "max_open_trades"):
            logger.debug(
                "%s[%s] Max open trades (%d) reached (re-check inside lock) — skipping",
                _tag(client), ticker, _s(client, "max_open_trades"),
            )
            return

        # Place the order while still holding the lock so the slot is reserved
        # before any other concurrent scan can slip through the count check.
        # The lock covers only the API placement call (~100-200 ms); the fill
        # poll loop below runs after the lock is released.
        if order_type_str == "limit":
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="limit",
                limit_price=order_price,
            )
        else:
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="market",
            )
    # _entry_lock released — other scans can now proceed

    entry_price = await _await_entry_fill(
        client, ticker, order, order_type_str, order_price, ask_price,
        selected.symbol, qty,
    )
    if entry_price is None:
        return

    # Recompute levels from the ACTUAL fill price.  Structural: keep the same
    # underlying anchor levels, re-translate to option prices via delta.
    if struct:
        refit = compute_structural_levels(
            direction=direction,
            option_entry=entry_price,
            delta=selected.delta,
            underlying_entry=signal.current_price,
            stop_underlying=struct.stop_underlying,
            target_underlying=struct.target_underlying,
        )
        if refit.ok:
            levels = {
                "stop_price": refit.stop_price,
                "tp1_price": refit.tp_price,
                "tp2_price": refit.tp_price,
            }
        else:
            # Fill moved enough to break the refit — keep pre-fill levels
            levels = {
                "stop_price": struct.stop_price,
                "tp1_price": struct.tp_price,
                "tp2_price": struct.tp_price,
            }
    else:
        levels = compute_trade_levels(entry_price, direction)

    trade = Trade(
        symbol=ticker,
        option_symbol=selected.symbol,
        direction=signal.direction,
        strategy_name="vwap_pullback",
        account_id=_aid(client),
        tradier_order_id=order.order_id,
        quantity=qty,
        remaining_qty=qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        underlying_entry=signal.current_price,
        vwap_at_entry=signal.vwap,
        original_stop_price=levels["stop_price"],   # entry-time snapshot for honest exit labels
        **levels,
    )
    db.add(trade)
    await db.commit()
    logger.info(
        "%s[%s] Trade OPENED: %s %s x%d @ $%.2f  SL=%.2f  TP=%.2f  "
        "(premium $%.0f, risk-at-stop $%.0f)",
        _tag(client), ticker, direction, selected.symbol, qty, entry_price,
        levels["stop_price"], levels["tp2_price"],
        qty * cost_per_contract,
        qty * (entry_price - levels["stop_price"]) * 100,
    )

    # ── Broker-side resting stop order ───────────────────────────────────
    # Placed as a GTC sell-to-close stop at the trade's stop_price.
    # Can coexist with a TP limit order — _close_trade cancels both before
    # placing any bot-initiated exit, so double-sell is not a risk.
    if settings.broker_stop_enabled:
        await _place_broker_stop(db, client, trade)

    # ── Broker-side resting TP limit order ───────────────────────────────
    # Tradier rejects a second resting sell on the same contracts (live
    # evidence: SOFI #137's broker TP was REJECTED 17s after placement while
    # the disaster stop was resting).  When a stop is resting, skip the TP —
    # the bot-side TP check remains fully active.  A true OCO order class
    # would be required to rest both simultaneously.
    if settings.broker_tp_enabled:
        if trade.stop_order_id:
            logger.info(
                "[%s] Broker TP skipped — resting disaster stop %s already "
                "reserves the contracts (bot-side TP remains active)",
                ticker, trade.stop_order_id,
            )
        else:
            await _place_broker_tp(db, client, trade)


# ---------------------------------------------------------------------------
# Entry scanner — PUT Scalp mode ("PS", Jul 23 2026)
#
# Momentum-short experiment, independent of the S1/S2 PUT kill switches.
# The Jul 22-23 post-mortem showed pullback-style PUT entries are adversely
# selected (no fill on trend days, filled-then-bounced on chop days).  PS
# enters on BREAKDOWN state instead: Stock Trend (completed-bar 15-min EMA)
# bearish AND Thesis (underlying below session VWAP beyond the exit band)
# — the same two signals SIGNAL_FADE uses to exit, required at entry.
# Tight brackets (TP +8% / SL −7%), half size, own spread gate.
# ---------------------------------------------------------------------------

async def scan_for_put_scalp() -> None:
    """PS entry scan — once per enabled account enrolled in PUT Scalp mode."""
    # Global master switch first: PUT_SCALP_ENABLED=0 kills PS everywhere,
    # regardless of any account's own toggle (§6b: "if PS bleeds, kill
    # PUT_SCALP_ENABLED alone").
    if not settings.put_scalp_enabled:
        return
    # Calendar AND clock guard (ghost-trade lesson: S2 checked the clock but
    # not the calendar and ran on a Saturday).
    if not is_in_trading_window() or not is_market_open():
        return
    await _for_each_account("scan_for_put_scalp", "put_scalp_enabled",
                            _scan_for_put_scalp_account)


async def _scan_for_put_scalp_account(acct: AccountView) -> None:
    client = get_tradier_client(acct)
    _t = _tag(client)

    async with AsyncSessionLocal() as db:
        # Daily P&L guard — shared with S1/S2, scoped to this account
        daily_pnl = await _get_daily_pnl(db, acct)
        if daily_pnl <= -abs(_s(client, "max_daily_loss")):
            return

        # PS capacity: its own small cap, ADDITIVE to the S1/S2 slots so a
        # scalp can never squeeze out a CALL entry (the CALL-only evaluation
        # must stay uncontaminated — capacity-wise too).
        open_result = await db.execute(_scope(
            select(Trade).where(Trade.status == TradeStatus.OPEN,
                                Trade.strategy_name == "put_scalp"), acct)
        )
        if len(open_result.scalars().all()) >= _s(client, "put_scalp_max_open"):
            return

        sym_result = await db.execute(
            select(Symbol).where(Symbol.active == True, Symbol.s1_enabled == True)  # noqa: E712
        )
        symbols = sym_result.scalars().all()

    # QQQ regime — never short while the market proxy is above its VWAP.
    qqq_bars_1m: list = []
    try:
        qqq_bars_1m = await _get_bars(
            client, settings.adaptive_band_symbol, interval="1min", lookback_days=1
        )
    except Exception as exc:
        logger.warning("[PS] Could not fetch QQQ bars: %s — regime neutral", exc)
    if get_regime_from_vwap(qqq_bars_1m) == "bullish":
        logger.debug("%s[PS] QQQ above VWAP — no PUT scalps this cycle", _t)
        return

    # Chop gate — breakdown continuation needs range like everything else.
    if await _chop_gate_blocks(client, qqq_bars_1m):
        return

    sem = asyncio.Semaphore(3)

    async def _scan_one(ticker: str) -> None:
        async with sem:
            async with AsyncSessionLocal() as sym_db:
                try:
                    await _attempt_put_scalp(sym_db, client, ticker)
                except Exception as exc:
                    logger.error(
                        "%sscan_for_put_scalp error for %s: %s",
                        _t, ticker, exc, exc_info=True,
                    )

    await asyncio.gather(*[_scan_one(sym.ticker) for sym in symbols])


async def _attempt_put_scalp(db, client, ticker: str) -> None:
    """PS entry stack for one symbol — state signal + quality gates + entry."""
    acct = _acct(client)
    # No open trade on this symbol IN THIS ACCOUNT, any strategy
    existing = await db.execute(_scope(
        select(Trade).where(Trade.symbol == ticker,
                            Trade.status == TradeStatus.OPEN), acct)
    )
    if existing.scalars().first():
        return

    # PS cooldown — Trend+Thesis agreement is a STATE, not an event.  Without
    # this the mode would re-enter the instant a trade exits, all day long.
    _cd_cutoff = datetime.now(tz=timezone.utc) - timedelta(
        minutes=settings.put_scalp_cooldown_minutes
    )
    recent_ps = await db.execute(_scope(
        select(Trade).where(
            Trade.symbol == ticker,
            Trade.strategy_name == "put_scalp",
            Trade.status == TradeStatus.CLOSED,
            Trade.exit_time >= _cd_cutoff,
        ), acct)
    )
    if recent_ps.scalars().first():
        logger.debug("%s[PS][%s] Cooldown active (%d min after any PS exit)",
                     _tag(client), ticker, settings.put_scalp_cooldown_minutes)
        return

    # Per-symbol daily caps — shared with S1
    if settings.max_losses_per_symbol_per_day > 0:
        if await _get_symbol_losses_today(db, ticker, acct) >= settings.max_losses_per_symbol_per_day:
            return
    if settings.max_trades_per_symbol_per_day > 0:
        if await _get_symbol_trades_today(db, ticker, acct) >= settings.max_trades_per_symbol_per_day:
            return

    bars_1m  = await _get_bars(client, ticker, interval="1min",  lookback_days=1)
    bars_15m = await _get_bars(client, ticker, interval="15min",
                               lookback_days=settings.trend_lookback_days)
    if not bars_1m or not bars_15m or len(bars_1m) < 5:
        return

    # Volatility ceiling — post-event hyper-ATR names whip premium ±25%/candle
    if await _symbol_energy_blocks(client, ticker, bars_1m,
                                   check_floor=False, check_ceiling=True):
        return

    # ── PS signal: Stock Trend AND Thesis agree on PUT ───────────────────
    # Exactly the dashboard's two signals (and SIGNAL_FADE's inverse):
    #   Trend  — completed-bar 15-min EMA bearish
    #   Thesis — underlying below session VWAP by more than the exit band
    trend = ema_direction(completed_bars(bars_15m, 15), settings.ema_period)
    if trend != "bearish":
        return
    vwap = calculate_vwap(session_bars(bars_1m))
    if vwap <= 0:
        return
    last_price = bars_1m[-1].close
    _band = vwap * max(settings.vwap_exit_band_pct, 0.003)
    if last_price >= vwap - _band:
        return  # thesis not bearish (at/above the VWAP band)

    # Momentum confirmation — last completed 1-min bar still pushing down.
    # Without this, PS would enter mid-bounce (the exact PUT failure mode).
    if not check_momentum_candle(bars_1m, "PUT", ticker=ticker):
        return

    # ── Bounce guards (Jul 24 — AMZN #176 + INTC #181, tally reached 2) ──
    # Both first PS losses entered mid-bounce: trend/thesis were true but
    # STALE — price had already lifted off the session low behind a green
    # 5-min candle.  PS must short a FRESH breakdown, not chop:
    #   1. the last COMPLETED 5-min bar must not be green
    #   2. price must still be near the session low
    if settings.put_scalp_no_green_5m_enabled:
        bars_5m = await _get_bars(client, ticker, interval="5min", lookback_days=1)
        _b5 = completed_bars(bars_5m, 5)
        if _b5 and _b5[-1].close > _b5[-1].open:
            logger.info(
                "[PS][%s] Blocked — last completed 5-min bar is GREEN "
                "(%.2f→%.2f): that's a bounce, not a breakdown",
                ticker, _b5[-1].open, _b5[-1].close,
            )
            return
    if settings.put_scalp_max_bounce_from_low_pct > 0:
        _lows = [b.low for b in session_bars(bars_1m) if b.low]
        if _lows:
            _session_low = min(_lows)
            if last_price > _session_low * (1 + settings.put_scalp_max_bounce_from_low_pct):
                logger.info(
                    "[PS][%s] Blocked — price %.2f is %.2f%% above session low "
                    "%.2f (max %.2f%%): breakdown is stale / bounce underway",
                    ticker, last_price, (last_price / _session_low - 1) * 100,
                    _session_low,
                    settings.put_scalp_max_bounce_from_low_pct * 100,
                )
                return

    logger.info(
        "[PS][%s] Signal — 15m trend bearish + underlying %.2f below session "
        "VWAP %.2f (band %.2f) + red momentum bar",
        ticker, last_price, vwap, _band,
    )

    # ── Contract selection (same delta/liquidity policy as S1) ───────────
    expirations = await client.get_option_expirations(ticker)
    if not expirations:
        return
    today_str  = date.today().isoformat()
    non_0dte   = [e for e in expirations if e > today_str]
    expiration = non_0dte[0] if non_0dte else expirations[0]

    full_chain = await client.get_options_chain(ticker, expiration)
    puts = [o for o in full_chain if o.option_type == "put"]
    if not puts:
        return
    eligible = [
        o for o in puts
        if (o.volume or 0) >= settings.option_min_volume
        and o.ask > 0
        and settings.option_min_delta <= abs(o.delta or 0) <= settings.option_max_delta
    ]
    if not eligible:
        eligible = [
            o for o in puts
            if (o.volume or 0) >= settings.option_min_volume and o.ask > 0
        ]
    if not eligible:
        eligible = [o for o in puts if o.ask > 0]
    if not eligible:
        return
    selected = min(eligible, key=lambda o: abs(o.strike - last_price))

    # Contract quality floors — min premium shared; spread gate is PS-OWN
    # (8% default): a 12% spread would consume the entire 8% target.
    _sel_mid = selected.mid if (selected.mid and selected.mid > 0) else selected.ask
    if settings.option_min_premium > 0 and _sel_mid < settings.option_min_premium:
        logger.info(
            "[PS][%s] Contract too cheap — mid $%.2f < min premium $%.2f — skipping",
            ticker, _sel_mid, settings.option_min_premium,
        )
        return
    if not check_option_spread(selected.bid, selected.ask,
                               settings.put_scalp_max_spread_pct, ticker=ticker):
        return  # logged inside

    # ── Sizing at the PS stop, half size by default ──────────────────────
    ask_price = round(selected.ask, 2)
    mid_price = round(selected.mid, 2) if selected.mid and selected.mid > 0 else ask_price
    if settings.use_limit_orders and mid_price > 0:
        order_price, order_type_str = mid_price, "limit"
    else:
        order_price, order_type_str = ask_price, "market"

    cost_per_contract = order_price * 100
    if cost_per_contract <= 0:
        return
    budget_qty = int(_s(client, "amount_per_trade") / cost_per_contract)
    risk_per_contract = cost_per_contract * settings.put_scalp_sl_pct
    _ps_risk = _s(client, "put_scalp_risk_per_trade")
    if _ps_risk > 0 and risk_per_contract > 0:
        qty = min(int(_ps_risk / risk_per_contract), budget_qty)
    else:
        qty = budget_qty
    if qty < 1:
        logger.info(
            "[PS][%s] Skipping — 1 contract @ $%.2f exceeds PS risk $%.0f or budget",
            ticker, order_price, _ps_risk,
        )
        return

    # ── Place order (entry lock guards the PS slot against concurrent scans) ─
    order: object = None
    async with _entry_lock:
        ps_recheck = await db.execute(_scope(
            select(sqlfunc.count(Trade.id)).where(
                Trade.status == TradeStatus.OPEN,
                Trade.strategy_name == "put_scalp",
            ), acct)
        )
        if int(ps_recheck.scalar() or 0) >= _s(client, "put_scalp_max_open"):
            return
        order = await client.place_option_order(
            option_symbol=selected.symbol,
            side="buy_to_open",
            quantity=qty,
            order_type=order_type_str,
            **({"limit_price": order_price} if order_type_str == "limit" else {}),
        )

    entry_price = await _await_entry_fill(
        client, ticker, order, order_type_str, order_price, ask_price,
        selected.symbol, qty,
    )
    if entry_price is None:
        return

    # Fixed PS brackets from the ACTUAL fill
    _sl = round(entry_price * (1 - settings.put_scalp_sl_pct), 2)
    _tp = round(entry_price * (1 + settings.put_scalp_tp_pct), 2)

    trade = Trade(
        symbol=ticker,
        option_symbol=selected.symbol,
        direction=Direction.PUT,
        strategy_name="put_scalp",
        account_id=_aid(client),
        tradier_order_id=order.order_id,
        quantity=qty,
        remaining_qty=qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        underlying_entry=last_price,
        vwap_at_entry=vwap,
        stop_price=_sl,
        tp1_price=_tp,
        tp2_price=_tp,
        original_stop_price=_sl,
    )
    db.add(trade)
    await db.commit()
    logger.info(
        "%s[PS][%s] Trade OPENED: PUT %s x%d @ $%.2f  SL=%.2f (−%.0f%%)  "
        "TP=%.2f (+%.0f%%)  (premium $%.0f, risk-at-stop $%.0f)",
        _tag(client), ticker, selected.symbol, qty, entry_price,
        _sl, settings.put_scalp_sl_pct * 100,
        _tp, settings.put_scalp_tp_pct * 100,
        qty * cost_per_contract,
        qty * (entry_price - _sl) * 100,
    )

    # Broker disaster stop (same machinery as S1; TP skipped while it rests)
    if settings.broker_stop_enabled:
        await _place_broker_stop(db, client, trade)
    if settings.broker_tp_enabled:
        if trade.stop_order_id:
            logger.info(
                "[PS][%s] Broker TP skipped — resting disaster stop %s already "
                "reserves the contracts (bot-side TP remains active)",
                ticker, trade.stop_order_id,
            )
        else:
            await _place_broker_tp(db, client, trade)


# ---------------------------------------------------------------------------
# Entry scanner — Strategy 2 (EMA crossover)
# ---------------------------------------------------------------------------

async def scan_for_entries_s2() -> None:
    """
    S2 entry scanner — runs on the same interval as S1 (SCAN_INTERVAL_SECONDS).
    Only active when s2_enabled=True.

    Trading window is checked against s2_trading_start_time / s2_last_entry_time.
    S2 uses its own symbol list (strategy="S2" in the symbols table).
    """
    # Global master switch first — an account toggle can only narrow it.
    if not settings.s2_enabled:
        return

    # Market-open guard (Jul 20 post-mortem): this scanner previously checked
    # only the TIME of day, not the day itself.  On Saturday Jul 18 it scanned
    # Friday's stale bars inside the 11:00-12:30 clock window, placed entry
    # limits at Friday's closing prices, and Tradier queued those weekend
    # orders for Monday's open — the "ghost trades" (−$373).  S1 always had
    # this guard via is_in_trading_window(); S2 now does too.
    if not is_market_open():
        return

    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=ET)

    # Trading window check using S2-specific times
    start_h, start_m = (int(x) for x in settings.s2_trading_start_time.split(":"))
    last_h,  last_m  = (int(x) for x in settings.s2_last_entry_time.split(":"))
    end_h,   end_m   = (int(x) for x in settings.s2_trading_end_time.split(":"))

    now_minutes = now_et.hour * 60 + now_et.minute
    start_min   = start_h * 60 + start_m
    last_min    = last_h  * 60 + last_m
    end_min     = end_h   * 60 + end_m

    if now_minutes < start_min or now_minutes > end_min:
        return
    if now_minutes > last_min:
        logger.debug("[S2] Past last entry time (%s ET) — no new S2 entries", settings.s2_last_entry_time)
        return

    await _for_each_account("scan_for_entries_s2", "s2_enabled",
                            _scan_for_entries_s2_account)


async def _scan_for_entries_s2_account(acct: AccountView) -> None:
    client = get_tradier_client(acct)
    _t = _tag(client)

    async with AsyncSessionLocal() as db:
        # Daily P&L guard (shared with S1, scoped to this account)
        daily_pnl = await _get_daily_pnl(db, acct)
        if daily_pnl <= -abs(_s(client, "max_daily_loss")):
            logger.info("%s[S2] Daily loss limit reached — no new S2 entries today", _t)
            return

        # Chop-day gate (shared with S1; 60-s cached verdict)
        if await _chop_gate_blocks(client):
            return

        # S2-specific daily loss circuit breaker
        # Tracks only S2 (ema_cross) realized losses — S1 losses don't count against S2's budget.
        # Set S2_MAX_DAILY_LOSS=0 in .env to disable.
        _s2_max_daily_loss = _s(client, "s2_max_daily_loss")
        if _s2_max_daily_loss > 0:
            s2_pnl = await _get_s2_daily_pnl(db, acct)
            if s2_pnl <= -abs(_s2_max_daily_loss):
                logger.info(
                    "%s[S2] S2 daily loss circuit breaker triggered ($%.2f realized) — "
                    "no new S2 entries today",
                    _t, s2_pnl,
                )
                return

        # S2 max concurrent positions
        s2_open_result = await db.execute(_scope(
            select(Trade).where(
                Trade.status == TradeStatus.OPEN,
                Trade.strategy_name == "ema_cross",
            ), acct)
        )
        s2_open_count = len(s2_open_result.scalars().all())
        if s2_open_count >= _s(client, "s2_max_open_trades"):
            logger.debug("%s[S2] Max S2 open trades (%d) reached",
                         _t, _s(client, "s2_max_open_trades"))
            return

        # Active symbols enrolled in S2
        sym_result = await db.execute(
            select(Symbol).where(Symbol.active == True, Symbol.s2_enabled == True)  # noqa: E712
        )
        symbols = sym_result.scalars().all()

    if not symbols:
        return

    sem = asyncio.Semaphore(3)

    async def _scan_s2_one(ticker: str) -> None:
        async with sem:
            async with AsyncSessionLocal() as sym_db:
                try:
                    await _attempt_entry_s2(sym_db, client, ticker)
                except Exception as exc:
                    logger.error(
                        "%s[S2] scan_for_entries_s2 error for %s: %s",
                        _t, ticker, exc, exc_info=True,
                    )

    await asyncio.gather(*[_scan_s2_one(sym.ticker) for sym in symbols])


async def _attempt_entry_s2(db, client, ticker: str) -> None:
    """
    S2 entry gate stack:

    Pre-entry guards (DB):
      G1  Per-symbol S2 open trade exists
      G2  S2 cooldown after recent exit on this symbol
      G3  First-cross-only: symbol already had any S2 trade today

    Signal layers (market data — all must pass):
      Step 1   5-min Trend Filter    : EMA9>EMA21, Close>VWAP, EMA9 slope↑, EMA21 slope↑
                                       (cached — only updates when a 5-min bar closes)
      Step 1b  EMA Cross Freshness   : EMA9/21 cross must be ≤ s2_cross_max_bars_old 5-min bars ago
                                       (blocks stale/exhausted trends; 0 = disabled)
      Step 1c  15-min Alignment Gate : CALL — blocked only by a bearish 15-min trend
                                       (neutral allowed).
                                       PUT  — with s2_put_15m_strict (default on) requires a
                                       confirmed BEARISH 15-min trend; neutral or missing
                                       15-min data blocks.  s2_puts_enabled=0 disables the
                                       PUT side entirely.
      Step 2   1-min Pullback        : price touches or comes within 0.10% of 5-min EMA9
      Step 3   1-min Confirmation    : candle closes in trade direction, breaks prior bar's range,
                                       AND close reclaims EMA9 (CALL: close > EMA9; PUT: close < EMA9)

    Post-signal filters:
      Volume   : 1-min underlying volume ≥ 20-bar rolling average
      Spread   : option bid/ask spread ≤ s2_max_spread_pct of the mid price
    """
    acct = _acct(client)

    # ── G1: Per-symbol open trade (any strategy, THIS account) ──────────
    # Prevents S2 from entering a symbol that S1 already holds, and vice versa.
    # Mirrors S1's G4 — strategy-agnostic so both strategies respect each other.
    # Scoped per account: a position held in another account is a separate
    # book and must not block this one.
    existing = await db.execute(_scope(
        select(Trade).where(
            Trade.symbol == ticker,
            Trade.status == TradeStatus.OPEN,
        ), acct)
    )
    if existing.scalars().first():
        return

    # ── G2: S2 cooldown ─────────────────────────────────────────────────
    recent_exit = await _get_s2_recent_bad_exit(db, ticker, acct)
    if recent_exit:
        logger.info(
            "[S2][%s] Cooldown active — last %s exit at %s (%d-min window)",
            ticker,
            recent_exit.exit_reason.value,
            recent_exit.exit_time.strftime("%H:%M UTC"),
            settings.s2_cooldown_minutes,
        )
        return

    # ── G3: Daily trade cap per symbol ───────────────────────────────────
    # Limit S2 entries to s2_max_trades_per_day per symbol.  A trending day
    # can produce multiple valid pullbacks, but repeated re-entries compound
    # intraday exposure.  Set s2_max_trades_per_day=0 to disable the cap.
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    if settings.s2_max_trades_per_day > 0:
        count_result = await db.execute(_scope(
            select(sqlfunc.count(Trade.id)).where(
                Trade.symbol == ticker,
                Trade.strategy_name == "ema_cross",
                Trade.entry_time >= today_start,
            ), acct)
        )
        today_count = count_result.scalar() or 0
        if today_count >= settings.s2_max_trades_per_day:
            logger.debug(
                "[S2][%s] Daily cap: %d/%d S2 trades today — skip",
                ticker, today_count, settings.s2_max_trades_per_day,
            )
            return

    # ── Fetch market data ────────────────────────────────────────────────
    # 5-min bars (multi-day): EMA history + trend filter + exit detection
    # 1-min bars (today only): pullback level, confirmation candle, VWAP, volume
    bars_5m = await _get_bars(client, ticker, interval="5min", lookback_days=5)
    bars_1m = await _get_bars(client, ticker, interval="1min", lookback_days=1)

    if not bars_5m or not bars_1m:
        missing = [tf for tf, b in (("5-min", bars_5m), ("1-min", bars_1m)) if not b]
        logger.warning("[S2][%s] Missing %s bars — skipping", ticker, " + ".join(missing))
        return

    # ── Energy floor + volatility ceiling (S2 toggles) ────────────────────
    if (settings.energy_gate_s2_enabled or settings.vol_ceiling_s2_enabled) \
            and await _symbol_energy_blocks(
        client, ticker, bars_1m,
        check_floor=settings.energy_gate_s2_enabled,
        check_ceiling=settings.vol_ceiling_s2_enabled,
    ):
        return

    # ── Step 1: 5-min Trend Filter ────────────────────────────────────────
    # Determines direction and validates trend (cached between candle closes).
    direction: str | None = None
    if check_5min_trend_filter(bars_5m, "CALL", ticker=ticker):
        direction = "CALL"
    elif check_5min_trend_filter(bars_5m, "PUT", ticker=ticker):
        direction = "PUT"

    if direction is None:
        return  # trend not aligned in either direction

    # ── PUT kill switch ───────────────────────────────────────────────────
    # Live results (Jun 8 – Jul 7 2026): S2 PUTs went 4W/14L for −$554 while
    # CALLs were net positive.  s2_puts_enabled=0 disables the PUT side
    # entirely until the signal is re-validated in backtests.
    if direction == "PUT" and not settings.s2_puts_enabled:
        logger.info("[S2][%s] PUT entries disabled (s2_puts_enabled=0) — skip", ticker)
        return

    # ── Step 1b: EMA cross freshness ──────────────────────────────────────
    # Block entries when the EMA9/21 cross is older than s2_cross_max_bars_old
    # 5-min bars (default 8 bars = 40 min).  A cross that happened hours ago
    # means price has been trending for a long time and the move is likely
    # exhausted — entering now is chasing, not catching a fresh pullback.
    if not check_ema_cross_freshness(bars_5m, direction, ticker=ticker):
        return

    # ── Step 1c: 15-min higher-timeframe alignment gate ───────────────────
    # CALL: blocked only by a confirmed OPPOSING (bearish) 15-min trend.
    #       Neutral is allowed (early session, consolidation).
    # PUT:  asymmetric on purpose.  Live results (Jun 8 – Jul 7 2026) showed
    #       almost every losing S2 PUT fired on a 5-min dip inside a larger
    #       15-min uptrend — the old symmetric gate let them through because
    #       a NEUTRAL 15-min allowed both directions.  With s2_put_15m_strict
    #       enabled, a PUT requires the 15-min trend to be confirmed BEARISH;
    #       neutral or missing 15-min data blocks the entry.
    bars_15m_s2 = await _get_bars(
        client, ticker, interval="15min", lookback_days=settings.trend_lookback_days
    )
    bars_15m_s2 = completed_bars(bars_15m_s2, interval_minutes=15) if bars_15m_s2 else []
    htf_trend = ema_direction(bars_15m_s2) if bars_15m_s2 else "unknown"

    if direction == "CALL":
        if htf_trend == "bearish":
            logger.info(
                "[S2][%s] 15-min alignment gate — CALL blocked: 15-min trend is bearish",
                ticker,
            )
            return
    else:  # PUT
        if settings.s2_put_15m_strict:
            if htf_trend != "bearish":
                logger.info(
                    "[S2][%s] 15-min alignment gate (strict) — PUT blocked: "
                    "15-min trend is %s (need confirmed bearish)",
                    ticker, htf_trend,
                )
                return
        elif htf_trend == "bullish":
            logger.info(
                "[S2][%s] 15-min alignment gate — PUT blocked: 15-min trend is bullish",
                ticker,
            )
            return

    # ── Step 2: 1-min Pullback to 5-min EMA9 ─────────────────────────────
    ema9_5m = get_5min_ema9(bars_5m)
    if ema9_5m is None:
        logger.debug("[S2][%s] Could not compute 5-min EMA9 — skipping", ticker)
        return
    if not check_1min_pullback(bars_1m, direction, ema9_5m, ticker=ticker):
        return  # price hasn't pulled back to EMA9 yet

    # ── Step 3: 1-min Confirmation candle ─────────────────────────────────
    # Pass ema9_5m so the confirmation requires close to reclaim EMA9
    # (CALL: close > EMA9; PUT: close < EMA9). Weak bounces that stall on
    # the wrong side of EMA9 are filtered out before committing capital.
    if not check_1min_confirmation(bars_1m, direction, ema9_5m=ema9_5m, ticker=ticker):
        return  # confirmation candle not yet closed in the right direction

    # ── Volume filter (underlying liquidity) ──────────────────────────────
    if not check_volume_filter(bars_1m, lookback=20, ticker=ticker):
        return  # current 1-min volume below 20-bar rolling average

    # ── Contract selection (same logic as S1) ─────────────────────────────
    # Use the most recent 1-min bar close for strike selection (more current
    # than the last 5-min bar close, which may be up to 5 minutes stale).
    current_price = bars_1m[-1].close

    expirations = await client.get_option_expirations(ticker)
    if not expirations:
        return

    today_str  = date.today().isoformat()
    non_0dte   = [e for e in expirations if e > today_str]
    expiration = non_0dte[0] if non_0dte else expirations[0]

    full_chain = await client.get_options_chain(ticker, expiration)
    side_chain = [o for o in full_chain if o.option_type == ("call" if direction == "CALL" else "put")]

    if not side_chain:
        return

    eligible = [
        o for o in side_chain
        if (o.volume or 0) >= settings.option_min_volume
        and o.ask > 0
        and settings.option_min_delta <= abs(o.delta or 0) <= settings.option_max_delta
    ]
    if not eligible:
        eligible = [o for o in side_chain if (o.volume or 0) >= settings.option_min_volume and o.ask > 0]
    if not eligible:
        eligible = [o for o in side_chain if o.ask > 0]
    if not eligible:
        return

    selected = min(eligible, key=lambda o: abs(o.strike - current_price))

    # ── Contract quality floors ────────────────────────────────────────────
    # Minimum premium: sub-$1 contracts tick in whole cents — F #146's stop
    # was 2 ticks wide.  Skip them entirely.
    _s2_mid = selected.mid if (selected.mid and selected.mid > 0) else selected.ask
    if settings.option_min_premium > 0 and _s2_mid < settings.option_min_premium:
        logger.info(
            "[S2][%s] Contract too cheap — mid $%.2f < min premium $%.2f — skipping",
            ticker, _s2_mid, settings.option_min_premium,
        )
        return

    # Spread filter: skip illiquid contracts where bid/ask spread exceeds the threshold.
    _max_spread = settings.s2_max_spread_pct
    if not check_option_spread(selected.bid, selected.ask, _max_spread, ticker=ticker):
        return

    # ── Position sizing ───────────────────────────────────────────────────
    ask_price  = round(selected.ask, 2)
    mid_price  = round(selected.mid, 2) if selected.mid and selected.mid > 0 else ask_price

    # Pullback-confirmed signals have enough persistence for a limit at mid.
    # This saves half the bid-ask spread vs hitting the ask.
    if settings.use_limit_orders and mid_price > 0:
        order_price    = mid_price
        order_type_str = "limit"
    else:
        order_price    = ask_price
        order_type_str = "market"

    cost_per_contract = order_price * 100
    if cost_per_contract <= 0:
        return

    # ── Structural (chart-based) levels ───────────────────────────────────
    # Stop anchored below the pullback structure / 5-min EMA9 (the entry
    # thesis level), target at the session swing high/low, translated to
    # option prices via delta.  Skips setups without room to the target.
    struct = None
    if settings.structural_levels_enabled:
        stop_u, target_u = get_structural_stop_target(
            bars_1m, direction, ema9_5m,
            buffer_pct=settings.struct_stop_buffer_pct,
            pullback_lookback=3,   # S2's pattern is exactly pullback + confirm bars
        )
        struct = compute_structural_levels(
            direction=direction,
            option_entry=order_price,
            delta=selected.delta,
            underlying_entry=current_price,
            stop_underlying=stop_u,
            target_underlying=target_u,
        )
        if not struct.ok:
            if struct.skip_reason == "fallback":
                logger.info(
                    "[S2][%s] Structural levels unavailable (no delta) — "
                    "using percentage levels", ticker,
                )
                struct = None
            else:
                logger.info(
                    "[S2][%s] Structural gate blocked %s entry — %s",
                    ticker, direction, struct.skip_reason,
                )
                return
        else:
            logger.info(
                "[S2][%s] Structural levels: stop_u=%.2f target_u=%.2f R/R=%.2f "
                "→ option SL=%.2f (−%.0f%%) TP=%.2f",
                ticker, struct.stop_underlying, struct.target_underlying,
                struct.reward_risk, struct.stop_price, struct.risk_pct * 100,
                struct.tp_price,
            )

    risk_frac = struct.risk_pct if struct else settings.s2_stop_loss_pct
    _s2_amount = _s(client, "s2_amount_per_trade")
    _s2_risk   = _s(client, "s2_risk_per_trade")
    budget_qty = int(_s2_amount / cost_per_contract)
    if _s2_risk > 0 and risk_frac > 0:
        risk_per_contract = cost_per_contract * risk_frac
        risk_qty = int(_s2_risk / risk_per_contract)
        qty = min(risk_qty, budget_qty)
    else:
        qty = budget_qty

    if qty < 1:
        logger.info(
            "[S2][%s] Skipping — 1 contract @ $%.2f exceeds S2 size limits "
            "(budget $%.0f, risk $%.0f)",
            ticker, order_price, _s2_amount, _s2_risk,
        )
        return

    # ── TP price-chase guard ──────────────────────────────────────────────
    # If this symbol had a profitable S2 exit today in the same direction,
    # block re-entry when the new option price is significantly above the
    # previous entry price.  After a TP the move is already extended — the
    # EMA9 has drifted higher with price and the next "pullback" still lands
    # at a much more expensive option level, eroding R/R.
    # Different direction is always allowed (fresh opposing setup).
    # Unprofitable exits (STOP, EMA_CROSS, or TRAILING_STOP below entry) are
    # excluded — price may have reset and the new setup is legitimate.
    if settings.s2_tp_chase_pct > 0:
        s2_chase_trade = await _get_s2_tp_exit_for_chase_check(db, ticker, acct)
        if (
            s2_chase_trade
            and s2_chase_trade.direction.value == direction
            and s2_chase_trade.entry_price > 0
        ):
            price_ratio = order_price / s2_chase_trade.entry_price
            if price_ratio > (1 + settings.s2_tp_chase_pct):
                logger.info(
                    "[S2][%s] TP chase guard — %s new entry $%.2f is %.0f%% above "
                    "last profitable entry $%.2f (max %.0f%%) — move extended, skipping",
                    ticker, direction,
                    order_price, (price_ratio - 1) * 100,
                    s2_chase_trade.entry_price, settings.s2_tp_chase_pct * 100,
                )
                return

    # ── Place order (inside lock) ─────────────────────────────────────────
    order: object = None
    async with _entry_lock:
        # Re-check S2 cap inside lock
        s2_recheck = await db.execute(_scope(
            select(sqlfunc.count(Trade.id)).where(
                Trade.status == TradeStatus.OPEN,
                Trade.strategy_name == "ema_cross",
            ), acct)
        )
        if int(s2_recheck.scalar() or 0) >= _s(client, "s2_max_open_trades"):
            logger.debug("%s[S2][%s] S2 cap reached (re-check inside lock) — skipping",
                         _tag(client), ticker)
            return

        if order_type_str == "limit":
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="limit",
                limit_price=order_price,
            )
        else:
            order = await client.place_option_order(
                option_symbol=selected.symbol,
                side="buy_to_open",
                quantity=qty,
                order_type="market",
            )

    # ── Limit order fill poll (same as S1) ────────────────────────────────
    if order_type_str == "limit":
        logger.info(
            "[S2][%s] Limit order %s placed: %s x%d @ $%.2f",
            ticker, order.order_id, selected.symbol, qty, order_price,
        )
        filled   = False
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=settings.limit_order_timeout_seconds)
        while datetime.now(tz=timezone.utc) < deadline:
            await asyncio.sleep(2)
            try:
                status_data = await client.get_order_status(order.order_id)
                status_str  = (status_data.get("status") or "").lower()
            except Exception:
                status_str = "unknown"
            if status_str == "filled":
                filled = True
                break
            if status_str in ("rejected", "canceled", "cancelled"):
                logger.info("[S2][%s] Limit order %s %s — aborting", ticker, order.order_id, status_str.upper())
                return
        if not filled:
            try:
                await client.cancel_order(order.order_id)
            except Exception:
                pass
            # Post-cancel race check
            try:
                post = await client.get_order_status(order.order_id)
                if (post.get("status") or "").lower() == "filled":
                    filled = True
                else:
                    return
            except Exception:
                return

        actual_fill = await client.get_fill_price(order.order_id)
        entry_price = actual_fill if actual_fill and actual_fill > 0 else order_price
    else:
        try:
            order_status_data = await client.get_order_status(order.order_id)
            order_status_str  = (order_status_data.get("status") or "").lower()
        except Exception:
            order_status_str = "unknown"
        if order_status_str in ("rejected", "canceled", "cancelled"):
            logger.error("[S2][%s] Buy order %s was %s — aborting", ticker, order.order_id, order_status_str.upper())
            return
        entry_price = ask_price

    # Levels from the ACTUAL fill price.  Structural: keep the underlying
    # anchors, re-translate to option prices; fall back to pre-fill levels
    # if the refit breaks (fill moved past a gate).
    tp2_price: float | None = None
    if struct:
        refit = compute_structural_levels(
            direction=direction,
            option_entry=entry_price,
            delta=selected.delta,
            underlying_entry=current_price,
            stop_underlying=struct.stop_underlying,
            target_underlying=struct.target_underlying,
        )
        stop_price = refit.stop_price if refit.ok else struct.stop_price
        tp2_price  = refit.tp_price   if refit.ok else struct.tp_price
    else:
        stop_price = round(entry_price * (1.0 - settings.s2_stop_loss_pct), 2)
        if settings.s2_take_profit_pct > 0:
            tp2_price = round(entry_price * (1.0 + settings.s2_take_profit_pct), 2)

    trade = Trade(
        symbol=ticker,
        option_symbol=selected.symbol,
        direction=direction,
        strategy_name="ema_cross",
        account_id=_aid(client),
        tradier_order_id=order.order_id,
        quantity=qty,
        remaining_qty=qty,
        entry_price=entry_price,
        entry_time=datetime.now(tz=timezone.utc),
        underlying_entry=current_price,
        stop_price=stop_price,
        original_stop_price=stop_price,   # entry-time snapshot for honest exit labels
        tp1_price=None,
        tp2_price=tp2_price,
    )
    db.add(trade)
    await db.commit()
    _tp_str = (f"  TP={tp2_price:.2f}" if tp2_price else "  TP=signal exit only")
    logger.info(
        "%s[S2][%s] Trade OPENED: %s %s x%d @ $%.2f  SL=%.2f%s",
        _tag(client), ticker, direction, selected.symbol, qty, entry_price,
        stop_price, _tp_str,
    )


async def _place_broker_tp(db, client, trade: Trade) -> None:
    """Place a resting sell-to-close limit order at the TP price and record its id."""
    tp_price = trade.tp1_price or trade.tp2_price
    if not tp_price:
        logger.warning("[%s] Trade %d has no TP price — cannot place broker TP order", trade.symbol, trade.id)
        return
    try:
        tp_order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=trade.remaining_qty or trade.quantity,
            order_type="limit",
            limit_price=tp_price,
        )
        if tp_order.order_id:
            trade.tp_order_id = tp_order.order_id
            await db.commit()
            logger.info(
                "[%s] Broker TP placed: order %s @ $%.2f",
                trade.symbol, tp_order.order_id, tp_price,
            )
        else:
            logger.error(
                "[%s] Broker TP order returned no order_id (raw=%s) — "
                "bot-side TP remains the only protection for trade %d",
                trade.symbol, tp_order.raw, trade.id,
            )
    except Exception as exc:
        logger.error(
            "[%s] Failed to place broker TP for trade %d: %s — "
            "bot-side TP remains active",
            trade.symbol, trade.id, exc,
        )


def _broker_stop_price(stop_price: float) -> float:
    """
    Broker-side DISASTER stop level: the bot's working stop minus the
    configured buffer (broker_stop_buffer_pct, default 8%).

    Tradier stops trigger on traded prints and fill at market — at the bot's
    exact level they would front-run the smarter mid-based bot stop on
    spread noise.  Buffered below it, the broker stop is unreachable in
    normal operation and only fills when the bot is dead or the move gapped
    through a management tick.
    """
    return max(0.01, round(stop_price * (1 - settings.broker_stop_buffer_pct), 2))


async def _place_broker_stop(db, client, trade: Trade) -> None:
    """Place a resting sell-to-close DISASTER stop at the broker (buffered
    below the bot's working stop) and record its id."""
    try:
        disaster_price = _broker_stop_price(trade.stop_price)
        stop_order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=trade.remaining_qty or trade.quantity,
            order_type="stop",
            stop_price=disaster_price,
        )
        if stop_order.order_id:
            trade.stop_order_id = stop_order.order_id
            await db.commit()
            logger.info(
                "[%s] Broker disaster stop placed: order %s @ $%.2f "
                "(bot stop $%.2f − %.0f%% buffer)",
                trade.symbol, stop_order.order_id, disaster_price,
                trade.stop_price, settings.broker_stop_buffer_pct * 100,
            )
        else:
            logger.error(
                "[%s] Broker stop order returned no order_id (raw=%s) — "
                "bot-side stop remains the only protection for trade %d",
                trade.symbol, stop_order.raw, trade.id,
            )
    except Exception as exc:
        logger.error(
            "[%s] Failed to place broker stop for trade %d: %s — "
            "bot-side stop remains the only protection",
            trade.symbol, trade.id, exc,
        )


# ---------------------------------------------------------------------------
# Open trade manager
# ---------------------------------------------------------------------------

async def manage_open_trades() -> None:
    """
    Exit management — runs once per account, over that account's OPEN trades.

    Uses ALL accounts, not just enabled ones: disabling an account stops NEW
    entries, but an already-open position must still be managed to its exit.
    Abandoning a live position because someone unticked a checkbox would be
    the worst possible failure mode in this system.
    """
    if not is_market_open():
        return

    try:
        accounts = await all_account_views()
    except Exception as exc:
        logger.error("manage_open_trades: could not load accounts: %s", exc,
                     exc_info=True)
        return

    for acct in accounts:
        try:
            await _manage_open_trades_account(acct)
        except Exception as exc:
            logger.error(
                "manage_open_trades failed for account '%s': %s — OPEN POSITIONS "
                "IN THIS ACCOUNT WERE NOT MANAGED THIS TICK",
                acct.name, exc, exc_info=True,
            )


async def _manage_open_trades_account(acct: AccountView) -> None:
    client = get_tradier_client(acct)
    cutoff = is_past_cutoff()

    async with AsyncSessionLocal() as db:
        result = await db.execute(_scope(
            select(Trade).where(Trade.status == TradeStatus.OPEN), acct)
        )
        trades = result.scalars().all()

        for trade in trades:
            # ── S3 trades are STOCKS managed by the S3 engine ────────────
            # The options logic below (option quotes, premium-% stops,
            # option sell orders) must never touch them — the S3 engine
            # owns their stops, targets and flatten.
            if trade.strategy_name == "S3":
                continue
            try:
                # ── Broker-stop reconciliation ──────────────────────────────
                # If the broker filled our resting stop order (e.g. while the
                # bot's price check missed it due to spread timing), detect and
                # record the close here before doing anything else.
                if trade.stop_order_id and settings.broker_stop_enabled:
                    if await _reconcile_broker_stop(db, client, trade):
                        continue

                # ── Broker-TP reconciliation ─────────────────────────────────
                # If the broker filled our resting TP limit order (e.g. while
                # the bot's bid-price check missed it due to spread timing),
                # detect and record the close here before doing anything else.
                if trade.tp_order_id and settings.broker_tp_enabled:
                    if await _reconcile_broker_tp(db, client, trade):
                        continue

                # ── Strategy 2 (EMA cross) management ───────────────────────
                if trade.strategy_name == "ema_cross":
                    await _manage_s2_trade(db, client, trade, cutoff)
                    continue

                # Force close at daily cutoff
                if cutoff:
                    await _close_trade(db, client, trade, ExitReason.CUTOFF)
                    continue

                # Two prices are needed for different purposes:
                #
                #   bid_price — used for TP2 evaluation.
                #     TP2 should only fire when you can actually *receive* the
                #     target price.  A market sell fills at the bid, so using
                #     mid would trigger TP2 at a price you can never achieve on
                #     wide-spread options (GOOGL: mid=$2.79 → bid=$1.90 = real loss).
                #
                #   mid_price — used for STOP / trailing-stop evaluation.
                #     Stops should fire when the market has genuinely moved against
                #     the position.  Using bid for stops causes premature triggers
                #     whenever the bid temporarily dips below the stop level due to
                #     wide spreads, even though the mid (true market price) is still
                #     above the stop.  (META: bid=$5.95 < trail-stop=$6.07 → fired
                #     at breakeven even though mid=$6.22 was safely above the stop.)
                opt_q = await client.get_option_quote(trade.option_symbol)
                if not opt_q:
                    continue
                bid, ask, last = opt_q.bid, opt_q.ask, opt_q.last
                # ── Two-sided quote required (Jul 27 2026 WMT #195 / ORCL #197) ──
                # This used to fall back to `last` when a side was missing.
                # `last` is the most recent TRADE print — on a thin option it
                # can be minutes stale and far from the market.  During a
                # Tradier feed wobble that stale number fired two exits at
                # prices the market was not offering: WMT triggered on $1.00
                # and filled $1.14 (+14%), ORCL triggered on $3.65 and filled
                # $4.18 (+14.5%), neither anywhere near its stop.  A one-sided
                # or missing quote is not a price — wait for the next tick.
                # Delayed exits during a feed outage are precisely what the
                # broker disaster stop is there to cover.
                if not (bid and bid > 0 and ask and ask > 0):
                    logger.warning(
                        "[%s] Trade %d: degraded option quote (bid=%s ask=%s "
                        "last=%s) — skipping exit checks this tick",
                        trade.symbol, trade.id, bid, ask, last,
                    )
                    continue
                bid_price = bid
                mid_price = (bid + ask) / 2
                if not bid_price or not mid_price:
                    continue  # no valid price — skip this tick

                # ── Runner mode activation (S1) ─────────────────────────────
                # When the bid reaches the runner check zone near the TP and
                # the last completed 1-min candle still has momentum in the
                # trade direction, waive the fixed TP and let the runner trail
                # manage the exit.  If momentum has faded, the TP fires
                # normally on a later tick.
                # PUT-scalp trades use their own (tighter) runner params:
                # arm at +arm_pct GAIN over entry (not TP proximity), trail
                # and floor from the PS settings.
                _is_ps = trade.strategy_name == "put_scalp"
                _r_trail = (settings.put_scalp_runner_trail_pct if _is_ps
                            else settings.runner_trail_pct)
                _r_floor_lock = (settings.put_scalp_runner_floor_lock_pct if _is_ps
                                 else settings.runner_floor_lock_pct)
                if _is_ps:
                    _runner_zone = (
                        trade.entry_price > 0
                        and bid_price >= trade.entry_price
                        * (1 + settings.put_scalp_runner_arm_pct)
                    )
                else:
                    _runner_zone = bool(trade.tp2_price) and bid_price >= (
                        trade.tp2_price * (1 - settings.runner_proximity_pct)
                    )
                if (
                    settings.runner_mode_enabled
                    and not trade.runner_mode
                    and not trade.tp_manual      # never waive a HUMAN-set target
                    and trade.tp2_price
                    and _runner_zone
                ):
                    r_bars = await _get_bars(client, trade.symbol, interval="1min", lookback_days=1)
                    if r_bars and should_activate_runner(
                        bid_price, trade.tp2_price, r_bars, trade.direction.value,
                        # PS is already in the zone by gain — pass a proximity
                        # that cannot re-block (1.0 = any price qualifies).
                        proximity_pct=1.0 if _is_ps else None,
                    ):
                        trade.runner_mode = True
                        trade.tp1_price = trade.tp2_price   # keep original target for reference
                        trade.tp2_price = None              # disarm the fixed TP
                        # Floor never below entry+1%: activation can fire with
                        # the bid 5% under the TP, and a plain −8% trail from
                        # there can sit below entry — converting a winner into
                        # a loser (GOOGL #140 exited −3% via its own runner).
                        runner_floor = max(
                            round(mid_price * (1 - _r_trail), 2),
                            round(trade.entry_price * (1 + _r_floor_lock), 2),
                        )
                        if trade.stop_price is None or runner_floor > trade.stop_price:
                            trade.stop_price = runner_floor
                        await db.commit()
                        logger.info(
                            "[%s]%s RUNNER mode activated — TP $%.2f waived on momentum, "
                            "trailing %.0f%% below mid (stop now $%.2f)",
                            trade.symbol, " [PS]" if _is_ps else "", trade.tp1_price,
                            _r_trail * 100, trade.stop_price,
                        )
                        # Cancel the broker-side resting TP — it would still
                        # fill at the old target otherwise.
                        if trade.tp_order_id:
                            try:
                                await client.cancel_order(trade.tp_order_id)
                                trade.tp_order_id = None
                                await db.commit()
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Could not cancel broker TP %s at runner "
                                    "activation: %s — CHECK BROKER",
                                    trade.symbol, trade.tp_order_id, exc,
                                )
                        # Sync the broker-side resting stop to the runner floor
                        # now — the trailing block below only syncs on the NEXT
                        # raise, which may not come until the price moves.
                        if trade.stop_order_id:
                            try:
                                await client.modify_order(
                                    trade.stop_order_id,
                                    order_type="stop",
                                    stop_price=_broker_stop_price(trade.stop_price),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Could not raise broker stop %s at runner "
                                    "activation: %s (bot-side stop is current)",
                                    trade.symbol, trade.stop_order_id, exc,
                                )

                # ── Trailing stop: raise stop as trade moves in our favour ──
                # Uses mid_price — we want to track genuine option appreciation,
                # not bid bounces caused by wide spreads.
                if trade.entry_price and trade.stop_price is not None:
                    new_stop = compute_trailing_stop(
                        entry_price=trade.entry_price,
                        current_option_price=mid_price,
                        current_stop=trade.stop_price,
                        entry_time=trade.entry_time,
                    )
                    # Runner trail: ratchet the stop runner_trail_pct below the
                    # current mid — tighter/faster than the standard trail and
                    # active regardless of the standard trail's gain threshold.
                    if trade.runner_mode:
                        runner_floor = round(mid_price * (1 - _r_trail), 2)
                        new_stop = max(new_stop, runner_floor)
                    if new_stop != trade.stop_price:
                        gain_pct = (
                            (mid_price - trade.entry_price)
                            / trade.entry_price * 100
                        )
                        logger.info(
                            "[%s] Trailing stop raised: $%.2f → $%.2f "
                            "(option +%.1f%% — entry $%.2f  current $%.2f)",
                            trade.symbol,
                            trade.stop_price, new_stop,
                            gain_pct,
                            trade.entry_price, mid_price,
                        )
                        trade.stop_price = new_stop
                        await db.commit()

                        # Keep the broker-side disaster stop in sync (buffered).
                        if trade.stop_order_id:
                            try:
                                _disaster = _broker_stop_price(new_stop)
                                await client.modify_order(
                                    trade.stop_order_id,
                                    order_type="stop",
                                    stop_price=_disaster,
                                )
                                logger.info(
                                    "[%s] Broker disaster stop %s raised to $%.2f "
                                    "(bot stop $%.2f)",
                                    trade.symbol, trade.stop_order_id, _disaster, new_stop,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[%s] Could not raise broker stop %s to $%.2f: %s "
                                    "(bot-side stop is at the new level)",
                                    trade.symbol, trade.stop_order_id, new_stop, exc,
                                )

                # Current underlying price (needed for VWAP_BREAK check).
                underlying_q = await client.get_quote(trade.symbol)

                # ── Signal-conflict exit (SIGNAL_FADE, Jul 23) ──────────────
                # Both dashboard signals must oppose the trade:
                #   Stock Trend — completed-bar 15-min EMA trend flipped
                #   Thesis      — underlying beyond the exit band on the
                #                 wrong side of the SESSION VWAP
                # Requiring both (and bar-close trend) is the noise guard.
                if settings.signal_conflict_exit_enabled and underlying_q.last:
                    c15 = await _get_bars(client, trade.symbol, interval="15min",
                                          lookback_days=settings.trend_lookback_days)
                    c1  = await _get_bars(client, trade.symbol, interval="1min",
                                          lookback_days=1)
                    if c15 and c1:
                        _t = ema_direction(completed_bars(c15, 15), settings.ema_period)
                        _dirv = trade.direction.value
                        trend_conflict = (
                            (_dirv == "CALL" and _t == "bearish")
                            or (_dirv == "PUT" and _t == "bullish")
                        )
                        thesis_broken = False
                        _sv = calculate_vwap(session_bars(c1))
                        if _sv > 0:
                            _band = _sv * max(settings.vwap_exit_band_pct, 0.003)
                            if _dirv == "CALL":
                                thesis_broken = underlying_q.last < _sv - _band
                            else:
                                thesis_broken = underlying_q.last > _sv + _band
                        if trend_conflict and thesis_broken:
                            if trade.signal_conflict_time is None:
                                trade.signal_conflict_time = datetime.now(tz=timezone.utc)
                                await db.commit()
                            logger.info(
                                "[%s] SIGNAL_FADE — trend=%s against %s AND thesis "
                                "broken (underlying %.2f vs session VWAP %.2f) — "
                                "exiting via marketable limit",
                                trade.symbol, _t, _dirv, underlying_q.last, _sv,
                            )
                            await _close_trade(
                                db, client, trade, ExitReason.SIGNAL_FADE, bid_price
                            )
                            continue

                # VWAP_BREAK band exit stays disabled whenever the disable
                # flag is set — REGARDLESS of structural levels (Jul 23:
                # fixed 21/17 levels turned structural off; the old coupling
                # would have silently resurrected the 0.3% noise exit).
                # SIGNAL_FADE (session-VWAP + trend, both required) is its
                # principled replacement.
                _vwap_for_exit = trade.vwap_at_entry or 0
                if settings.struct_disable_vwap_break:
                    _vwap_for_exit = 0

                exit_cond = check_exit_conditions(
                    direction=trade.direction.value,
                    entry_price=trade.entry_price,
                    current_option_price=bid_price,   # bid  — for TP evaluation
                    stop_eval_price=mid_price,         # mid  — for stop evaluation
                    stop_price=trade.stop_price or 0,
                    tp1_price=trade.tp1_price or 999_999,
                    tp2_price=trade.tp2_price or 999_999,
                    tp1_hit=trade.tp1_hit,
                    vwap_at_entry=_vwap_for_exit,
                    current_underlying=underlying_q.last,
                    remaining_qty=trade.remaining_qty or trade.quantity,
                    entry_time=trade.entry_time,
                    original_stop=trade.original_stop_price,
                    # PS: stop suppressed for its own hold window (6 min).
                    # Quick-loss + the broker disaster stop remain active.
                    stop_min_hold_minutes=(
                        settings.put_scalp_stop_min_hold_minutes if _is_ps else None
                    ),
                )

                if not exit_cond:
                    continue

                # Pass bid_price as the trigger quote so _close_trade records
                # the realistically achievable exit price for P&L calculation.
                # ── v1: all exits close 100 % of the position ──────────────
                _s1_reason = ExitReason[exit_cond.reason]
                # Runner-mode trades exiting via their trail get the RUNNER
                # label so analytics can separate them from ordinary stops.
                if trade.runner_mode and _s1_reason in (
                    ExitReason.STOP, ExitReason.TRAILING_STOP
                ):
                    _s1_reason = ExitReason.RUNNER
                await _close_trade(
                    db, client, trade,
                    _s1_reason,
                    bid_price,
                )

            except Exception as exc:
                logger.error(
                    "manage_open_trades error trade %d: %s", trade.id, exc, exc_info=True
                )

        # ── Orphan auto-stop backstop ──────────────────────────────────────
        # Check any Tradier positions Ajoy isn't tracking and auto-close
        # them if they've dropped below the configured stop-loss threshold.
        try:
            await _manage_orphan_stops(db, client)
        except Exception as exc:
            logger.error("_manage_orphan_stops failed: %s", exc, exc_info=True)


async def _manage_s2_trade(db, client, trade: Trade, cutoff: bool) -> None:
    """
    S2 (EMA cross) exit manager for a single open trade.

    Exit priority:
      1. Daily cutoff (same as S1)
      2. check_s2_exit_conditions():
         a. Hard stop (min-hold respected)
         b. Trailing stop cascade (breakeven → trail)
         c. Opposite EMA cross on 5-min
    """
    # 1. Force close at cutoff
    if cutoff:
        await _close_trade(db, client, trade, ExitReason.CUTOFF)
        return

    # 2. Fetch current option quote
    opt_q = await client.get_option_quote(trade.option_symbol)
    if not opt_q:
        return

    bid, ask, last = opt_q.bid, opt_q.ask, opt_q.last
    # Two-sided quote required — see the note in manage_open_trades.  ORCL
    # #197 (S2) quick-lossed on a $3.65 trigger and filled $4.18.
    if not (bid and bid > 0 and ask and ask > 0):
        logger.warning(
            "[S2][%s] Trade %d: degraded option quote (bid=%s ask=%s last=%s) "
            "— skipping exit checks this tick",
            trade.symbol, trade.id, bid, ask, last,
        )
        return
    bid_price = bid
    mid_price = (bid + ask) / 2
    if not bid_price or not mid_price:
        return

    # 2b. Runner mode activation — must run BEFORE the TP check so a target
    #     reached WITH momentum waives the TP instead of banking it.
    #     Floor uses the BID (S2 stops trigger on bid): at activation the
    #     floor sits runner_trail_pct below the bid, so it can never fire on
    #     the same tick regardless of spread width.
    if (
        settings.runner_mode_enabled
        and not trade.runner_mode
        and not trade.tp_manual          # never waive a HUMAN-set target
        and trade.tp2_price
        and bid_price >= trade.tp2_price * (1 - settings.runner_proximity_pct)
    ):
        r_bars = await _get_bars(client, trade.symbol, interval="1min", lookback_days=1)
        if r_bars and should_activate_runner(
            bid_price, trade.tp2_price, r_bars, trade.direction.value
        ):
            trade.runner_mode = True
            trade.tp1_price = trade.tp2_price   # keep original target for reference
            trade.tp2_price = None              # disarm the fixed TP
            # Floor never below entry+1% (see S1 comment — GOOGL #140).
            runner_floor = max(
                round(bid_price * (1 - settings.runner_trail_pct), 2),
                round(trade.entry_price * (1 + settings.runner_floor_lock_pct), 2),
            )
            if trade.stop_price is None or runner_floor > trade.stop_price:
                trade.stop_price = runner_floor
            await db.commit()
            logger.info(
                "[S2][%s] RUNNER mode activated — TP $%.2f waived on momentum, "
                "trailing %.0f%% below bid (stop now $%.2f)",
                trade.symbol, trade.tp1_price,
                settings.runner_trail_pct * 100, trade.stop_price,
            )

    # 2c. Runner trail ratchet — stop follows runner_trail_pct below the bid,
    #     only ever moving up.
    if trade.runner_mode and trade.stop_price is not None:
        runner_floor = round(bid_price * (1 - settings.runner_trail_pct), 2)
        if runner_floor > trade.stop_price:
            logger.debug(
                "[S2][%s] Runner trail raised: $%.2f → $%.2f",
                trade.symbol, trade.stop_price, runner_floor,
            )
            trade.stop_price = runner_floor
            await db.commit()

    # 3. Manual take-profit override — checked against bid so it only fires when
    #    the trader can actually receive that price.  Uses TP2 field (same as S1 UI).
    if trade.tp2_price and bid_price >= trade.tp2_price:
        logger.info(
            "[S2][%s] Trade %d: manual TP hit — bid $%.2f ≥ tp2 $%.2f",
            trade.symbol, trade.id, bid_price, trade.tp2_price,
        )
        await _close_trade(db, client, trade, ExitReason.TP2, bid_price)
        return

    # 4. Fetch 5-min bars for EMA levels (+ legacy cross exit fallback)
    bars_5m = await _get_bars(client, trade.symbol, interval="5min", lookback_days=5)
    if not bars_5m:
        logger.warning("[S2][%s] Trade %d: no 5-min bars — skipping exit check", trade.symbol, trade.id)
        return

    # 4a. Signal-conflict exit (SIGNAL_FADE, Jul 23): the 5-min trend filter
    #     now fully validates the OPPOSITE direction (EMA9/21 crossed against
    #     us + close on the wrong side of VWAP + both slopes against) — the
    #     dashboard's Stock Trend and Thesis both conflict.  Exit via the
    #     marketable-limit urgent path.
    if settings.signal_conflict_exit_enabled:
        _opp = "PUT" if trade.direction.value == "CALL" else "CALL"
        if check_5min_trend_filter(bars_5m, _opp, ticker=trade.symbol):
            if trade.signal_conflict_time is None:
                trade.signal_conflict_time = datetime.now(tz=timezone.utc)
                await db.commit()
            logger.info(
                "[S2][%s] SIGNAL_FADE — 5-min trend fully validated %s against "
                "open %s — exiting via marketable limit",
                trade.symbol, _opp, trade.direction.value,
            )
            await _close_trade(db, client, trade, ExitReason.SIGNAL_FADE, bid_price)
            return

    # 4b. Structure exit inputs: 1-min bars + current 5-min EMA9.
    #     The entry thesis is "price bounced off the 5-min EMA9" — the exit
    #     watches 1-min closes back through that level (reacts in 2–3 min;
    #     the old 5-min EMA9/21 cross took 10–15 min and never beat the stop).
    bars_1m_exit: list = []
    ema9_5m_exit: float | None = None
    if settings.s2_structure_exit_enabled:
        bars_1m_exit = await _get_bars(client, trade.symbol, interval="1min", lookback_days=1)
        ema9_5m_exit = get_5min_ema9(bars_5m)

    # 5. Evaluate S2 exit conditions
    # mid_price → gain computation + trail level (accurate market price)
    # bid_price → stop trigger (we exit at bid, so bid must breach the stop
    #             before we act — prevents firing when mid == stop but bid is
    #             already 5-10% lower due to a wide spread)
    exit_cond = check_s2_exit_conditions(
        bars=bars_5m,
        direction=trade.direction.value,
        entry_price=trade.entry_price,
        current_price=mid_price,
        stop_price=trade.stop_price or round(trade.entry_price * (1.0 - settings.s2_stop_loss_pct), 2),
        be_stop_set=trade.be_stop_set,
        entry_time=trade.entry_time,
        interval_minutes=5,
        bid_price=bid_price,
        bars_1m=bars_1m_exit or None,
        ema9_5m=ema9_5m_exit,
        original_stop=trade.original_stop_price,
    )

    if exit_cond is None:
        return

    if not exit_cond.close_all and exit_cond.new_stop is not None:
        # Raise the stop (breakeven or trail) — don't close yet
        old_stop = trade.stop_price
        trade.stop_price = exit_cond.new_stop
        if exit_cond.reason == "TRAILING_STOP" and not trade.be_stop_set:
            # Mark breakeven as set only when new_stop == entry_price (breakeven raise)
            if abs(exit_cond.new_stop - trade.entry_price) < 0.005:
                trade.be_stop_set = True
        await db.commit()
        logger.info(
            "[S2][%s] Trade %d stop raised: $%.2f → $%.2f",
            trade.symbol, trade.id, old_stop or 0, exit_cond.new_stop,
        )
        return

    # Map S2 reason string to ExitReason enum
    reason_map = {
        "STOP": ExitReason.STOP,
        "TRAILING_STOP": ExitReason.TRAILING_STOP,
        "EMA_CROSS": ExitReason.EMA_CROSS,
        "STRUCT_EXIT": ExitReason.STRUCT_EXIT,
        "QUICK_LOSS": ExitReason.QUICK_LOSS,
        "CUTOFF": ExitReason.CUTOFF,
    }
    reason = reason_map.get(exit_cond.reason, ExitReason.STOP)
    # Runner-mode trades exiting via their trail get the RUNNER label so
    # analytics can separate "let it run" outcomes from ordinary stops.
    if trade.runner_mode and reason in (ExitReason.STOP, ExitReason.TRAILING_STOP):
        reason = ExitReason.RUNNER
    await _close_trade(db, client, trade, reason, bid_price)


async def _manage_orphan_stops(db, client) -> None:
    """
    Auto-stop backstop for orphaned Tradier positions.

    A position is an orphan if it exists in Tradier but has no matching open
    Ajoy Trade record.  These are NOT managed by the normal exit logic, so we
    add a safety net here: if an orphan's current price has dropped ≥
    STOP_LOSS_PCT below its cost-per-unit, we place a market sell immediately.

    This runs at the end of every manage_open_trades() cycle.
    """
    try:
        positions = await client.get_positions()
    except Exception as exc:
        logger.warning("orphan stop-check: could not fetch Tradier positions: %s", exc)
        return

    if not positions:
        return

    # Build exclusion set from config (e.g. ORPHAN_STOP_EXCLUDED_SYMBOLS=PLTR,HOOD)
    # Matches against the underlying ticker prefix of the OCC option symbol.
    excluded_tickers: set[str] = {
        s.strip().upper()
        for s in settings.orphan_stop_excluded_symbols.split(",")
        if s.strip()
    }

    # Collect all open Ajoy option symbols (set for O(1) lookup)
    result = await db.execute(select(Trade).where(Trade.status == TradeStatus.OPEN))
    ajoy_symbols: set[str] = {t.option_symbol for t in result.scalars().all()}

    for pos in positions:
        if pos.symbol in ajoy_symbols:
            continue  # managed by normal exit logic

        # Skip positions the user has explicitly excluded (manual holds)
        if excluded_tickers:
            import re as _re
            m = _re.match(r'^([A-Z]+)', pos.symbol)
            underlying = m.group(1) if m else ""
            if underlying in excluded_tickers:
                logger.debug(
                    "[ORPHAN] Skipping %s — %s is in ORPHAN_STOP_EXCLUDED_SYMBOLS",
                    pos.symbol, underlying,
                )
                continue

        qty = pos.quantity
        cost_basis_total = pos.cost_basis or 0.0
        cost_per_unit = cost_basis_total / (qty * 100) if qty else 0.0
        if cost_per_unit <= 0:
            continue

        # Fetch current option price
        try:
            q = await client.get_option_quote(pos.symbol)
            if not q:
                continue
            current = (q.bid + q.ask) / 2 if q.bid and q.ask else q.last
            if not current:
                continue
        except Exception as exc:
            logger.warning(
                "orphan stop-check: could not quote %s: %s", pos.symbol, exc
            )
            continue

        loss_pct = (cost_per_unit - current) / cost_per_unit
        if loss_pct < settings.stop_loss_pct:
            continue  # still above stop threshold — no action

        logger.info(
            "[ORPHAN] Auto-stop triggered for %s x%d: cost=%.4f current=%.4f "
            "loss=%.1f%% (threshold %.1f%%) — placing market sell",
            pos.symbol, qty,
            cost_per_unit, current,
            loss_pct * 100, settings.stop_loss_pct * 100,
        )
        try:
            await client.place_option_order(
                option_symbol=pos.symbol,
                side="sell_to_close",
                quantity=qty,
                order_type="market",
            )
            logger.info(
                "[ORPHAN] Market sell submitted for %s x%d", pos.symbol, qty
            )
        except Exception as exc:
            logger.error(
                "[ORPHAN] Failed to auto-stop %s: %s", pos.symbol, exc, exc_info=True
            )


async def _reconcile_external_close(
    db, client, trade: Trade, fallback_price: float | None = None
) -> bool:
    """
    A sell_to_close was REJECTED — usually because the position no longer
    exists at the broker (closed manually in the Tradier UI, or the disaster
    stop filled while the bot was down — SOFI #137 got stuck OPEN this way,
    with both the Close button and the startup cleanup failing repeatedly).

    Verify the position is really gone and, if confirmed, reconcile the DB
    record with the actual external fill.

    Safety rule: we only close when the absence is POSITIVELY confirmed —
    either other positions exist (so an empty match isn't an API hiccup) or
    an actual filled sell order for this contract is found in the account's
    order history.  Otherwise the trade stays OPEN and the caller keeps its
    original error handling.
    """
    try:
        positions = await client.get_positions()
    except Exception:
        return False   # can't verify — leave OPEN
    if any(p.symbol == trade.option_symbol for p in positions):
        return False   # genuinely still live

    ext_fill = None
    try:
        ext_fill = await client.get_last_sell_fill(trade.option_symbol)
    except Exception:
        pass

    if not positions and ext_fill is None:
        # Empty account AND no trace of a closing fill — cannot positively
        # confirm the external close; don't risk marking a live position flat.
        return False

    # Cancel any remaining resting orders — zombies otherwise.
    for _attr in ("stop_order_id", "tp_order_id"):
        _oid = getattr(trade, _attr)
        if _oid:
            try:
                await client.cancel_order(_oid)
            except Exception:
                pass
            setattr(trade, _attr, None)

    qty = trade.remaining_qty or trade.quantity
    rounded = round(ext_fill or fallback_price or trade.entry_price, 2)
    trade.status        = TradeStatus.CLOSED
    trade.exit_price    = rounded
    trade.exit_time     = datetime.now(tz=timezone.utc)
    trade.exit_reason   = ExitReason.MANUAL   # closed outside Ajoy
    trade.pnl           = round(
        (trade.pnl or 0) + (rounded - trade.entry_price) * qty * 100, 2
    )
    trade.remaining_qty = 0
    await db.commit()
    logger.info(
        "[%s] Trade %d was already closed at the broker — reconciled as "
        "MANUAL @ $%.2f (%s fill)",
        trade.symbol, trade.id, rounded,
        "actual" if ext_fill else "estimated",
    )
    return True


async def _finalize_broker_stop_close(db, client, trade: Trade) -> bool:
    """
    Record the DB close for a trade whose broker-side resting stop order filled.
    The position is already flat at the broker — no sell order is placed here.
    """
    fill = await client.get_fill_price(trade.stop_order_id)
    exit_price = fill or trade.stop_price or trade.entry_price
    qty = trade.remaining_qty or trade.quantity

    original_stop = trade.original_stop_price if trade.original_stop_price \
                    else round(trade.entry_price * (1 - settings.stop_loss_pct), 2)
    reason = (
        ExitReason.TRAILING_STOP
        if (trade.stop_price or 0) > original_stop
        else ExitReason.STOP
    )

    rounded_exit      = round(exit_price, 2)
    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = rounded_exit
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = reason
    trade.pnl         = round(
        (trade.pnl or 0) + (rounded_exit - trade.entry_price) * qty * 100, 2
    )
    await db.commit()
    logger.info(
        "[%s] Trade %d CLOSED via broker-side %s @ $%.2f  PnL=$%.2f",
        trade.symbol, trade.id, reason.value, rounded_exit, trade.pnl,
    )
    return True


async def _reconcile_broker_stop(db, client, trade: Trade) -> bool:
    """
    Check the broker-side resting stop order's status.

    filled                       → close the DB trade, return True
    canceled/rejected/expired    → clear stop_order_id (loudly), return False
    open/pending/anything else   → return False (no action)
    """
    try:
        status_data = await client.get_order_status(trade.stop_order_id)
        status_str  = (status_data.get("status") or "").lower()
    except Exception as exc:
        logger.warning(
            "[%s] Trade %d: could not check broker stop %s: %s",
            trade.symbol, trade.id, trade.stop_order_id, exc,
        )
        return False

    if status_str == "filled":
        return await _finalize_broker_stop_close(db, client, trade)

    if status_str in ("canceled", "cancelled", "rejected", "expired"):
        logger.warning(
            "[%s] Trade %d: broker stop %s is %s — clearing it. "
            "Bot-side stop is now the only protection for this position.",
            trade.symbol, trade.id, trade.stop_order_id, status_str.upper(),
        )
        trade.stop_order_id = None
        await db.commit()

    return False


async def _reconcile_broker_tp(db, client, trade: Trade) -> bool:
    """
    Check whether the broker-side resting TP limit order has filled.

    filled                       → close the DB trade as TP2, return True
    canceled/rejected/expired    → clear tp_order_id (loudly), return False
    open/pending/anything else   → return False (no action)
    """
    try:
        status_data = await client.get_order_status(trade.tp_order_id)
        status_str  = (status_data.get("status") or "").lower()
    except Exception as exc:
        logger.warning(
            "[%s] Trade %d: could not check broker TP %s: %s",
            trade.symbol, trade.id, trade.tp_order_id, exc,
        )
        return False

    if status_str == "filled":
        fill_price = await client.get_fill_price(trade.tp_order_id)
        exit_price = fill_price or trade.tp1_price or trade.tp2_price or trade.entry_price
        qty = trade.remaining_qty or trade.quantity
        trade.status      = TradeStatus.CLOSED
        trade.exit_price  = round(exit_price, 2)
        trade.exit_time   = datetime.now(tz=timezone.utc)
        trade.exit_reason = ExitReason.TP2
        trade.pnl         = round(
            (trade.pnl or 0) + (trade.exit_price - trade.entry_price) * qty * 100, 2
        )
        trade.tp_order_id = None
        await db.commit()
        logger.info(
            "[%s] Trade %d CLOSED via broker-side TP @ $%.2f  PnL=$%.2f",
            trade.symbol, trade.id, trade.exit_price, trade.pnl,
        )
        return True

    if status_str in ("canceled", "cancelled", "rejected", "expired"):
        logger.warning(
            "[%s] Trade %d: broker TP %s is %s — clearing it. "
            "Bot-side TP check is now the only exit for this position.",
            trade.symbol, trade.id, trade.tp_order_id, status_str.upper(),
        )
        trade.tp_order_id = None
        await db.commit()

    return False


# Exits where price is moving against the position fast — never wait on a
# limit for these; the guaranteed market exit is worth the half-spread.
_URGENT_EXIT_REASONS = {
    ExitReason.STOP,
    ExitReason.QUICK_LOSS,
    ExitReason.VWAP_BREAK,
    ExitReason.SIGNAL_FADE,
}


@dataclass
class _ExitSellResult:
    order: object | None = None   # order whose fill Step 2 must verify
    was_limit: bool = False       # True = fill came from a limit (may beat the bid)
    failed: bool = False          # API failure — leave trade OPEN and retry
    already_flat: bool = False    # limit fill(s) closed the whole position
    flat_price: float = 0.0       # avg fill price when already_flat


async def _execute_exit_sell(db, client, trade: Trade, qty: int, reason: ExitReason) -> _ExitSellResult:
    """
    Place the sell_to_close for an exit, mirroring the entry's limit-at-mid
    logic for PATIENT exits:

      1. Fresh quote → limit sell at the mid.
      2. Poll every 2 s up to exit_limit_timeout_seconds.
      3. Unfilled → cancel, re-check status (a fill can race the cancel):
           • filled           → done (was_limit=True)
           • partially filled → book the filled slice into trade.pnl /
                                remaining_qty, market-sell the remainder
           • unfilled         → market-sell the full quantity
    URGENT exits (STOP / QUICK_LOSS / VWAP_BREAK / SIGNAL_FADE) use a
    MARKETABLE limit at bid × (1 − urgent_exit_limit_pct): fills like a
    market order in normal tape but caps the worst fill 3% below bid — raw
    market sells on fast moves paid full spread-at-velocity (CRM/COIN ~$36
    extra each).  Short 6 s timeout, then true market.
    """
    async def _market(q: int):
        return await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=q,
            order_type="market",
        )

    urgent = reason in _URGENT_EXIT_REASONS

    # ── Choose limit price + timeout by urgency ──────────────────────────
    limit_price: float | None = None
    timeout = settings.exit_limit_timeout_seconds
    want_limit = (
        (urgent and settings.urgent_exit_limit_enabled)
        or (not urgent and settings.exit_limit_orders_enabled)
    )
    if want_limit:
        try:
            q = await client.get_option_quote(trade.option_symbol)
            if q and q.bid and q.ask and q.bid > 0 and q.ask > 0:
                if urgent:
                    limit_price = round(q.bid * (1 - settings.urgent_exit_limit_pct), 2)
                    timeout = settings.urgent_exit_limit_timeout_seconds
                else:
                    limit_price = round((q.bid + q.ask) / 2, 2)
        except Exception:
            limit_price = None

    if not limit_price or limit_price <= 0:
        try:
            return _ExitSellResult(order=await _market(qty))
        except Exception as exc:
            logger.error(
                "[%s] Trade %d: market sell_to_close failed — leaving OPEN to retry. %s",
                trade.symbol, trade.id, exc,
            )
            return _ExitSellResult(failed=True)

    # ── Limit (mid for patient, marketable bid−3% for urgent) ────────────
    try:
        limit_order = await client.place_option_order(
            option_symbol=trade.option_symbol,
            side="sell_to_close",
            quantity=qty,
            order_type="limit",
            limit_price=limit_price,
        )
    except Exception as exc:
        logger.warning(
            "[%s] Trade %d: exit limit placement failed (%s) — using market",
            trade.symbol, trade.id, exc,
        )
        try:
            return _ExitSellResult(order=await _market(qty))
        except Exception as exc2:
            logger.error(
                "[%s] Trade %d: market sell_to_close failed — leaving OPEN to retry. %s",
                trade.symbol, trade.id, exc2,
            )
            return _ExitSellResult(failed=True)

    logger.info(
        "[%s] Trade %d: exit limit placed at $%.2f (%s, %s) — waiting up to %ds",
        trade.symbol, trade.id, limit_price, reason.value,
        "marketable bid−%.0f%%" % (settings.urgent_exit_limit_pct * 100) if urgent else "mid",
        timeout,
    )

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=timeout)
    status_str = "unknown"
    while datetime.now(tz=timezone.utc) < deadline:
        await asyncio.sleep(2)
        try:
            sd = await client.get_order_status(limit_order.order_id)
            status_str = (sd.get("status") or "").lower()
        except Exception:
            status_str = "unknown"
        if status_str == "filled":
            logger.info(
                "[%s] Trade %d: exit limit %s filled",
                trade.symbol, trade.id, limit_order.order_id,
            )
            return _ExitSellResult(order=limit_order, was_limit=not urgent)
        if status_str in ("rejected", "canceled", "cancelled"):
            break

    # ── Timeout / rejected → cancel and resolve the race ─────────────────
    if status_str not in ("rejected", "canceled", "cancelled"):
        try:
            await client.cancel_order(limit_order.order_id)
        except Exception:
            pass

    exec_qty, avg_fill = 0, 0.0
    try:
        sd = await client.get_order_status(limit_order.order_id)
        status_str = (sd.get("status") or "").lower()
        exec_qty = int(float(sd.get("exec_quantity") or 0))
        avg_fill = float(sd.get("avg_fill_price") or 0)
    except Exception:
        status_str = "unknown"

    if status_str == "filled":
        # Fill won the race against the cancel
        return _ExitSellResult(order=limit_order, was_limit=not urgent)

    if exec_qty > 0 and avg_fill > 0:
        # Partial fill — book the filled slice now so a retry can never
        # double-sell it, then market-sell only what remains.
        booked = min(exec_qty, qty)
        trade.remaining_qty = qty - booked
        trade.pnl = round(
            (trade.pnl or 0) + (round(avg_fill, 2) - trade.entry_price) * booked * 100, 2
        )
        await db.commit()
        logger.info(
            "[%s] Trade %d: exit limit partially filled %d/%d @ $%.2f — "
            "market-selling remaining %d",
            trade.symbol, trade.id, booked, qty, avg_fill, trade.remaining_qty,
        )
        if trade.remaining_qty <= 0:
            return _ExitSellResult(already_flat=True, flat_price=round(avg_fill, 2))

    remaining = trade.remaining_qty if (exec_qty > 0 and avg_fill > 0) else qty
    try:
        return _ExitSellResult(order=await _market(remaining))
    except Exception as exc:
        logger.error(
            "[%s] Trade %d: market fallback after limit timeout failed — "
            "leaving OPEN to retry. %s",
            trade.symbol, trade.id, exc,
        )
        return _ExitSellResult(failed=True)


async def _close_trade(
    db,
    client,
    trade: Trade,
    reason: ExitReason,
    exit_price: float | None = None,
) -> bool:
    """
    Place a sell_to_close order and close the trade in the DB.

    Returns True on success, False if the order was rejected/failed
    (DB record is left OPEN so the next manage cycle can retry).

    Exit-price priority
    -------------------
    1. Caller-supplied production mid-quote (most accurate — this is the real
       market price that triggered the exit signal).  Passed for TP2, STOP,
       VWAP_BREAK, TREND_REVERSAL.
    2. Sandbox fill price — used ONLY when no production price is available
       (e.g. CUTOFF, where we close without a preceding quote check).
       NOTE: sandbox fills are *synthetic* and diverge from real market prices,
       so they must not override a known production quote.
    3. Fresh production mid-quote fetched here as last resort.
    4. Entry price — absolute worst case so P&L records as 0.
    """
    qty = trade.remaining_qty or trade.quantity

    # ── Step 0: cancel the broker-side resting stop first ───────────────────
    # The resting sell order reserves the contracts — placing a second
    # sell_to_close while it is live risks rejection or a double-sell.
    # A fill can race the cancel, so re-check status after canceling.
    if trade.stop_order_id:
        try:
            await client.cancel_order(trade.stop_order_id)
        except Exception as exc:
            logger.warning(
                "[%s] Trade %d: cancel of broker stop %s failed: %s",
                trade.symbol, trade.id, trade.stop_order_id, exc,
            )
        try:
            st_data = await client.get_order_status(trade.stop_order_id)
            st_str  = (st_data.get("status") or "").lower()
        except Exception:
            st_str = "unknown"
        if st_str == "filled":
            # Stop won the race — position already flat; record that close.
            logger.info(
                "[%s] Trade %d: broker stop filled during cancel window — "
                "recording broker-stop exit instead of %s",
                trade.symbol, trade.id, reason.value,
            )
            return await _finalize_broker_stop_close(db, client, trade)
        if st_str not in ("canceled", "cancelled", "expired", "rejected"):
            # Cancel not yet confirmed — don't risk a double-sell.  Leave the
            # trade OPEN; the next manage cycle will retry the whole exit.
            logger.warning(
                "[%s] Trade %d: broker stop %s still %s after cancel — "
                "deferring exit to next cycle",
                trade.symbol, trade.id, trade.stop_order_id, st_str.upper(),
            )
            return False
        trade.stop_order_id = None
        await db.commit()

    # ── Step 0b: cancel broker-side TP limit order (if any) ─────────────────
    # Must cancel before placing the bot exit sell — two resting sell orders
    # on the same position risks a double-sell rejection.
    if trade.tp_order_id:
        try:
            await client.cancel_order(trade.tp_order_id)
        except Exception as exc:
            logger.warning(
                "[%s] Trade %d: cancel of broker TP %s failed: %s",
                trade.symbol, trade.id, trade.tp_order_id, exc,
            )
        try:
            tp_st_data = await client.get_order_status(trade.tp_order_id)
            tp_st_str  = (tp_st_data.get("status") or "").lower()
        except Exception:
            tp_st_str = "unknown"
        if tp_st_str == "filled":
            # TP order won the race — position already flat, record as TP2 exit.
            logger.info(
                "[%s] Trade %d: broker TP filled during cancel window — "
                "recording TP exit instead of %s",
                trade.symbol, trade.id, reason.value,
            )
            fill_price = await client.get_fill_price(trade.tp_order_id)
            trade.exit_price  = fill_price or trade.tp1_price or trade.tp2_price
            trade.exit_time   = datetime.now(tz=timezone.utc)
            trade.exit_reason = ExitReason.TP2
            trade.status      = TradeStatus.CLOSED
            trade.pnl         = round(
                (trade.exit_price - trade.entry_price) * (trade.remaining_qty or trade.quantity) * 100, 2
            )
            trade.tp_order_id = None
            await db.commit()
            return True
        trade.tp_order_id = None
        await db.commit()

    # ── Step 1: place the sell order ────────────────────────────────────────
    # Patient exits try a limit at the mid first (same half-spread saving as
    # entries); urgent exits (STOP / QUICK_LOSS / VWAP_BREAK) go straight to
    # market.  See _execute_exit_sell for the timeout / partial-fill handling.
    sell_result = await _execute_exit_sell(db, client, trade, qty, reason)
    if sell_result.failed:
        return False

    if sell_result.already_flat:
        # Limit fill(s) closed the whole position; the P&L for those fills was
        # already booked into trade.pnl by _execute_exit_sell.
        trade.status      = TradeStatus.CLOSED
        trade.exit_price  = round(sell_result.flat_price, 2)
        trade.exit_time   = datetime.now(tz=timezone.utc)
        trade.exit_reason = reason
        trade.remaining_qty = 0
        await db.commit()
        logger.info(
            "[%s] Trade %d CLOSED via %s @ $%.2f (exit limit)  PnL=$%.2f",
            trade.symbol, trade.id, reason.value, trade.exit_price, trade.pnl,
        )
        return True

    sell_order = sell_result.order
    was_limit  = sell_result.was_limit
    # A partial limit fill may have shrunk the open quantity — refresh it so
    # Step 4 books P&L for exactly what this final order closes.
    qty = trade.remaining_qty or trade.quantity

    # ── Step 2: confirm the sell order actually filled ──────────────────────
    # We must verify the order reached "filled" status before closing the DB
    # record.  In production, market orders usually fill within milliseconds,
    # but pending/submitted/partially_filled states can occur on illiquid options.
    # If we close the DB record before the fill is confirmed we believe the
    # position is flat while a real live position still exists in Tradier.
    fill = await client.get_fill_price(sell_order.order_id)

    try:
        order_status = await client.get_order_status(sell_order.order_id)
        status_str   = (order_status.get("status") or "").lower()
    except Exception:
        status_str = "unknown"

    if status_str in ("rejected", "canceled", "cancelled"):
        # A rejected sell usually means the position no longer exists at the
        # broker (manual close / disaster-stop fill).  Verify and reconcile
        # instead of leaving the trade stuck OPEN (SOFI #137).
        if await _reconcile_external_close(db, client, trade, fallback_price=exit_price):
            return True
        logger.error(
            "[%s] Trade %d: sell order %s was %s — NOT closing in DB. "
            "Position is still live in Tradier. Manual intervention may be required.",
            trade.symbol, trade.id, sell_order.order_id, status_str.upper(),
        )
        return False  # Leave the trade OPEN so it shows up in Open Positions

    if status_str != "filled" and not fill:
        # Order placed but not yet confirmed as filled (pending / submitted /
        # partially_filled / unknown).  Leave the trade OPEN — the next
        # manage_open_trades cycle will call _close_trade again and retry.
        logger.warning(
            "[%s] Trade %d: sell order %s status=%s — not yet filled. "
            "Leaving trade OPEN to retry on next management cycle.",
            trade.symbol, trade.id, sell_order.order_id, status_str.upper(),
        )
        return False

    # ── Step 3: resolve exit price — actual Tradier fill is most accurate ────
    # Prefer the actual fill price from Tradier — it reflects what the
    # position was sold for (bid price for market sells, slightly below mid).
    #
    # Sanity-check the fill against the trigger quote.
    # The trigger quote is the option price at the moment the exit condition
    # fired.  By the time the market order routes to the exchange, the option
    # can have moved significantly — especially for near-expiry ATM options
    # where a fast underlying reversal can cause a 50-100% price bounce in
    # a matter of seconds.
    #
    # In LIVE mode (USE_SANDBOX=0) the exchange fill IS the authoritative
    # price — use generous bounds so legitimate bounces aren't discarded.
    # In SANDBOX mode prices can be stale; apply tighter bounds.
    #
    # Historical bug: bounds of 2% upper / 12% lower caused a real $2.10
    # fill (option bounced from $1.05 trigger) to be rejected and replaced
    # with the $1.05 trigger quote, overstating the loss by ~$250.
    if settings.use_sandbox:
        _FILL_UPPER_PCT = 0.02   # sandbox: price should stay near trigger
        _FILL_LOWER_PCT = 0.12
    else:
        _FILL_UPPER_PCT = 1.00   # live: option can double between check and fill
        _FILL_LOWER_PCT = 0.50   # live: can lose half in fast gap-down
    if fill and exit_price:
        signed_dev = (fill - exit_price) / exit_price   # positive = fill above trigger
        if signed_dev > _FILL_UPPER_PCT and not was_limit:
            logger.warning(
                "[%s] Trade %d exit: fill $%.2f is %.0f%% ABOVE trigger $%.2f "
                "— market sells can't improve above bid, using trigger quote",
                trade.symbol, trade.id, fill, signed_dev * 100, exit_price,
            )
            fill = None
        elif signed_dev < -_FILL_LOWER_PCT:
            logger.warning(
                "[%s] Trade %d exit: fill $%.2f deviates %.0f%% below trigger $%.2f "
                "— likely stale sandbox price, using trigger quote",
                trade.symbol, trade.id, fill, abs(signed_dev) * 100, exit_price,
            )
            fill = None
        elif abs(signed_dev) > 0.01:
            logger.info(
                "[%s] Trade %d exit: actual fill $%.2f vs trigger quote $%.2f "
                "(diff %+.2f — using fill price for P&L)",
                trade.symbol, trade.id, fill, exit_price, fill - exit_price,
            )
    if fill:
        exit_price = fill
    elif not exit_price:
        try:
            q = await client.get_option_quote(trade.option_symbol)
            if q:
                exit_price = (q.bid + q.ask) / 2 if q.bid and q.ask else q.last
        except Exception:
            pass
    if not exit_price:
        exit_price = trade.entry_price  # worst-case: record at cost

    # ── Step 4: persist the close ────────────────────────────────────────────
    partial_pnl      = trade.pnl or 0
    rounded_exit     = round(exit_price, 2)   # round FIRST, then use same value for P&L
    close_pnl        = (rounded_exit - trade.entry_price) * qty * 100

    trade.status      = TradeStatus.CLOSED
    trade.exit_price  = rounded_exit
    trade.exit_time   = datetime.now(tz=timezone.utc)
    trade.exit_reason = reason
    trade.pnl         = round(partial_pnl + close_pnl, 2)
    await db.commit()
    logger.info(
        "[%s] Trade %d CLOSED via %s @ $%.2f  PnL=$%.2f",
        trade.symbol, trade.id, reason.value, exit_price, trade.pnl,
    )
    return True


# ---------------------------------------------------------------------------
# Startup orphan close
# ---------------------------------------------------------------------------

async def cancel_stale_entry_orders() -> None:
    """
    Startup sweep: cancel every resting BUY order at the broker.

    An entry limit the bot placed but failed to cancel (API error, process
    killed mid-poll, weekend queueing) is a time bomb — it can fill at a
    later session's open with stale pricing and no managing trade record
    (Jul 18 Saturday orders → Jul 20 ghost fills, −$373).  Entry orders are
    only ever meant to live for limit_order_timeout_seconds, so ANY resting
    buy at startup is by definition stale.  Sell orders (disaster stops for
    adopted/open trades) are left untouched.
    """
    for acct in await all_account_views():
        await _cancel_stale_entry_orders_account(acct)


async def _cancel_stale_entry_orders_account(acct: AccountView) -> None:
    client = get_tradier_client(acct)
    _t = _tag(client)
    try:
        pending = await client.get_open_orders()
    except Exception as exc:
        logger.warning("%s[startup] Entry-order sweep: could not list orders: %s",
                       _t, exc)
        return
    for o in pending:
        side = (o.get("side") or "").lower()
        if "buy" not in side:
            continue
        oid = str(o.get("id", ""))
        if not oid:
            continue
        try:
            await client.cancel_order(oid)
            logger.warning(
                "%s[startup] Cancelled STALE resting buy order %s (%s %s x%s) — "
                "orphaned entry orders must never survive into a session",
                _t, oid, o.get("option_symbol") or o.get("symbol", "?"),
                side, o.get("quantity", "?"),
            )
        except Exception as exc:
            logger.error(
                "%s[startup] Could not cancel stale buy order %s: %s — "
                "CHECK TRADIER MANUALLY",
                _t, oid, exc,
            )


async def close_orphaned_open_trades() -> None:
    """
    On bot startup, close any OPEN trades that survived past the force-close
    window.  This handles the case where the bot was stopped/restarted after
    15:16 ET — the scheduler's cutoff job never ran, leaving live positions
    dangling in Tradier overnight.

    Fires once immediately when the scheduler starts.  Safe to run multiple
    times: trades already CLOSED are skipped.
    """
    for acct in await all_account_views():
        try:
            await _close_orphaned_open_trades_account(acct)
        except Exception as exc:
            logger.error(
                "[startup] Orphan close failed for account '%s': %s",
                acct.name, exc, exc_info=True,
            )


async def _close_orphaned_open_trades_account(acct: AccountView) -> None:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=ET)
    cutoff_h = settings.cutoff_hour
    cutoff_m = settings.cutoff_minute

    async with AsyncSessionLocal() as db:
        result = await db.execute(_scope(
            select(Trade).where(Trade.status == TradeStatus.OPEN), acct))
        open_trades = result.scalars().all()

    # S3 stock trades are never closed via the option sell path — the S3
    # engine flattens its own positions (and reconciles on its own startup).
    open_trades = [t for t in open_trades if t.strategy_name != "S3"]

    if not open_trades:
        return

    past_cutoff = (
        now_et.hour > cutoff_h
        or (now_et.hour == cutoff_h and now_et.minute >= cutoff_m)
    )

    # Also close on weekend / outside market hours where no new bars will arrive
    # (is_market_open checks the calendar via strategy helpers).
    # Simple time check: outside 09:30–16:00 ET on weekdays counts as "closed".
    market_closed = not is_market_open()

    if not (past_cutoff or market_closed):
        logger.debug(
            "[startup] %d open trade(s) found — within trading window, no orphan close needed",
            len(open_trades),
        )
        return

    client = get_tradier_client(acct)
    logger.warning(
        "%s[startup] %d orphaned open trade(s) found past cutoff / market closed — "
        "force-closing now",
        _tag(client), len(open_trades),
    )
    async with AsyncSessionLocal() as db:
        for trade in open_trades:
            # Re-fetch inside session
            result = await db.execute(select(Trade).where(Trade.id == trade.id))
            t = result.scalar_one_or_none()
            if not t or t.status != TradeStatus.OPEN:
                continue
            logger.info(
                "[startup] Force-closing orphaned trade %d %s %s (entered %s ET)",
                t.id, t.symbol, t.direction.value, t.entry_time,
            )
            closed = await _close_trade(db, client, t, ExitReason.CUTOFF)
            if not closed:
                logger.error(
                    "[startup] Could not close orphaned trade %d %s — "
                    "manual intervention required in Tradier",
                    t.id, t.symbol,
                )


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

async def _startup_tasks() -> None:
    """
    Run every one-shot startup task in order, isolating failures.

    Order matters: cancel stale resting BUY orders BEFORE closing orphans, so
    a stale entry cannot fill into a position we are in the middle of
    flattening.
    """
    for name, job in (
        ("account roster",     _log_account_roster),
        ("stale-order sweep",  cancel_stale_entry_orders),
        ("orphan close",       close_orphaned_open_trades),
    ):
        try:
            await job()
        except Exception as exc:
            logger.error("[startup] %s failed: %s", name, exc, exc_info=True)


async def _log_account_roster() -> None:
    """Print one startup line per account: mode, strategies, sizing, slots."""
    try:
        accounts = await all_account_views()
    except Exception as exc:
        logger.warning("[accounts] Could not list accounts at startup: %s", exc)
        return
    for a in accounts:
        strategies = [
            name for name, on in (
                ("S1", a.s1_enabled and True),
                ("S2", a.s2_enabled and settings.s2_enabled),
                ("PS", a.put_scalp_enabled and settings.put_scalp_enabled),
                ("S3", a.s3_enabled and settings.s3_enabled),
            ) if on
        ]
        logger.info(
            "[accounts] %-14s %-7s acct=%-10s %-4s → %s | risk S1 $%.0f / S2 $%.0f | "
            "slots S1 %d / S2 %d / PS %d",
            a.name,
            "ENABLED" if a.enabled else "PAUSED",
            a.account_number or "?",
            "SBOX" if a.use_sandbox else "LIVE",
            ", ".join(strategies) or "none",
            float(a.setting("risk_per_trade")),
            float(a.setting("s2_risk_per_trade")),
            int(a.setting("max_open_trades")),
            int(a.setting("s2_max_open_trades")),
            int(a.setting("put_scalp_max_open")),
        )


def start_scheduler() -> None:
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled via config")
        return

    # Show the dual-environment config at startup so logs are self-explanatory.
    # (Old version always printed the sandbox URL string regardless of the
    # flag — caused a false alarm during the Jul 20 ghost-trade incident.)
    logger.info(
        "Tradier: market data → %s | .env orders → %s [%s] (account %s)",
        settings.tradier_base_url,
        settings.tradier_base_url_sandbox if settings.use_sandbox
        else settings.tradier_base_url,
        "SANDBOX" if settings.use_sandbox else "LIVE",
        settings.tradier_account_id_sandbox if settings.use_sandbox
        else settings.tradier_account_id,
    )

    scheduler.add_job(
        scan_for_entries, "interval",
        seconds=settings.scan_interval_seconds,
        id="scan_entries", replace_existing=True,
    )
    scheduler.add_job(
        scan_for_entries_s2, "interval",
        seconds=settings.s2_scan_interval_seconds,
        id="scan_entries_s2", replace_existing=True,
    )
    # PUT Scalp scanner (Jul 23 2026) — no-ops unless PUT_SCALP_ENABLED.
    # Reuses the S1 scan cadence; the function itself guards calendar+clock.
    scheduler.add_job(
        scan_for_put_scalp, "interval",
        seconds=settings.scan_interval_seconds,
        id="scan_put_scalp", replace_existing=True,
    )
    scheduler.add_job(
        manage_open_trades, "interval",
        seconds=settings.manage_interval_seconds,
        id="manage_trades", replace_existing=True,
    )
    # ── One-shot startup work, run SEQUENTIALLY in a single job ──────────
    # These used to be two independent "date" jobs.  With multi-account each
    # of them now iterates every account (DB reads + a Tradier round-trip per
    # account), and running them concurrently made them contend with each
    # other — and with the first scan tick — over the same SQLite file.
    # Boot-time work has no reason to be parallel, so it is one job now:
    #   1. log the account roster
    #   2. cancel stale resting BUY orders (Jul 20 ghost-trade lesson)
    #   3. force-close trades left open past the cutoff
    scheduler.add_job(
        _startup_tasks, "date",
        run_date=datetime.now(tz=scheduler.timezone),
        id="startup_tasks", replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — scan every %ds, manage every %ds, window %s–%s ET  "
        "| regime gate=%s (%s) | IV max=%.0f%% | cooldown=%dm | "
        "sym loss cap=%d/day",
        settings.scan_interval_seconds, settings.manage_interval_seconds,
        settings.trading_start_time, settings.trading_end_time,
        "ON" if settings.regime_gate_enabled else "OFF",
        settings.regime_gate_symbol,
        settings.iv_max_threshold * 100,
        settings.cooldown_minutes,
        settings.max_losses_per_symbol_per_day,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
