from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── Tradier ──────────────────────────────────────────────────
    # Production — market data (quotes, bars, chains, IV, earnings)
    tradier_api_token: str = ""
    tradier_base_url: str = "https://api.tradier.com/v1"

    # Sandbox — order execution + account (paper trading)
    tradier_api_token_sandbox: str = ""
    tradier_base_url_sandbox: str = "https://sandbox.tradier.com/v1"
    tradier_account_id_sandbox: str = ""   # sandbox account (VA...)
    tradier_account_id: str = ""           # live production account

    # ── Trading mode ─────────────────────────────────────────────
    # USE_SANDBOX=1  → paper trading  (sandbox orders, safe default)
    # USE_SANDBOX=0  → LIVE REAL MONEY (production orders)
    # Market data always uses the production API regardless of this flag.
    use_sandbox: bool = True

    # ── Database ─────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./ajoy.db"

    # ── Scheduler ────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scan_interval_seconds: int = 60    # how often to scan for new entries
    manage_interval_seconds: int = 10  # how often to manage open trades

    # ── Trading session window (ET) ──────────────────────────────
    trading_start_time: str = "09:35"  # HH:MM ET — wait 5 min after open
    trading_end_time: str = "14:45"    # HH:MM ET — close ALL open positions at this time
    last_entry_time: str = "14:15"     # HH:MM ET — no NEW entries after this time
    #                                  # gives existing trades time to develop before cutoff
    max_open_trades: int = 3           # max concurrent open positions

    # ── Position sizing ──────────────────────────────────────────
    amount_per_trade: float = 500.0    # USD premium budget cap per trade (options premium)
    max_daily_loss: float = 500.0      # stop trading for the day if P&L < -this

    # Fixed-dollar risk sizing (professional position sizing):
    #   qty = risk_per_trade / (entry_price × stop_loss_pct × 100)
    # so every trade risks ~the same dollar amount at its stop, regardless
    # of premium.  amount_per_trade still caps total premium spent.
    # Set to 0 to disable and fall back to premium-budget sizing.
    risk_per_trade: float = 120.0      # USD at risk per trade if the stop fires

    # ── Broker-side stop orders ──────────────────────────────────
    # After each entry fill, place a resting sell-to-close STOP order at the
    # broker.  If the bot process dies or lags, the broker still exits the
    # position at the stop.  The bot raises this order when the trailing
    # stop moves, and cancels it before any bot-initiated exit.
    broker_stop_enabled: bool = True
    broker_tp_enabled:   bool = False    # place a resting limit sell at TP price; update when target changes

    # ── Risk / reward levels ─────────────────────────────────────
    # Simple percentage-based exits: close 100 % at profit target or stop.
    stop_loss_pct: float = 0.25        # exit if option drops this % below entry (e.g. 0.25 → −25%)
    take_profit_pct: float = 0.35      # exit if option rises this % above entry (e.g. 0.35 → +35%)

    # ── Strategy / indicator params ──────────────────────────────
    ema_period: int = 9                # slow EMA period (default 21 in .env)
    ema_fast_period: int = 9           # fast EMA for dual-EMA alignment gate (default 9 in .env)
    ema_alignment_enabled: bool = True # require EMA(fast) above/below EMA(slow) before entering
    ema_timeframe: str = "15min"
    bounce_bars_required: int = 2
    ema_consecutive_bars: int = 2      # require last N 15-min bars all above/below EMA before entering
    # 1-min EMA disagreement threshold: if the spread between EMA(fast) and
    # EMA(slow) on the 1-min chart is smaller than this fraction, treat the
    # 1-min as NEUTRAL and let the 15-min decide.  Prevents the dual-TF gate
    # from firing on noise (e.g. EMA9=209.51 vs EMA21=209.39 → 0.06% gap).
    # 0.001 = 0.1%  — spreads below this are considered flat / inconclusive.
    # Set to 0.0 to require any disagreement to block (original behaviour).
    ema_1m_min_margin_pct: float = 0.001   # 0.1% minimum spread to call 1-min "trending"
    vwap_band_pct: float = 0.002       # 0.2 % pullback tolerance to VWAP (normal band)
    # Minimum clearance from VWAP before entry is allowed.
    # Stock must be AT LEAST this far on the correct VWAP side to enter.
    # Prevents entries when stock is right at VWAP (AT RISK thesis) — those
    # trades have no directional conviction and consistently go wrong.
    # Matches the thesis_status AT RISK threshold in the UI (0.2% = 0.002).
    # 0.0 = disabled (original behaviour — any distance on correct side is OK).
    vwap_min_clearance_pct: float = 0.002  # 0.2% minimum distance below/above VWAP

    # ── Adaptive VWAP band (QQQ-based) ───────────────────────────
    # When QQQ is extended above its own session VWAP (gap-up days),
    # the normal 0.9% band will block every entry because all tech stocks
    # are similarly extended.  This gate widens the band proportionally
    # so the bot can still find pullbacks relative to VWAP even on
    # strongly trending days.
    #
    # QQQ distance from VWAP → effective entry band used for ALL symbols:
    #   < relaxed_threshold   → vwap_band_pct       (normal  — e.g. 0.9%)
    #   relaxed–wider range   → vwap_band_relaxed_pct (relaxed — e.g. 1.3%)
    #   > wider_threshold     → vwap_band_wider_pct   (wider   — e.g. 1.8%)
    adaptive_band_enabled: bool = True
    adaptive_band_symbol: str = "QQQ"          # reference symbol (must be Nasdaq proxy)
    adaptive_band_relaxed_threshold: float = 0.005   # QQQ >0.5% from VWAP → relaxed band
    adaptive_band_wider_threshold:   float = 0.015   # QQQ >1.5% from VWAP → wider band
    vwap_band_relaxed_pct: float = 0.013       # 1.3% band when QQQ moderately extended
    vwap_band_wider_pct:   float = 0.018       # 1.8% band when QQQ strongly extended

    trend_lookback_days: int = 5       # trading days of 15-min bars to fetch for EMA

    # ── RSI indicator ─────────────────────────────────────────────
    rsi_period: int = 14
    rsi_oversold: float = 45.0         # below → bullish
    rsi_overbought: float = 55.0       # above → bearish

    # ── Volume spike ─────────────────────────────────────────────
    volume_spike_multiplier: float = 2.0
    volume_spike_lookback: int = 20

    # ── PCR (Put-Call Ratio) ──────────────────────────────────────
    pcr_bullish_above: float = 1.1     # contrarian bullish
    pcr_bearish_below: float = 0.9     # contrarian bearish

    # ── Option contract selection ─────────────────────────────────
    option_min_delta: float = 0.30     # minimum absolute delta for contract selection
    option_max_delta: float = 0.55     # maximum absolute delta
    option_min_volume: int = 10        # skip illiquid contracts below this volume

    # ── Layer 5: Market regime gate (QQQ VWAP position) ──────────────
    # Blocks entries that fight the real-time market direction, as measured
    # by QQQ's position vs its session VWAP.  Unlike the old SPY 15-min EMA
    # approach, this reacts within 1 minute (no lag) and has no circular
    # reference issue when SPY itself is a scan candidate.
    #
    #   QQQ > VWAP + threshold  → BULLISH → block PUT  entries
    #   QQQ < VWAP − threshold  → BEARISH → block CALL entries
    #   |QQQ − VWAP| < threshold → NEUTRAL → allow all
    #
    # regime_vwap_threshold: how far QQQ must be from its VWAP before a
    # regime is declared.  0.002 (0.2%) filters noise while catching clear
    # intraday trends within 1–2 minutes.
    regime_gate_enabled: bool = True
    regime_gate_symbol: str = "QQQ"           # kept for log labels / adaptive band
    regime_gate_ttl_seconds: int = 300        # unused (VWAP gate is real-time)
    regime_vwap_threshold: float = 0.002      # 0.2% QQQ distance from VWAP to declare regime

    # ── Layer 6: IV filter ────────────────────────────────────────
    # Skips entries when ATM implied volatility exceeds this level.
    # High IV = overpriced premium, poor risk/reward.
    iv_max_threshold: float = 1.50       # skip if ATM IV > 150 %

    # ── Layer 4 tuning ────────────────────────────────────────────
    vwap_slope_lookback_bars: int = 20   # compare VWAP now vs N 1-min bars ago
    vwap_slope_threshold_pct: float = 0.05  # block if slope |Δ| exceeds this %

    # ── VWAP exit band (separate from entry band) ─────────────────
    # For VWAP_BREAK exits, use a tighter band than the entry filter.
    # The entry band (vwap_band_pct) is wide enough to catch pullbacks.
    # The exit band should be narrow — if underlying crosses this far past
    # the entry VWAP, the trade is on the wrong side and should exit promptly.
    # Near-expiry ATM options lose 25–30 % from just 0.5–1 % adverse underlying
    # moves (high gamma), so the exit band must be much tighter than 0.9 %.
    # Set to 0.0 to disable (falls back to vwap_band_pct).
    vwap_exit_band_pct: float = 0.003   # 0.3 % — fire VWAP_BREAK well before hard stop

    # ── Pre-entry guards ──────────────────────────────────────────
    cooldown_minutes: int = 60           # re-entry cooldown after STOP/VWAP_BREAK
    tp_cooldown_minutes: int = 30        # re-entry cooldown after TP1 or TP2 exit
    tp_chase_pct: float = 0.15          # block same-direction re-entry if new price > last TP entry × (1 + this)
    max_losses_per_symbol_per_day: int = 2  # max losing trades per symbol per day
    max_trades_per_symbol_per_day: int = 2  # max total entries per symbol per day (wins+losses)
    #                                       # forces diversification — prevents the same 3 symbols
    #                                       # from monopolising all MAX_OPEN_TRADES slots all day

    # ── Exit guards ───────────────────────────────────────────────
    # Minimum minutes a trade must be open before the HARD STOP can fire.
    # Gives the trade time to breathe past initial bid-ask noise and price
    # discovery — early stop-outs often happen on options that would have
    # recovered within 10-15 minutes.
    # The quick-loss exit (below) remains active during this window as
    # an emergency brake for truly catastrophic moves.
    # Set to 0 to disable (hard stop fires from tick 1 — original behaviour).
    stop_loss_min_hold_minutes: int = 15

    # Suppress TREND_REVERSAL for this many minutes after entry.
    # Prevents a single choppy 15-min bar from closing a trade that
    # was entered only seconds ago (the EMA-9 flip fires the moment
    # the bar closes, which can be just 1-2 minutes after entry).
    # Hard stop is now separately gated by stop_loss_min_hold_minutes.
    # ── Quick-loss early exit ─────────────────────────────────────
    # If the option loses quick_loss_pct % within the first quick_loss_max_minutes
    # of the trade, exit immediately (QUICK_LOSS reason).  This catches
    # wrong-direction entries — where the trade goes against us from tick one —
    # before the full hard-stop loss compounds via gamma.
    # Example: option entry $4.50, quick_loss_pct=0.12 → exit at $3.96
    #          fires in the first 5 minutes — saves ~$54 vs the -27% hard stop
    # Set quick_loss_pct=0.0 to disable.
    quick_loss_pct: float = 0.12         # exit if option drops this much within the window
    quick_loss_max_minutes: int = 5      # window: UPPER bound — only active in first N min
    # Quiet period before quick-loss arms: the trade must be open at least this
    # many minutes before QUICK_LOSS can fire.  Gives the option a moment to
    # breathe before gamma amplification kicks in.
    # quick_loss_min_hold_minutes=0  → arms immediately (original behaviour)
    # quick_loss_min_hold_minutes=3  → no quick-loss in first 3 minutes
    # NOTE: the broker hard-stop at Tradier is still active during this window.
    quick_loss_min_hold_minutes: int = 0   # lower bound — quiet period before QUICK_LOSS arms

    trend_reversal_min_hold_minutes: int = 10   # reduced from 20 — faster response

    # How many consecutive 15-min bars must close on the WRONG side of the EMA
    # before a TREND_REVERSAL exit is triggered.
    # n=1 → original single-bar behaviour (any one bar fires the exit)
    # n=2 → two bars required (prevents single-candle chop from shaking out trades)
    # n=0 → TREND_REVERSAL exit disabled entirely
    trend_reversal_confirm_bars: int = 1   # reduced from 2 — single bar reversal sufficient with EMA alignment gate at entry

    # Re-entry cooldown after a TREND_REVERSAL exit.
    # Prevents the churn pattern where EMA-9 flips repeatedly in choppy
    # markets and the bot keeps re-entering the same symbol every ~10 min.
    # Separate from cooldown_minutes (STOP/VWAP_BREAK) because a trend
    # reversal is less catastrophic and a shorter window is appropriate.
    # Set to 0 to disable.
    trend_reversal_cooldown_minutes: int = 30

    # ── Limit order entry ─────────────────────────────────────────
    # Enter at the mid-quote (bid+ask)/2 via a limit order instead of
    # hitting the ask with a market order.  Saves the half-spread on
    # entry — options often have wide spreads (e.g. bid 2.40 / ask 2.50
    # → limit at 2.45 saves $0.05/contract = $1 on a 20-contract trade).
    # If the order is not filled within limit_order_timeout_seconds the
    # order is cancelled and no trade is opened for this scan cycle.
    use_limit_orders: bool = True
    limit_order_timeout_seconds: int = 15

    # ── Trailing stop ─────────────────────────────────────────────
    # Once the trade is profitable enough, automatically raise the
    # stop price to lock in gains.  Thresholds are option-price
    # percentages above entry (e.g. 0.05 = 5 % gain).
    #
    # Stage 1 (breakeven): at BREAKEVEN_PCT gain → stop = entry price
    # Stage 2 (trail):     at TRAIL_PCT gain     → stop = current × (1 − TRAIL_FROM_CURRENT_PCT)
    #                        i.e. always trails N% below wherever the option is trading right now.
    #                        As the price rises, the floor rises with it.
    #
    # The stop only ever moves UP — it will never be lowered by these rules.
    # Set both to 0 to disable trailing stop entirely.
    trailing_stop_breakeven_pct:         float = 0.06   # trigger Stage 1 at 6% gain
    trailing_stop_lock_profit_pct:       float = 0.01   # Stage 1 stop = entry × (1 + this)
    #                                                    # e.g. entry $2.00 → stop $2.02 (+1%)
    #                                                    # locks in 1% to cover commission
    trailing_stop_trail_pct:             float = 0.10   # start trailing at 10% gain
    trailing_stop_trail_from_current_pct: float = 0.10  # trail stop = current × (1 − this)
    # Minimum minutes to hold before the trailing stop can activate.
    # Prevents the stop from firing on the very first management tick when the
    # option price is already above the threshold at the moment of entry
    # (common with limit orders at mid-price — the option may have already moved
    # 5–6 % above the entry mid before the first 30-second tick runs).
    # Hard stop and TP are never affected — only the trailing stop adjustment.
    trailing_stop_min_hold_minutes: int = 15

    # ── Lunch-hour noise filter ───────────────────────────────────
    # Block new entries during the low-liquidity midday chop window.
    # Both times are HH:MM strings in America/New_York timezone.
    lunch_break_enabled: bool = True
    lunch_break_start: str = "11:30"     # stop entries at this ET time
    lunch_break_end: str = "12:15"       # resume entries at this ET time

    # ══════════════════════════════════════════════════════════════
    # Strategy 2 — EMA Crossover
    # ══════════════════════════════════════════════════════════════
    # Entry logic:
    #   5-min trend filter : Price > EMA(200) AND EMA(9) > EMA(21)  → bullish
    #                        Price < EMA(200) AND EMA(9) < EMA(21)  → bearish
    #   1-min trigger       : EMA(9) crosses above/below EMA(21)
    #                       : Volume of trigger bar > previous bar
    # Exit logic:
    #   Hard stop           : -10% from entry
    #   Breakeven           : +10% → stop moves to entry price
    #   Trailing            : +20% → trail 5% below current
    #   Signal exit         : opposite EMA crossover on 1-min
    # ──────────────────────────────────────────────────────────────
    s2_enabled: bool = False             # master on/off for the EMA cross scanner

    # Indicator periods
    s2_ema_fast: int = 9                 # fast EMA (1-min cross trigger)
    s2_ema_slow: int = 21               # slow EMA (1-min cross trigger)
    s2_ema_trend: int = 200              # trend filter EMA on 5-min chart

    # Entry guards
    s2_volume_confirm: bool = True       # require trigger bar volume > previous bar
    s2_cooldown_minutes: int = 30        # re-entry cooldown after stop/signal exit

    # Risk & sizing (mirrors S1 defaults)
    s2_amount_per_trade: float = 500.0   # max premium per trade
    s2_risk_per_trade: float = 120.0     # USD at risk per trade at stop
    s2_max_open_trades: int = 2          # max concurrent S2 positions

    # Exit levels
    s2_stop_loss_pct: float = 0.10             # hard stop at -10%
    s2_stop_loss_min_hold_minutes: int = 0     # S2 exits fast — no hold delay needed
    s2_take_profit_pct: float = 0.0            # auto-TP at entry: 0 = disabled (exit on EMA cross only); 0.14 = +14%
    s2_breakeven_pct: float = 0.10             # move stop to entry at +10%
    s2_trail_pct: float = 0.20                 # start trailing at +20%
    s2_trail_from_current_pct: float = 0.05    # trail = current × (1 - 0.05)

    # Trading window (ET)
    s2_trading_start_time: str = "09:35"
    s2_last_entry_time: str = "14:15"
    s2_trading_end_time: str = "15:30"

    # ── Convenience aliases for parsed HH:MM fields ──────────────
    @property
    def cutoff_hour(self) -> int:
        return int(self.trading_end_time.split(":")[0])

    @property
    def cutoff_minute(self) -> int:
        return int(self.trading_end_time.split(":")[1])

    @property
    def start_hour(self) -> int:
        return int(self.trading_start_time.split(":")[0])

    @property
    def start_minute(self) -> int:
        return int(self.trading_start_time.split(":")[1])

    @property
    def last_entry_hour(self) -> int:
        return int(self.last_entry_time.split(":")[0])

    @property
    def last_entry_minute(self) -> int:
        return int(self.last_entry_time.split(":")[1])


settings = Settings()
