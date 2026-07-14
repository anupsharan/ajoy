"""RiskManager — position sizing and every pre-trade / kill-switch gate.

Full size:  shares = floor(max_risk_dollars / estimated_per_share_risk)
then capped by buying power, notional, liquidity participation, per-symbol
and portfolio limits.  All state here is engine-thread-local.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.s3.bars import BarAggregator
from app.services.s3.book_analyzer import tick_size
from app.services.s3.normalizer import MarketDataNormalizer
from app.services.s3.types import BookSnapshot

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class AccountSnapshot:
    buying_power: float = 0.0
    ts: float = 0.0


@dataclass
class DayStats:
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    trades_per_symbol: dict[str, int] = field(default_factory=dict)
    last_exit_ts: dict[str, float] = field(default_factory=dict)

    def record_exit(self, symbol: str, pnl: float, ts: float) -> None:
        self.realized_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self.last_exit_ts[symbol] = ts

    def record_entry(self, symbol: str) -> None:
        self.trades_per_symbol[symbol] = self.trades_per_symbol.get(symbol, 0) + 1


@dataclass
class SizeDecision:
    shares: int
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.shares > 0


class RiskManager:
    def __init__(self, bars: BarAggregator, normalizer: MarketDataNormalizer) -> None:
        self._bars = bars
        self._normalizer = normalizer
        self.account = AccountSnapshot()
        self.day = DayStats()
        self.halted_reason: str | None = None

    # ── Session gates ────────────────────────────────────────────
    @staticmethod
    def _hhmm(value: str) -> tuple[int, int]:
        h, m = value.split(":")
        return int(h), int(m)

    def in_entry_window(self, now_et: datetime | None = None) -> bool:
        now_et = now_et or datetime.now(ET)
        t = now_et.time()
        sh, sm = self._hhmm(settings.s3_trading_start_time)
        eh, em = self._hhmm(settings.s3_last_entry_time)
        return (t.hour, t.minute) >= (sh, sm) and (t.hour, t.minute) < (eh, em)

    def past_flatten_time(self, now_et: datetime | None = None) -> bool:
        now_et = now_et or datetime.now(ET)
        fh, fm = self._hhmm(settings.s3_flatten_time)
        return (now_et.hour, now_et.minute) >= (fh, fm)

    # ── Halt state ───────────────────────────────────────────────
    def check_halts(self) -> str | None:
        """Returns the halt reason (and latches it) or None."""
        if settings.s3_kill_switch:
            self.halted_reason = "KILL_SWITCH"
        elif settings.s3_max_daily_loss > 0 and self.day.realized_pnl <= -settings.s3_max_daily_loss:
            self.halted_reason = "MAX_DAILY_LOSS"
        elif (
            settings.s3_max_consecutive_losses > 0
            and self.day.consecutive_losses >= settings.s3_max_consecutive_losses
        ):
            self.halted_reason = "CONSECUTIVE_LOSSES"
        return self.halted_reason

    # ── Entry gates (cheap → expensive) ──────────────────────────
    def entry_blocked(
        self,
        symbol: str,
        book: BookSnapshot,
        open_positions: int,
        portfolio_notional: float,
        now_ts: float,
    ) -> str | None:
        if self.check_halts():
            return self.halted_reason
        if not self.in_entry_window():
            return "OUTSIDE_WINDOW"
        if open_positions >= settings.s3_max_open_positions:
            return "MAX_OPEN_POSITIONS"
        if self.day.trades_per_symbol.get(symbol, 0) >= settings.s3_max_trades_per_symbol:
            return "MAX_TRADES_SYMBOL"
        last_exit = self.day.last_exit_ts.get(symbol, 0.0)
        if last_exit and now_ts - last_exit < settings.s3_cooldown_minutes * 60:
            return "COOLDOWN"
        if portfolio_notional >= settings.s3_max_portfolio_notional:
            return "PORTFOLIO_NOTIONAL"
        if self._normalizer.data_age_ms(symbol) > settings.s3_stale_data_max_ms:
            return "STALE_DATA"
        spread = book.spread
        if spread is None or book.best_ask is None:
            return "NO_QUOTE"
        px = book.best_ask.price
        if spread > max(
            settings.s3_max_spread_ticks * tick_size(px),
            settings.s3_max_spread_pct * px,
        ):
            return "SPREAD_TOO_WIDE"
        return None

    # ── Stop sanity ──────────────────────────────────────────────
    @staticmethod
    def stop_valid(entry_px: float, stop_px: float) -> str | None:
        ts_ = tick_size(entry_px)
        dist = entry_px - stop_px
        if dist < settings.s3_min_stop_ticks * ts_:
            return "STOP_TOO_TIGHT"
        if dist > settings.s3_max_stop_pct * entry_px:
            return "STOP_TOO_WIDE"
        return None

    # ── Sizing ───────────────────────────────────────────────────
    def size_position(self, symbol: str, entry_px: float, stop_px: float) -> SizeDecision:
        # Prices are tick-quantized — round away float noise before dividing
        # (50.00 − 49.80 must be exactly 0.20, not 0.20000000000000284).
        per_share_risk = round(entry_px - stop_px, 6)
        if per_share_risk <= 0:
            return SizeDecision(0, "NON_POSITIVE_RISK")

        shares = math.floor(settings.s3_max_risk_dollars / per_share_risk)
        caps: list[tuple[str, int]] = []

        caps.append(("NOTIONAL", math.floor(settings.s3_max_notional / entry_px)))
        if self.account.buying_power > 0:
            caps.append((
                "BUYING_POWER",
                math.floor(self.account.buying_power * settings.s3_max_bp_fraction / entry_px),
            ))
        vol_1m = self._bars.recent_volume(symbol, 1)
        if vol_1m > 0:
            caps.append(("PARTICIPATION", math.floor(vol_1m * settings.s3_max_participation)))

        reason = "RISK"
        for name, cap in caps:
            if cap < shares:
                shares, reason = cap, name
        if shares <= 0:
            return SizeDecision(0, f"CAP_{reason}")
        # Economic-viability floor: a 2-3 share position on a $400 stock
        # cannot overcome spread + slippage — skip rather than dabble.
        if settings.s3_min_shares > 0 and shares < settings.s3_min_shares:
            return SizeDecision(
                0, f"BELOW_MIN_SHARES({shares}<{settings.s3_min_shares},{reason})"
            )
        logger.info(
            "[S3][SIZE] %s %d sh (binding=%s, risk/share=%.3f, entry=%.2f, stop=%.2f)",
            symbol, shares, reason, per_share_risk, entry_px, stop_px,
        )
        return SizeDecision(shares, reason)
