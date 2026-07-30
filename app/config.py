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
    scan_interval_seconds: int = 60     # how often S1 scans for new entries
    s2_scan_interval_seconds: int = 30  # how often S2 scans (shorter to catch 2-bar pullback window)
    manage_interval_seconds: int = 10   # how often to manage open trades

    # ── S1 master switch ─────────────────────────────────────────
    # Mirror of s2_enabled / s3_enabled / put_scalp_enabled (Jul 30 2026 —
    # the UI had toggles for S2/S3 but S1 could only be stopped by disabling
    # every symbol).  OFF blocks NEW S1 entries only; open S1 positions keep
    # being managed to their exit.  ANDed with each account's own s1 flag.
    s1_enabled: bool = True            # master on/off for the VWAP pullback scanner

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

    # Disaster buffer: the broker-side stop rests this fraction BELOW the
    # bot's working stop.  Tradier stops trigger on traded prints (noisy edge
    # of the spread) and fill at market; at the same price they would front-
    # run the bot's smarter mid-based stop and reintroduce noise stop-outs.
    # With the buffer, the bot handles every normal exit first — the broker
    # stop only fills if the bot process is dead or the move gapped through
    # a 30-second management tick.  0 = broker stop at the bot's exact level.
    broker_stop_buffer_pct: float = 0.08

    # ── Orphan auto-stop exclusions ──────────────────────────────
    # Comma-separated underlying tickers whose orphaned Tradier positions are
    # exempt from the auto-stop backstop in _manage_orphan_stops.
    # Use when you want to hold a position manually after removing it from Ajoy.
    # Example: ORPHAN_STOP_EXCLUDED_SYMBOLS=PLTR,HOOD
    orphan_stop_excluded_symbols: str = ""

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
    # ── L1 EMA slope filter ───────────────────────────────────────
    # Requires the 15-min EMA(ema_period) ITSELF to be rising for CALLs
    # (falling for PUTs), measured over the last ema_slope_lookback completed
    # 15-min bars.  Catches the stale-trend failure mode: price still above a
    # flattening or declining EMA21 — the pattern behind S1's losing CALLs
    # (−$510 live, Jun–Jul 2026).  Checks the EMA's direction, not price's
    # position, so genuine VWAP pullbacks are unaffected.
    ema_slope_filter_enabled: bool = True
    ema_slope_lookback: int = 2        # completed 15-min bars (2 = 30 minutes)

    # S1 PUT kill switch — mirror of s2_puts_enabled.  Kept ON by default;
    # exists so the Jul 28 review can disable the PUT side with one click if
    # the data condemns it (clean-engine PUTs −$261 vs CALLs positive).
    s1_puts_enabled: bool = True

    # ── PUT Scalp mode (PS) — Jul 23 2026 ────────────────────────────────
    # Temporary momentum-short experiment, fully independent of S1/S2 PUT
    # kill switches.  Entry: Stock Trend (completed-bar 15-min EMA bearish)
    # AND Thesis (underlying below session VWAP beyond the exit band) in
    # agreement, plus a red last-completed 1-min bar.  This is the breakdown
    # entry style the Jul 22-23 PUT post-mortem called for — no pullback
    # wait, so no adverse selection.  Tight fixed brackets, own risk size,
    # own spread gate (a 12% spread eats an 8% TP), strategy_name
    # "put_scalp" so analytics NEVER mix these with the CALL-only verdict.
    put_scalp_enabled: bool = False
    put_scalp_tp_pct: float = 0.11            # TP  = entry × 1.11
    put_scalp_sl_pct: float = 0.06            # SL  = entry × 0.94
    # 2 min, NOT 6 (Jul 24 AMZN #176): during the hold the only protection is
    # the broker disaster stop 8% below the bot stop — with a 6% scalp stop
    # that turns planned −6% risk into realized −13 to −17%.
    put_scalp_stop_min_hold_minutes: int = 2
    put_scalp_runner_arm_pct: float = 0.07    # runner arms at +7% gain (momentum bar req.)
    put_scalp_runner_trail_pct: float = 0.02  # trail 2% below mid once armed
    # Floor never below entry × 1.05 — the observed "+5-7% then reverse"
    # pattern must exit as a locked win, not a round-trip (Jul 23 tune).
    put_scalp_runner_floor_lock_pct: float = 0.05
    put_scalp_risk_per_trade: float = 75.0    # half size while the mode proves itself
    put_scalp_max_open: int = 1               # at most 1 concurrent PS trade
    put_scalp_max_spread_pct: float = 0.08    # tighter than S1/S2's 12%
    put_scalp_cooldown_minutes: int = 30      # per-symbol pause after ANY PS exit
    # Bounce guards (Jul 24 — AMZN #176 + INTC #181): PS state signals can be
    # true but stale.  Require a FRESH breakdown: no green last completed
    # 5-min bar, and price within this fraction of the session low.
    put_scalp_no_green_5m_enabled: bool = True
    put_scalp_max_bounce_from_low_pct: float = 0.005   # 0.5%; 0 = disabled

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

    # ── Structural (chart-based) stop / target levels ─────────────
    # Anchors the stop to the setup's invalidation point (pullback low /
    # VWAP / EMA9) and the target to the session swing high/low, translated
    # to option prices via delta.  Replaces flat percentage-of-premium levels
    # which stop out on underlying noise and cap winners arbitrarily.
    # Also enforces a minimum reward/risk at entry — setups without room to
    # the target are SKIPPED instead of entered.
    # Position size derives from the actual stop distance (risk_per_trade
    # dollars at the structural stop).
    structural_levels_enabled: bool = True
    struct_stop_buffer_pct: float = 0.001    # stop sits 0.1% beyond the invalidation level
    struct_pullback_lookback: int = 10       # S1: completed 1-min bars scanned for pullback extreme
    struct_min_stop_pct: float = 0.08        # option stop never tighter than 8% of premium (spread noise floor)
    struct_max_stop_pct: float = 0.30        # option stop never wider than 30% of premium (risk ceiling)
    struct_min_reward_risk: float = 1.2      # skip entry when underlying R/R below this
    # When structural levels are on, the S1 VWAP_BREAK band exit is disabled:
    # the structural stop below VWAP/pullback-low replaces it at a level
    # derived from structure instead of a fixed noise-width band.
    struct_disable_vwap_break: bool = True

    # ── Runner mode (S1 + S2) ─────────────────────────────────────
    # A breakout through the session high is exactly when an option can pay
    # +40% instead of the structural TP.  When the bid gets within
    # runner_proximity_pct of the TP AND the last completed 1-min candle
    # still shows momentum in the trade direction, the TP is waived and the
    # trade switches to a dedicated trail (runner_trail_pct below mid,
    # ratcheting up, never down).  Broker-side resting TP is cancelled at
    # activation.  Exit fires with reason RUNNER.
    # Without momentum at the target, the TP fires normally.
    runner_mode_enabled: bool = False
    runner_proximity_pct: float = 0.05   # arm check when bid ≥ TP × (1 − this)
    runner_trail_pct: float = 0.08       # trail = mid × (1 − this) while in runner mode
    # Activation floor: stop never below entry × (1 + this).  1% barely
    # covered exit slippage (XYZ #168: floor held, fill slipped → −$3
    # scratch); 3% makes the worst runner outcome green after the spread —
    # same reasoning as S2's breakeven lock.
    runner_floor_lock_pct: float = 0.03

    # ── Chop-day regime filter (session range vs daily ATR) ───────
    # Both strategies need a trending day; this measures whether today is one.
    #   ratio = (QQQ session high − low) / QQQ daily ATR(chop_atr_period)
    # New entries are blocked while ratio < chop_min_range_ratio.
    # Only evaluated after chop_filter_start_time (ET) — the session range is
    # naturally small right after the open.
    chop_filter_enabled: bool = True
    chop_atr_period: int = 14
    chop_min_range_ratio: float = 0.5        # QQQ must have covered ≥50% of its normal daily range
    chop_filter_start_time: str = "10:30"    # ET; before this the filter passes

    # ── Per-symbol energy gate ("in play" filter) ─────────────────
    # A pullback can only CONTINUE if the stock still has fuel.  Blocks
    # entries when the symbol's own session TRUE range (incl. overnight gap)
    # is below energy_min_range_ratio of its own daily ATR(14) — the
    # decaying-flat-base signature behind SHOP/SMCI/F (Jul 14: four losers,
    # all with bleeding intraday ATR on range-less symbols).
    # Separate toggles per strategy so each can be validated independently.
    # NOTE: catches flat/range-less symbols; a big-range stock whose morning
    # move is exhausted (ORCL-type) needs an ATR-slope veto — phase 2.
    energy_gate_s1_enabled: bool = False
    energy_gate_s2_enabled: bool = False
    energy_min_range_ratio: float = 0.5   # symbol true range must be ≥ this × its ATR(14)

    # Volatility CEILING — the floor's twin.  Post-event names running far
    # beyond their normal range whip option premium ±25% per candle (HOOD:
    # −$98 disaster-stop whip Jul 16, −$55 stop Jul 17, both at 3-4× ATR).
    # Block entries when today's true range EXCEEDS this × the symbol's own
    # ATR(14).  0 = disabled.
    vol_ceiling_s1_enabled: bool = False
    vol_ceiling_s2_enabled: bool = False
    energy_max_range_ratio: float = 2.5

    # ── Option contract quality floors ────────────────────────────
    # Minimum premium: sub-$1 contracts quantize in 1-cent ticks, so a
    # structural stop can be 2 ticks wide (F #146: $0.30 entry, $0.28 stop —
    # every quote wobble = ±3% of premium) and spreads run 10-20% of mid.
    # Applies to BOTH strategies at contract selection.  0 = disabled.
    option_min_premium: float = 1.00

    # ── S2 structure exit (replaces 5-min EMA cross signal exit) ──
    # Exit when the last N completed 1-min bars close back through the 5-min
    # EMA9 (the entry thesis level) by at least the margin.  Reacts in 2–3 min
    # vs 10–15 min for the old EMA9/21 cross, which never fired before the
    # hard stop in live trading.
    s2_structure_exit_enabled: bool = True
    s2_structure_exit_bars: int = 2          # consecutive 1-min closes required
    # Margin widened 0.05% → 0.15% and min-hold added (Jul 17): S2 enters
    # BECAUSE price touched the 5m EMA9, so right after entry price sits AT
    # the exit level — two red minutes of noise was scratching valid trades
    # (NVDA #161, PLTR #155: exited −9%, then both recovered to highs).
    # Every other exit has a min-hold; this one now does too.
    s2_structure_exit_margin_pct: float = 0.0015  # 0.15% beyond EMA9 to count as broken
    s2_structure_exit_min_hold_minutes: int = 10  # let the entry breathe first

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

    # ── Limit order exits ─────────────────────────────────────────
    # Entries save the half-spread with a limit at the mid; exits used to give
    # it straight back by market-selling at the bid.  With this enabled,
    # PATIENT exits (TP2, RUNNER, TRAILING_STOP, STRUCT_EXIT, EMA_CROSS,
    # TREND_REVERSAL, CUTOFF, MANUAL) first try a limit at the mid for
    # exit_limit_timeout_seconds, then cancel and fall back to a market sell.
    # Partial limit fills are booked at their fill price and the remainder is
    # market-sold, so P&L stays exact.
    # URGENT exits (STOP, QUICK_LOSS, VWAP_BREAK) always go straight to
    # market — chasing a falling market with a limit costs more than the
    # half-spread saves.
    exit_limit_orders_enabled: bool = True
    exit_limit_timeout_seconds: int = 12

    # Marketable-limit URGENT exits (Jul 23): raw market sells on fast moves
    # paid full spread-at-velocity (CRM/COIN: ~$36 extra each).  Urgent exits
    # now place a limit at bid × (1 − urgent_exit_limit_pct) — fills like a
    # market order in normal tape but caps the worst fill at 3% below bid.
    # Unfilled after the (short) timeout → cancel → true market.
    urgent_exit_limit_enabled: bool = True
    urgent_exit_limit_pct: float = 0.03
    urgent_exit_limit_timeout_seconds: int = 6

    # ── Signal-conflict exit (Jul 23, user-requested "last try" rule) ─────
    # Every manage tick, recompute Stock Trend and Thesis (same logic as the
    # dashboard columns).  When BOTH conflict with the trade's direction —
    # S1: completed-bar 15-min trend flipped AND underlying beyond the exit
    # band on the wrong side of session VWAP;  S2: 5-min EMA9/21 crossed
    # opposite — exit immediately via the marketable-limit urgent path,
    # reason SIGNAL_FADE.  First conflict timestamp is stored on the trade
    # (signal_conflict_time) for later analysis.
    # Requiring BOTH signals (and bar-close trend, not live-price) is the
    # noise guard that separates this from the removed VWAP_BREAK band exit.
    signal_conflict_exit_enabled: bool = True

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
    # Volume filter threshold: confirmation bar volume must be ≥ this fraction
    # of the 20-bar average.  1.0 (original) rejected ~half of all bars near-
    # randomly at 1-min resolution — on Jul 15 it vetoed 34 of 51 fully-
    # confirmed S2 signals.  0.8 keeps the thin-tape protection while letting
    # ordinary bars through.
    s2_volume_min_ratio: float = 1.0
    s2_cooldown_minutes: int = 30        # re-entry cooldown after stop/signal exit
    s2_cross_max_bars_old: int = 8       # block entry if EMA9/21 cross is older than N 5-min bars (0 = disabled)

    # ── S2 PUT-side guards ────────────────────────────────────────
    # Live results (Jun 8 – Jul 7 2026): S2 PUTs went 4W/14L for −$554 while
    # S2 CALLs were net positive.  Nearly every losing PUT fired on a 5-min
    # dip inside a larger 15-min uptrend — the old alignment gate let these
    # through because a NEUTRAL 15-min trend allowed both directions.
    #
    # s2_puts_enabled=False   → disable S2 PUT entries entirely (kill switch)
    # s2_put_15m_strict=True  → a PUT entry requires the 15-min trend to be
    #                           confirmed BEARISH.  Neutral or missing 15-min
    #                           data blocks the PUT (CALL behaviour unchanged:
    #                           only a confirmed opposing trend blocks it).
    s2_puts_enabled: bool = True
    s2_put_15m_strict: bool = True

    # Risk & sizing (mirrors S1 defaults)
    s2_amount_per_trade: float = 500.0   # max premium per trade
    s2_risk_per_trade: float = 120.0     # USD at risk per trade at stop
    s2_max_open_trades: int = 2          # max concurrent S2 positions

    # Exit levels
    # Entry filters
    s2_max_spread_pct: float = 0.10           # max bid/ask spread as % of mid (10% default)
    s2_max_trades_per_day: int = 2            # max S2 entries per symbol per day; 0 = no cap
    s2_max_daily_loss: float = 300.0          # halt ALL S2 entries once S2-only realized loss hits this; 0 = disabled

    # TP price-chase guard (mirrors S1's tp_chase_pct, S2-specific).
    # After a profitable S2 exit today in the same direction, block re-entry
    # if the new option mid is more than this % above the previous entry price.
    # 0.12 = 12%: absorbs normal spread movement but catches genuinely extended moves.
    # Set to 0 to disable.
    s2_tp_chase_pct: float = 0.12

    s2_stop_loss_pct: float = 0.10             # hard stop at -10%
    s2_stop_loss_min_hold_minutes: int = 0     # S2 exits fast — no hold delay needed
    s2_take_profit_pct: float = 0.0            # auto-TP at entry: 0 = disabled (exit on EMA cross only); 0.14 = +14%
    s2_breakeven_pct: float = 0.10             # move stop to entry at +10%
    s2_trail_pct: float = 0.20                 # start trailing at +20%
    # Trail distance below current mid.  Never set this below ~0.05: exits
    # fill at the BID (5–10% below mid on cheap options), so a 1% trail
    # converts winners into net losses on the first tick of noise.
    s2_trail_from_current_pct: float = 0.08    # trail = current × (1 - 0.08)

    # ── S2 lock-profit at breakeven ───────────────────────────────
    # When the breakeven stop fires, raise the stop to entry × (1 + this)
    # instead of plain entry price.  Covers the bid-ask spread cost so the
    # trade is still green after commission even if the stop fires at the bid.
    # e.g. entry $3.00, lock_profit_pct=0.03 → stop raised to $3.09 (+3%)
    # Set to 0.0 to restore original behaviour (stop = entry price exactly).
    s2_trailing_stop_lock_profit_pct: float = 0.03   # 3% above entry at Stage 1

    # ── S2 quick-loss early exit ──────────────────────────────────
    # If the option drops s2_quick_loss_pct within the first
    # s2_quick_loss_max_minutes of the trade, exit immediately.
    # Catches wrong-direction entries before full hard-stop loss compounds.
    # Example: entry $3.00, pct=0.12 → exit fires at $2.64 in first 5 min,
    #          saving ~$108 vs waiting for the -21% hard stop at $2.37.
    # Set s2_quick_loss_pct=0.0 to disable.
    s2_quick_loss_pct: float = 0.12            # exit if option drops ≥12% in the window
    s2_quick_loss_min_hold_minutes: int = 0    # quiet period before quick-loss arms (0 = immediate)
    s2_quick_loss_max_minutes: int = 5         # window: only active in first N minutes

    # Trading window (ET)
    s2_trading_start_time: str = "09:35"
    s2_last_entry_time: str = "14:15"
    s2_trading_end_time: str = "15:30"

    # ══════════════════════════════════════════════════════════════
    # Strategy 3 — Ask-Wall Breakout Scalper (STOCKS via Moomoo OpenD)
    # ══════════════════════════════════════════════════════════════
    # Long-only US-stock breakout scalping on tick + 10-level order book.
    # Detects persistent ask walls that are genuinely CONSUMED by aggressive
    # buying (not merely cancelled), and enters on price confirmation above
    # the former wall.  Trades the S1 watchlist symbols.
    s3_enabled: bool = False              # master on/off for the S3 engine

    # ── Execution broker ──────────────────────────────────────────
    # "tradier" (default): Moomoo OpenD supplies MARKET DATA ONLY; every
    #     order routes to Tradier using the app's existing credentials.
    #     Sandbox vs live follows USE_SANDBOX, same as S1/S2.
    # "moomoo": orders go to the Moomoo account through OpenD instead
    #     (requires s3_trd_env / s3_trade_pwd below).
    s3_broker: str = "tradier"
    # S3-only sandbox override, INDEPENDENT of the global USE_SANDBOX that
    # S1/S2 follow (so S3 can paper-trade while S1/S2 stay live, or the
    # reverse):
    #   "inherit" → follow USE_SANDBOX (default)
    #   "1"       → S3 orders to Tradier SANDBOX
    #   "0"       → S3 orders to Tradier LIVE
    s3_use_sandbox: str = "inherit"

    # ── Moomoo / OpenD connection (market data) ───────────────────
    s3_opend_host: str = "127.0.0.1"
    s3_opend_port: int = 11111
    # Only used when s3_broker="moomoo":
    # SIMULATE = moomoo paper account, REAL = live money.
    # REAL additionally requires s3_trade_pwd (trade unlock password);
    # without it the engine runs signal-only and places no orders.
    s3_trd_env: str = "REAL"
    s3_trade_pwd: str = ""                # trade unlock password (secret — never exposed via /api/config)
    s3_record_dir: str = "s3_data"        # JSONL event-recording directory (for ReplayEngine)
    s3_record_events: bool = True         # record all normalized events + decisions

    # ── Session window (ET) ───────────────────────────────────────
    s3_trading_start_time: str = "09:40"  # no entries before (skip opening rotation)
    s3_last_entry_time: str = "10:50"     # no NEW entries after
    s3_flatten_time: str = "11:00"        # force-flatten every S3 position at this time

    # ── Order book baseline / wall detection ─────────────────────
    # Baseline = robust location/scale of level sizes per (symbol, side,
    # level-band, time-of-day bucket), using median + MAD (fallback:
    # percentiles when MAD degenerates to 0 on discrete size ladders).
    s3_baseline_window_min: int = 20        # rolling window (minutes) for the depth baseline
    s3_baseline_tod_bucket_min: int = 10    # time-of-day bucket size (minutes) for baseline segregation
    s3_baseline_min_samples: int = 60       # snapshots required before walls can be flagged
    s3_wall_abs_min_shares: int = 5000      # absolute floor: level must show at least this many shares
    s3_wall_rel_mult: float = 5.0           # AND at least this × the robust baseline (initially 5×)
    s3_wall_max_level: int = 3              # only walls within the top N ask levels are actionable
    s3_wall_min_persist_sec: float = 3.0    # wall must persist at least this long …
    s3_wall_min_updates: int = 5            # … and across at least this many book updates

    # ── Consumption vs withdrawal ─────────────────────────────────
    # consumption_ratio = matched aggressive-buy volume / initial wall size
    # pull_ratio        = unmatched reduction        / initial wall size
    # Reductions are matched against aggressive-buy prints at the wall price
    # within s3_match_window_ms of the book update.  Cancelled liquidity is
    # treated neutrally as "liquidity withdrawal" — never labelled intent.
    s3_match_window_ms: int = 750           # tape↔book correlation window
    s3_min_consumption_ratio: float = 0.60  # ≥60% of the wall must be genuinely traded through
    s3_max_pull_ratio: float = 0.35         # ≤35% may vanish unmatched, else treat as withdrawal → no trade
    s3_confirm_ticks: int = 1               # last trade must print ≥ wall + N ticks to confirm breakout
    s3_require_ask_advance: bool = True     # best ask must move beyond the former wall price
    # Iceberg-reload veto: a wall that REFRESHES upward by ≥ this fraction of
    # its initial size after meaningful consumption is a seller with more
    # behind it — the wall is discarded (no trade) and the price level is
    # blocked from re-detection for the cooldown.  0 = disabled.
    s3_reload_veto_frac: float = 0.25
    s3_reload_cooldown_sec: float = 120.0

    # ── Tape / order-flow gates ───────────────────────────────────
    s3_flow_short_window_sec: float = 5.0   # short window for buy-rate acceleration
    s3_flow_long_window_sec: float = 30.0   # long reference window
    s3_flow_accel_mult: float = 1.5         # short-window buy rate must exceed this × long-window rate
    s3_flow_min_imbalance: float = 0.25     # signed-volume imbalance (buys−sells)/(buys+sells) over short window
    s3_max_spread_ticks: int = 3            # reject entries when spread is wider than N ticks
    s3_max_spread_pct: float = 0.002        # … or wider than this fraction of price
    s3_stale_data_max_ms: int = 1500        # reject decisions on data older than this
    s3_halt_quiet_sec: float = 20.0         # no prints AND static book for this long → suspect halt, block entries

    # ── Position sizing / risk ────────────────────────────────────
    # shares = floor(s3_max_risk_dollars / estimated_per_share_risk)
    # then capped by buying power, notional, participation, per-symbol
    # and portfolio limits.
    s3_max_risk_dollars: float = 100.0      # $ at risk at the structural stop (full position)
    s3_max_notional: float = 15000.0        # max $ notional per position
    s3_max_bp_fraction: float = 0.50        # use at most this fraction of available buying power
    s3_max_participation: float = 0.05      # shares ≤ this × trailing 1-min volume
    s3_max_open_positions: int = 2          # max concurrent S3 positions
    s3_max_portfolio_notional: float = 25000.0  # total S3 notional cap across positions
    s3_max_trades_per_symbol: int = 2       # max S3 entries per symbol per day
    s3_max_daily_loss: float = 300.0        # halt S3 for the day once realized S3 P&L ≤ −this
    # Economic-viability floor: skip entries whose capped size comes out
    # below this share count — on high-priced symbols with small capital,
    # 2-3 share positions cannot beat spread + slippage.  0 = disabled.
    s3_min_shares: int = 20
    s3_max_consecutive_losses: int = 3      # halt S3 after N consecutive losing trades
    s3_cooldown_minutes: int = 20           # re-entry cooldown per symbol after any exit

    # ── Entry / scale-in ──────────────────────────────────────────
    s3_entry_slippage_ticks: int = 2        # marketable limit = ask + N ticks (strict cap; no chasing)
    s3_entry_timeout_sec: float = 3.0       # cancel unfilled entry after this
    s3_initial_tranche_pct: float = 0.50    # ~50% of risk-approved size on first confirmed breakout
    s3_scale_window_sec: float = 3.0        # add-on only if micro-high breaks within this window
    s3_scale_requires_flow: bool = True     # add-on also requires flow gates still passing

    # ── Stops / targets (R-based) ─────────────────────────────────
    # Stop = structural invalidation below the reclaimed wall / recent
    # micro-swing low, widened by spread + short-horizon volatility, snapped
    # to tick.  R = VWAP entry − initial stop.
    s3_stop_vol_mult: float = 1.5           # stop pad = this × short-horizon volatility (per-second sigma·√horizon)
    s3_stop_spread_pad_ticks: int = 1       # extra pad below invalidation, in ticks + current spread
    s3_min_stop_ticks: int = 4              # reject impractically tight stops (< N ticks)
    s3_max_stop_pct: float = 0.01           # reject stops wider than 1% of entry price
    s3_tp1_r_mult: float = 1.0              # first scale-out at +1R (≈⅓)
    s3_tp2_r_mult: float = 2.0              # second scale-out at +2R (≈⅓)
    s3_breakeven_cost_ticks: int = 1        # after TP1 fill confirms, stop → entry + N ticks (est. costs)
    s3_ema_exit_period: int = 9             # runner exits on 1-min close below this EMA
    # Scalps must work fast: before TP1 fills, exit if no NEW post-entry high
    # for this many seconds (0 = disabled).
    s3_stagnation_exit_sec: float = 75.0
    # Thesis invalidation: before TP1, a print a full tick BELOW the former
    # wall means the reclaim failed — exit immediately, don't wait for the
    # (lower) hard stop.
    s3_reclaim_fail_exit: bool = True
    s3_exit_slippage_ticks: int = 3         # marketable-limit cap for urgent exits
    s3_exit_timeout_sec: float = 2.0        # unfilled urgent exit → cancel/replace deeper

    # ── Engine hygiene ────────────────────────────────────────────
    s3_queue_max: int = 20000               # bounded event queue size (drop-oldest + counter)
    s3_reconnect_backoff_sec: float = 2.0   # initial reconnect backoff (doubles, capped 60s)
    s3_kill_switch: bool = False            # manual kill switch — flatten & halt when flipped on

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
