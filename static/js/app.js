/* a-joy Alpine.js application */

function ajoy() {
  return {
    // ── Navigation ──────────────────────────────────────────────
    activeTab: 'trades',
    tabs: [
      { id: 'symbols',    label: 'Symbols' },
      { id: 'indicators', label: 'Indicators' },
      { id: 'trades',     label: 'Trades' },
      { id: 'settings',   label: 'Settings' },
    ],
    // ── Trade sub-tabs ───────────────────────────────────────────
    tradeSubTab: 'open',

    // ── Clock ────────────────────────────────────────────────────
    clock: '',

    // ── Symbols (unified watchlist — S1 and/or S2 per row) ───────
    symbols: [],
    newTicker: '',
    newSymbolS1: true,
    newSymbolS2: true,
    newSymbolS3: true,

    // ── Indicators ───────────────────────────────────────────────
    indicators: [],
    groups: [],
    strategies: [],
    showAddIndicator: false,
    showAddGroup: false,
    showAddStrategy: false,
    newIndicator: { key: '', name: '', description: '', category: 'general', active: true },
    newGroup: { name: '', logic_type: 'AND', indicator_ids: [] },
    newStrategy: { name: '', description: '', enabled: true, indicator_group_id: '' },

    // ── Indicator sub-tabs ───────────────────────────────────────
    activeIndicatorTab: 'registry',
    // ── Indicator live eval ──────────────────────────────────────
    evalSymbol: '',
    evalDirection: 'CALL',
    evalLoading: false,
    evalResults: [],
    evalAllPass: false,      // backward-compat: L1 indicators only
    evalFullPass: false,     // true only when ALL 13 gates pass
    evalGateStack: [],       // [{id, name, pass, reason}, …] for G1–L6
    evalFirstBlocker: null,  // first gate that failed, or null
    evalMeta: { bars_1m: 0, bars_15m: 0 },
    evalError: '',
    evalGateOpen: true,      // collapse toggle for gate stack panel

    // ── Trades ───────────────────────────────────────────────────
    liveTrades: [],
    prevPnl: {},          // { trade_id → last live_pnl } for trend tracking
    orphanedPositions: [],  // positions in Tradier with no Ajoy DB record
    orphanLoading: false,
    closedToday: [],
    todaySummary: { trade_count: 0, total_pnl: 0, winners: 0, losers: 0 },
    history: [],

    // ── Per-trade level editor ────────────────────────────────────
    editingTradeId: null,   // trade.id currently being edited, or null
    editStopPrice:  '',     // dollar value
    editTpPrice:    '',     // dollar value
    editStopMode:   '$',    // '$' or '%'
    editTpMode:     '$',    // '$' or '%'
    editStopPct:    '',     // percentage value (when editStopMode === '%')
    editTpPct:      '',     // percentage value (when editTpMode === '%')
    editSaving:     false,
    editError:      '',

    // ── History filters & chart ───────────────────────────────────
    historyFilters: { date: 'All', symbol: 'All', dir: 'All', reason: 'All' },
    historyDates: [],
    historySymbols: [],
    historyReasons: [],
    pnlChart: null,

    // ── Settings ──────────────────────────────────────────────────
    config: {},
    configSaving: {},   // { groupId: 'idle' | 'saving' | 'saved' | 'error' }
    configGroups: [
      {
        id: 'risk',
        label: 'Risk & Sizing',
        fields: [
          { key: 'max_daily_loss',   label: 'Max Daily Loss',      hint: 'Halt day if P&L < -$this',       type: 'number', step: 1 },
          { key: 'risk_per_trade',   label: 'Risk Per Trade ($)',   hint: 'USD lost if stop fires — sizes qty = risk / (entry × stop%). 0 = disable', type: 'number', step: 10 },
          { key: 'amount_per_trade', label: 'Premium Budget Cap',   hint: 'Max USD premium per trade — skips trade if 1 contract exceeds it', type: 'number', step: 10 },
          { key: 'max_open_trades',  label: 'Max Open Trades',      hint: 'Concurrent positions cap',        type: 'number', step: 1 },
          { key: 'stop_loss_pct',    label: 'Stop Loss (decimal)',  hint: '0.27 = -27% from entry. FALLBACK only while Structural Levels are on (used when delta is unavailable)', type: 'number', step: 0.01 },
          { key: 'take_profit_pct',  label: 'Take Profit (decimal)',hint: '0.35 = +35% from entry. FALLBACK only while Structural Levels are on', type: 'number', step: 0.01 },
          { key: 'broker_stop_enabled', label: 'Broker Disaster Stop',  hint: 'Resting stop at Tradier, BUFFERED below the bot stop — only fills if the bot is down or the move gaps through a tick. Bot handles normal exits', type: 'bool' },
          { key: 'broker_stop_buffer_pct', label: 'Disaster Stop Buffer',  hint: 'Broker stop = bot stop × (1 − this). 0.08 = 8% below — unreachable in normal operation, prevents noise prints from front-running the smart bot stop', type: 'number', step: 0.01 },
          { key: 'broker_tp_enabled',   label: 'Broker-Side TP',    hint: 'Resting limit sell at TP price — auto-fills at target even if bot is down; updating Target in Open Positions also updates this order', type: 'bool' },
        ],
      },
      {
        id: 'entry',
        label: 'Entry Filters',
        fields: [
          { key: 'vwap_band_pct',            label: 'VWAP Band — Normal',      hint: 'Pullback tolerance when QQQ is flat (0.009 = 0.9%)',  type: 'number', step: 0.001 },
          { key: 'vwap_min_clearance_pct', label: 'VWAP Min Clearance',      hint: 'Stock must be at least this far from VWAP on the correct side — blocks AT RISK entries (0.002 = 0.2%). Set 0 to disable.', type: 'number', step: 0.001 },
          { key: 's1_puts_enabled',         label: 'S1 PUT Entries',          hint: 'Kill switch for S1 PUTs (mirror of S2\'s). Clean-engine PUTs −$261 vs CALLs positive — review lever, ON pending Jul 28 verdict', type: 'bool' },
          { key: 'ema_period',              label: 'EMA Period',              hint: 'Trend direction (default 21)',                                                    type: 'number', step: 1 },
          { key: 'ema_consecutive_bars',  label: 'EMA Confirm Bars',       hint: 'Consecutive bars on correct EMA side',                                           type: 'number', step: 1 },
          { key: 'ema_slope_filter_enabled', label: 'EMA Slope Filter',    hint: 'The 15-min EMA itself must be rising (CALL) / falling (PUT) — blocks stale trends where price sits above a flattening EMA. Pullbacks unaffected', type: 'bool' },
          { key: 'ema_slope_lookback',       label: 'EMA Slope Lookback',  hint: 'Completed 15-min bars the EMA must have risen/fallen over (2 = 30 min)', type: 'number', step: 1 },
          { key: 'ema_1m_min_margin_pct', label: '1-min EMA Min Margin',   hint: 'Min EMA9-EMA21 spread on 1-min to count as trending — below this = neutral, 15-min decides (0.001 = 0.1%)', type: 'number', step: 0.001 },
          { key: 'bounce_bars_required',  label: 'Bounce Bars',           hint: 'VWAP bounce confirmation bars (L2)',       type: 'number', step: 1 },
        ],
      },
      {
        id: 'adaptive_band',
        label: 'Adaptive VWAP Band (QQQ-Based)',
        fields: [
          { key: 'adaptive_band_enabled',           label: 'Adaptive Band',              hint: 'Widen entry band on strong gap-up days using QQQ as reference',       type: 'bool' },
          { key: 'adaptive_band_symbol',            label: 'Reference Symbol',            hint: 'Nasdaq proxy to measure market extension (default: QQQ)',              type: 'text' },
          { key: 'adaptive_band_relaxed_threshold', label: 'Relaxed Threshold',           hint: 'QQQ this far from VWAP → use relaxed band (0.005 = 0.5%)',            type: 'number', step: 0.001 },
          { key: 'vwap_band_relaxed_pct',           label: 'VWAP Band — Relaxed',         hint: 'Band when QQQ is moderately extended (0.013 = 1.3%)',                 type: 'number', step: 0.001 },
          { key: 'adaptive_band_wider_threshold',   label: 'Wider Threshold',             hint: 'QQQ this far from VWAP → use wider band (0.015 = 1.5%)',              type: 'number', step: 0.001 },
          { key: 'vwap_band_wider_pct',             label: 'VWAP Band — Wider',           hint: 'Band when QQQ is strongly extended, like a gap-up day (0.018 = 1.8%)', type: 'number', step: 0.001 },
        ],
      },
      {
        id: 'window',
        label: 'Trading Window',
        fields: [
          { key: 'trading_start_time',            label: 'Start Time (ET)',            hint: 'No entries before this',           type: 'time' },
          { key: 'last_entry_time',               label: 'Last Entry Time (ET)',        hint: 'No new entries after this',        type: 'time' },
          { key: 'trading_end_time',              label: 'End / Force-Close (ET)',      hint: 'All open trades closed here',      type: 'time' },
          { key: 'cooldown_minutes',              label: 'STOP Cooldown (min)',          hint: 'Re-entry wait after stop-out',     type: 'number', step: 1 },
          { key: 'tp_cooldown_minutes',           label: 'TP Cooldown (min)',            hint: 'Re-entry wait after TP hit',       type: 'number', step: 1 },
          { key: 'max_losses_per_symbol_per_day', label: 'Max Losses / Symbol / Day',   hint: 'Symbol halted after N losses',     type: 'number', step: 1 },
          { key: 'max_trades_per_symbol_per_day', label: 'Max Trades / Symbol / Day',   hint: 'Total entries per symbol per day', type: 'number', step: 1 },
          { key: 'lunch_break_enabled',           label: 'Lunch Break',                 hint: 'Block entries during lunch hours', type: 'bool' },
        ],
      },
      {
        id: 'trailing',
        label: 'Trailing Stop',
        fields: [
          { key: 'trailing_stop_breakeven_pct',          label: 'Breakeven Trigger',   hint: 'Lock breakeven at this gain (0.07 = 7%)',       type: 'number', step: 0.01 },
          { key: 'trailing_stop_lock_profit_pct',        label: 'Breakeven Lock',       hint: 'Stop = entry × (1 + this) after breakeven',    type: 'number', step: 0.01 },
          { key: 'trailing_stop_trail_pct',              label: 'Trail Start',           hint: 'Start trailing at this gain (0.10 = 10%)',      type: 'number', step: 0.01 },
          { key: 'trailing_stop_trail_from_current_pct', label: 'Trail Distance',        hint: 'Stop = current × (1 - this)',                  type: 'number', step: 0.01 },
          { key: 'trailing_stop_min_hold_minutes',       label: 'Min Hold (min)',         hint: "Don't activate trail until N min after entry", type: 'number', step: 1 },
        ],
      },
      {
        id: 'regime',
        label: 'Market Regime & IV',
        fields: [
          { key: 'regime_gate_enabled',   label: 'Regime Gate',        hint: 'Block trades opposing SPY trend',      type: 'bool' },
          { key: 'regime_gate_symbol',    label: 'Regime Symbol',       hint: 'Index to use as macro proxy (e.g. SPY)', type: 'text' },
          { key: 'iv_max_threshold',      label: 'Max IV Threshold',    hint: 'Skip contract if ATM IV > this (1.75 = 175%)', type: 'number', step: 0.05 },
        ],
      },
      {
        id: 'exit',
        label: 'Exit Guards',
        fields: [
          { key: 'quick_loss_pct',              label: 'Quick-Loss Threshold',         hint: 'Exit if option drops this much % within the armed window (0.25 = 25%)', type: 'number', step: 0.01 },
          { key: 'quick_loss_min_hold_minutes', label: 'Quick-Loss Min Hold (min)',   hint: 'Quiet period — quick-loss does NOT fire in first N minutes (0 = arms immediately)', type: 'number', step: 1 },
          { key: 'quick_loss_max_minutes',      label: 'Quick-Loss Max Window (min)', hint: 'Upper bound — quick-loss disarmed after this many minutes of entry',   type: 'number', step: 1 },
          { key: 'vwap_exit_band_pct',          label: 'VWAP Exit Band',             hint: 'VWAP_BREAK fires when underlying passes VWAP by this % (0.003 = 0.3%). INACTIVE while Structural Levels + Disable VWAP-Break are on', type: 'number', step: 0.001 },
          { key: 'trend_reversal_min_hold_minutes', label: 'Reversal Min Hold (min)', hint: 'Suppress TREND_REVERSAL for N min after entry (only when profitable)', type: 'number', step: 1 },
          { key: 'trend_reversal_confirm_bars',     label: 'Reversal Confirm Bars',   hint: '1 = single bar triggers exit, 2 = need 2 consecutive bars',             type: 'number', step: 1 },
          { key: 'trend_reversal_cooldown_minutes', label: 'Reversal Cooldown (min)', hint: 'Re-entry cooldown after a TREND_REVERSAL exit',                         type: 'number', step: 1 },
          { key: 'exit_limit_orders_enabled',   label: 'Limit-Order Exits',        hint: 'Patient exits (TP, trails, signal exits, cutoff) sell via limit at the mid — saves the half-spread. Urgent exits (STOP, QUICK_LOSS, VWAP_BREAK) always market-sell', type: 'bool' },
          { key: 'exit_limit_timeout_seconds',  label: 'Exit Limit Timeout (s)',   hint: 'Cancel the exit limit and fall back to a market sell after this many seconds (partial fills booked exactly)', type: 'number', step: 1 },
        ],
      },
      // ── Structural levels + chop filter (shared S1 + S2) ─────────
      {
        id: 'structural',
        label: 'Structural Levels (S1 + S2)',
        fields: [
          { key: 'structural_levels_enabled', label: 'Structural Levels',      hint: 'Stop = chart invalidation point (pullback low / VWAP / EMA9), target = session swing high/low, both delta-translated to option prices. Off = legacy % of premium', type: 'bool' },
          { key: 'struct_min_reward_risk',    label: 'Min Reward/Risk',         hint: 'Skip entry if underlying target-distance ÷ stop-distance is below this (1.2 recommended). Lower to 1.0 if too few trades', type: 'number', step: 0.1 },
          { key: 'struct_stop_buffer_pct',    label: 'Stop Buffer',             hint: 'Stop sits this far beyond the invalidation level (0.001 = 0.1%)', type: 'number', step: 0.001 },
          { key: 'struct_pullback_lookback',  label: 'Pullback Lookback (bars)', hint: 'S1: completed 1-min bars scanned for the pullback low/high (S2 uses its 2-bar pattern)', type: 'number', step: 1 },
          { key: 'struct_min_stop_pct',       label: 'Min Stop (% premium)',    hint: 'Option stop never tighter than this fraction of premium — floor against spread noise (0.08 = 8%)', type: 'number', step: 0.01 },
          { key: 'struct_max_stop_pct',       label: 'Max Stop (% premium)',    hint: 'Option stop never wider than this fraction of premium — risk ceiling (0.30 = 30%)', type: 'number', step: 0.01 },
          { key: 'struct_disable_vwap_break', label: 'Disable VWAP-Break Exit', hint: 'Skip the legacy 0.3% VWAP band exit while structural levels are on (band sits inside normal noise)', type: 'bool' },
        ],
      },
      {
        id: 'runner',
        label: 'Runner Mode (S1 + S2)',
        fields: [
          { key: 'runner_mode_enabled',  label: 'Runner Mode',        hint: 'When price reaches the TP WITH momentum (last 1-min candle still pushing), waive the TP and trail instead — lets breakout winners run past the target. Exits label as RUNNER', type: 'bool' },
          { key: 'runner_proximity_pct', label: 'Activation Zone',     hint: 'Start checking momentum when bid is within this fraction of the TP (0.05 = within 5%)', type: 'number', step: 0.01 },
          { key: 'runner_trail_pct',     label: 'Runner Trail',        hint: 'While in runner mode, stop ratchets this far below the price and never drops (0.08 = 8%)', type: 'number', step: 0.01 },
          { key: 'runner_floor_lock_pct', label: 'Runner Floor Lock',  hint: 'Activation stop never below entry × (1 + this). 0.03 = +3% — keeps the worst runner exit green after spread/slippage', type: 'number', step: 0.01 },
        ],
      },
      {
        id: 'energy',
        label: 'Energy Gate (S1 + S2)',
        fields: [
          { key: 'energy_gate_s1_enabled', label: 'Energy Gate — S1',   hint: 'Block S1 entries when the SYMBOL\'s own true range (incl. gap) is below the ratio × its own ATR(14) — flat, fuel-less stocks can\'t continue a pullback', type: 'bool' },
          { key: 'energy_gate_s2_enabled', label: 'Energy Gate — S2',   hint: 'Same check for S2 entries (Jul 14: all four S2 losers were range-less symbols with bleeding intraday ATR)', type: 'bool' },
          { key: 'energy_min_range_ratio', label: 'Min Range / ATR',     hint: 'Symbol true range must reach this fraction of its own ATR(14) to be "in play" (0.5 = 50%)', type: 'number', step: 0.05 },
          { key: 'vol_ceiling_s1_enabled', label: 'Vol Ceiling — S1',    hint: 'Block S1 entries when the symbol is TOO hot — true range beyond the max ratio × its ATR(14). Post-event names whip premium ±25%/candle (HOOD −$153, Jul 16-17)', type: 'bool' },
          { key: 'vol_ceiling_s2_enabled', label: 'Vol Ceiling — S2',    hint: 'Same too-hot check for S2 entries', type: 'bool' },
          { key: 'energy_max_range_ratio', label: 'Max Range / ATR',     hint: 'Ceiling: block when true range exceeds this × own ATR(14) (2.5 = 250%). 0 = disabled', type: 'number', step: 0.25 },
          { key: 'option_min_premium',     label: 'Min Option Premium',  hint: 'Skip contracts cheaper than this (both strategies) — sub-$1 options tick in whole cents and carry 10-20% spreads. 0 = disabled', type: 'number', step: 0.25 },
        ],
      },
      {
        id: 'chop',
        label: 'Chop-Day Filter (S1 + S2)',
        fields: [
          { key: 'chop_filter_enabled',    label: 'Chop Filter',            hint: 'Block ALL new entries while QQQ TRUE range (incl. overnight gap) < ratio × daily ATR — pullback setups need a trending day', type: 'bool' },
          { key: 'chop_min_range_ratio',   label: 'Min Range / ATR Ratio',   hint: 'QQQ true range (gap included) must reach this fraction of ATR(14) (0.5 = 50%). Lower to 0.4 if too few trades', type: 'number', step: 0.05 },
          { key: 'chop_atr_period',        label: 'ATR Period (days)',       hint: 'Daily bars used for the ATR baseline (default 14)', type: 'number', step: 1 },
          { key: 'chop_filter_start_time', label: 'Filter Start (ET)',       hint: 'Filter passes before this time — session range is naturally small right after the open', type: 'time' },
        ],
      },
      // ── Strategy 2 settings (EMA Pullback) ───────────────────────
      {
        id: 's2_core',
        label: 'S2 — EMA Pullback: Core',
        fields: [
          { key: 's2_enabled',            label: 'S2 Enabled',              hint: 'Master switch — enable the EMA Pullback strategy scanner',                                type: 'bool' },
          { key: 's2_max_open_trades',    label: 'Max Open Trades',          hint: 'Max concurrent S2 positions',                                                            type: 'number', step: 1 },
          { key: 's2_ema_fast',           label: 'EMA Fast Period',          hint: 'Fast EMA period used for 5-min trend filter (Step 1) and 5-min exit detection (default 9)',  type: 'number', step: 1 },
          { key: 's2_ema_slow',           label: 'EMA Slow Period',          hint: 'Slow EMA period used for 5-min trend filter (Step 1) and 5-min exit detection (default 21)', type: 'number', step: 1 },
          { key: 's2_cooldown_minutes',   label: 'Cooldown (min)',           hint: 'Re-entry wait after stop or EMA cross exit on this symbol',                              type: 'number', step: 1 },
          { key: 's2_max_spread_pct',     label: 'Max Option Spread (%)',    hint: 'Skip contract if (ask−bid)/mid exceeds this. 0.10 = 10% — filters illiquid strikes',     type: 'number', step: 0.01 },
          { key: 's2_max_trades_per_day', label: 'Max Trades / Symbol / Day',hint: 'Cap on S2 entries per symbol per day — multiple pullbacks allowed. 0 = no cap',         type: 'number', step: 1 },
          { key: 's2_volume_min_ratio',   label: 'Volume Threshold',         hint: 'Confirmation bar volume must be ≥ this × the 20-bar average. 1.0 rejected ~half of all bars near-randomly (Jul 15: 34 of 51 signals); 0.8 keeps only thin-tape protection', type: 'number', step: 0.05 },
          { key: 's2_cross_max_bars_old', label: 'Cross Freshness (bars)',   hint: 'Max age of the EMA9/21 cross in 5-min bars, counted within TODAY only — prior-day crosses always block. 40 passes any same-day cross through a 12:30 window. 0 = disabled', type: 'number', step: 1 },
          { key: 's2_puts_enabled',       label: 'PUT Entries',              hint: 'Kill switch for the S2 PUT side (live Jun–Jul: PUTs 4W/14L, −$554 — all on dips inside uptrends)', type: 'bool' },
          { key: 's2_put_15m_strict',     label: 'Strict PUT 15-min Gate',   hint: 'PUT requires a confirmed BEARISH 15-min trend — neutral or missing data blocks. CALL side unchanged', type: 'bool' },
        ],
      },
      {
        id: 's2_sizing',
        label: 'S2 — EMA Pullback: Sizing & Window',
        fields: [
          { key: 's2_amount_per_trade',   label: 'Premium Budget Cap ($)',  hint: 'Max USD premium per S2 trade',                                  type: 'number', step: 10 },
          { key: 's2_risk_per_trade',     label: 'Risk Per Trade ($)',      hint: 'USD at risk at stop — sizes qty = risk / (premium × stop%)',     type: 'number', step: 10 },
          { key: 's2_trading_start_time', label: 'Start Time (ET)',         hint: 'No S2 entries before this time',                                type: 'time' },
          { key: 's2_last_entry_time',    label: 'Last Entry Time (ET)',    hint: 'No new S2 entries after this',                                  type: 'time' },
          { key: 's2_trading_end_time',   label: 'End / Force-Close (ET)',  hint: 'All S2 positions closed here',                                  type: 'time' },
        ],
      },
      {
        id: 's2_exits',
        label: 'S2 — EMA Pullback: Exit Levels',
        fields: [
          { key: 's2_stop_loss_pct',              label: 'Stop Loss (decimal)',        hint: '0.12 = −12% from entry. FALLBACK only while Structural Levels are on (used when delta is unavailable)', type: 'number', step: 0.01 },
          { key: 's2_stop_loss_min_hold_minutes', label: 'Min Hold Before Stop (min)', hint: 'Suppress hard stop for N minutes after entry (0 = fires immediately)',              type: 'number', step: 1 },
          { key: 's2_take_profit_pct',            label: 'Take Profit (decimal)',      hint: 'Auto-TP at entry: 0.14 = +14%. Set 0 to disable — exit on 5-min EMA cross only',  type: 'number', step: 0.01 },
          { key: 's2_breakeven_pct',              label: 'Breakeven Trigger',          hint: 'Move stop to entry at this gain (0.10 = +10%)',                                    type: 'number', step: 0.01 },
          { key: 's2_trail_pct',                  label: 'Trail Start',                hint: 'Begin trailing stop at this gain (0.20 = +20%)',                                   type: 'number', step: 0.01 },
          { key: 's2_trail_from_current_pct',     label: 'Trail Distance',             hint: 'Stop = current × (1 − this). 0.05 = trail 5% below current price. Avoid <0.05 — a 1% trail stops out on the first tick of noise', type: 'number', step: 0.01 },
          { key: 's2_structure_exit_enabled',     label: 'Structure Exit',             hint: '1-min closes back through the 5-min EMA9 close the trade in 2–3 min. Off = legacy 5-min EMA9/21 cross (10–15 min lag, never beat the stop)', type: 'bool' },
          { key: 's2_structure_exit_bars',        label: 'Structure Exit Bars',        hint: 'Consecutive completed 1-min closes through EMA9 required (2 filters single wicks)', type: 'number', step: 1 },
          { key: 's2_structure_exit_margin_pct',  label: 'Structure Exit Margin',      hint: 'Close must be this far beyond EMA9 to count as broken (0.0015 = 0.15%). Entry sits AT the EMA9 — too tight = noise scratches valid trades', type: 'number', step: 0.0005 },
          { key: 's2_structure_exit_min_hold_minutes', label: 'Structure Exit Min Hold', hint: 'No structure exit for N minutes after entry — lets the trade breathe through entry-zone noise (stop/quick-loss stay active)', type: 'number', step: 1 },
        ],
      },
      {
        id: 's3_core',
        label: 'S3 — Ask-Wall Breakout (Stocks · Moomoo): Core',
        fields: [
          { key: 's3_enabled',               label: 'S3 Enabled',            hint: 'Master switch — starts the Moomoo tick/L2 engine at app startup (restart required to start/stop the engine)', type: 'bool' },
          { key: 's3_kill_switch',           label: 'Kill Switch',           hint: 'Flip ON to flatten every S3 position immediately and halt entries (hot — no restart)', type: 'bool' },
          { key: 's3_trading_start_time',    label: 'Start Time (ET)',       hint: 'No S3 entries before this (skips the opening rotation)',            type: 'time' },
          { key: 's3_last_entry_time',       label: 'Last Entry Time (ET)',  hint: 'No NEW S3 entries after this',                                       type: 'time' },
          { key: 's3_flatten_time',          label: 'Flatten Time (ET)',     hint: 'Every S3 position force-closed at this time',                        type: 'time' },
          { key: 's3_record_events',         label: 'Record Events',         hint: 'Write all ticks/books/decisions to s3_data/*.jsonl for the ReplayEngine', type: 'bool' },
        ],
      },
      {
        id: 's3_wall',
        label: 'S3 — Wall Detection & Consumption',
        fields: [
          { key: 's3_wall_abs_min_shares',     label: 'Wall Abs Min (shares)',   hint: 'Ask level must show at least this many shares to be a wall candidate',        type: 'number', step: 500 },
          { key: 's3_wall_rel_mult',           label: 'Wall Rel Multiple (×)',   hint: 'AND at least this × the robust median/MAD depth baseline (initially 5×)',     type: 'number', step: 0.5 },
          { key: 's3_wall_max_level',          label: 'Max Wall Level',          hint: 'Only walls within the top N ask levels are actionable',                       type: 'number', step: 1 },
          { key: 's3_wall_min_persist_sec',    label: 'Min Persistence (s)',     hint: 'Wall must survive this long before it counts',                                type: 'number', step: 0.5 },
          { key: 's3_wall_min_updates',        label: 'Min Book Updates',        hint: '…and across at least this many book updates',                                 type: 'number', step: 1 },
          { key: 's3_min_consumption_ratio',   label: 'Min Consumption Ratio',   hint: 'Matched aggressive-buy volume / initial wall size must reach this (0.60 = 60%) before a breakout is tradeable', type: 'number', step: 0.05 },
          { key: 's3_max_pull_ratio',          label: 'Max Pull Ratio',          hint: 'If more than this fraction of the wall vanishes UNMATCHED it is liquidity withdrawal — no trade', type: 'number', step: 0.05 },
          { key: 's3_confirm_ticks',           label: 'Confirm Ticks',           hint: 'A print must land at least N ticks above the former wall to confirm',         type: 'number', step: 1 },
          { key: 's3_reload_veto_frac',        label: 'Reload Veto Fraction',    hint: 'Wall refreshing by ≥ this × initial size after consumption = iceberg — vetoed, no trade. 0 = off', type: 'number', step: 0.05 },
          { key: 's3_reload_cooldown_sec',     label: 'Reload Cooldown (s)',     hint: 'A vetoed price level cannot be re-detected as a wall for this long',          type: 'number', step: 30 },
          { key: 's3_baseline_window_min',     label: 'Baseline Window (min)',   hint: 'Rolling window for the median/MAD depth baseline',                            type: 'number', step: 5 },
          { key: 's3_baseline_min_samples',    label: 'Baseline Min Samples',    hint: 'Book snapshots required before walls can be flagged',                         type: 'number', step: 10 },
        ],
      },
      {
        id: 's3_flow',
        label: 'S3 — Order Flow & Data Quality',
        fields: [
          { key: 's3_flow_short_window_sec',  label: 'Flow Short Window (s)',  hint: 'Short window for the aggressive-buy rate',                                     type: 'number', step: 1 },
          { key: 's3_flow_long_window_sec',   label: 'Flow Long Window (s)',   hint: 'Long reference window',                                                        type: 'number', step: 5 },
          { key: 's3_flow_accel_mult',        label: 'Acceleration (×)',       hint: 'Short-window buy rate must exceed this × the long-window rate',                type: 'number', step: 0.1 },
          { key: 's3_flow_min_imbalance',     label: 'Min Imbalance',          hint: '(buys − sells) / (buys + sells) over the short window (0.25 = 25% net buying)', type: 'number', step: 0.05 },
          { key: 's3_max_spread_ticks',       label: 'Max Spread (ticks)',     hint: 'Reject entries when the spread is wider than N ticks',                          type: 'number', step: 1 },
          { key: 's3_max_spread_pct',         label: 'Max Spread (fraction)',  hint: '…or wider than this fraction of price (0.002 = 0.2%)',                          type: 'number', step: 0.001 },
          { key: 's3_stale_data_max_ms',      label: 'Stale Data Max (ms)',    hint: 'Reject decisions on market data older than this',                               type: 'number', step: 100 },
          { key: 's3_halt_quiet_sec',         label: 'Halt Quiet (s)',         hint: 'Static book + silent tape for this long → suspected halt, entries blocked',     type: 'number', step: 5 },
        ],
      },
      {
        id: 's3_risk',
        label: 'S3 — Risk & Sizing',
        fields: [
          { key: 's3_max_risk_dollars',        label: 'Max Risk ($)',            hint: 'shares = floor(this / per-share risk at the structural stop)',           type: 'number', step: 10 },
          { key: 's3_max_notional',            label: 'Max Notional ($)',        hint: 'Cap on position value',                                                  type: 'number', step: 500 },
          { key: 's3_max_bp_fraction',         label: 'Max BP Fraction',         hint: 'Use at most this fraction of available buying power',                    type: 'number', step: 0.05 },
          { key: 's3_max_participation',       label: 'Max Participation',       hint: 'Shares ≤ this × trailing 1-min volume (0.05 = 5%)',                      type: 'number', step: 0.01 },
          { key: 's3_max_open_positions',      label: 'Max Open Positions',      hint: 'Concurrent S3 positions',                                                type: 'number', step: 1 },
          { key: 's3_max_portfolio_notional',  label: 'Max Portfolio Notional',  hint: 'Total S3 notional across positions',                                     type: 'number', step: 1000 },
          { key: 's3_max_trades_per_symbol',   label: 'Max Trades / Symbol',     hint: 'S3 entries per symbol per day',                                          type: 'number', step: 1 },
          { key: 's3_min_shares',              label: 'Min Shares (viability)',  hint: 'Skip entries sized below this share count — tiny positions on expensive stocks cannot beat spread+slippage. 0 = off', type: 'number', step: 5 },
          { key: 's3_max_daily_loss',          label: 'Max Daily Loss ($)',      hint: 'Halt S3 for the day once realized S3 P&L hits −this',                    type: 'number', step: 50 },
          { key: 's3_max_consecutive_losses',  label: 'Max Consecutive Losses',  hint: 'Halt S3 after N losers in a row',                                        type: 'number', step: 1 },
          { key: 's3_cooldown_minutes',        label: 'Cooldown (min)',          hint: 'Per-symbol re-entry wait after any exit',                                type: 'number', step: 5 },
        ],
      },
      {
        id: 's3_exec',
        label: 'S3 — Entry, Stops & Exits',
        fields: [
          { key: 's3_entry_slippage_ticks',   label: 'Entry Slippage Cap (ticks)', hint: 'Marketable limit = ask + N ticks. Strict cap — never a market order, never a passive bid below the wall', type: 'number', step: 1 },
          { key: 's3_entry_timeout_sec',      label: 'Entry Timeout (s)',          hint: 'Cancel unfilled entries after this',                                    type: 'number', step: 0.5 },
          { key: 's3_initial_tranche_pct',    label: 'Initial Tranche',            hint: 'Fraction bought on the first confirmed breakout (0.50 = 50%)',          type: 'number', step: 0.05 },
          { key: 's3_scale_window_sec',       label: 'Scale-In Window (s)',        hint: 'Add-on only if the post-entry micro-high breaks within this (initially 3s)', type: 'number', step: 0.5 },
          { key: 's3_scale_requires_flow',    label: 'Scale Needs Flow',           hint: 'Add-on also requires order flow still accelerating',                    type: 'bool' },
          { key: 's3_stop_vol_mult',          label: 'Stop Vol Multiple',          hint: 'Stop pad includes this × short-horizon volatility',                     type: 'number', step: 0.25 },
          { key: 's3_min_stop_ticks',         label: 'Min Stop (ticks)',           hint: 'Reject impractically tight stops',                                      type: 'number', step: 1 },
          { key: 's3_max_stop_pct',           label: 'Max Stop (fraction)',        hint: 'Reject stops wider than this fraction of entry (0.01 = 1%)',            type: 'number', step: 0.001 },
          { key: 's3_tp1_r_mult',             label: 'TP1 (R multiple)',           hint: '≈⅓ off at +this × R (R = VWAP entry − initial stop)',                   type: 'number', step: 0.25 },
          { key: 's3_tp2_r_mult',             label: 'TP2 (R multiple)',           hint: '≈⅓ off at +this × R',                                                   type: 'number', step: 0.25 },
          { key: 's3_breakeven_cost_ticks',   label: 'Breakeven + Costs (ticks)',  hint: 'After TP1 fill CONFIRMS, stop → entry + N ticks (est. costs). Stop only ever moves UP', type: 'number', step: 1 },
          { key: 's3_ema_exit_period',        label: 'Runner EMA Period',          hint: 'Runner exits on a completed 1-min close below this EMA',                type: 'number', step: 1 },
          { key: 's3_stagnation_exit_sec',    label: 'Stagnation Exit (s)',        hint: 'Before TP1: exit if no NEW post-entry high for this long — scalps must work fast. 0 = off', type: 'number', step: 15 },
          { key: 's3_reclaim_fail_exit',      label: 'Reclaim-Fail Exit',          hint: 'Before TP1: a print a full tick below the former wall exits immediately instead of riding to the stop', type: 'bool' },
          { key: 's3_exit_slippage_ticks',    label: 'Exit Slippage Cap (ticks)',  hint: 'Urgent exits: marketable limit = bid − N ticks; unfilled → cancel and go deeper', type: 'number', step: 1 },
          { key: 's3_exit_timeout_sec',       label: 'Exit Timeout (s)',           hint: 'Unfilled urgent exit is cancelled/replaced after this',                 type: 'number', step: 0.5 },
        ],
      },
    ],

    // ── Init ─────────────────────────────────────────────────────
    async init() {
      this.updateClock();
      setInterval(() => this.updateClock(), 1000);

      await Promise.all([
        this.loadSymbols(),
        this.loadIndicators(),
        this.loadGroups(),
        this.loadStrategies(),
        this.loadLive(),
        this.loadClosed(),
        this.loadHistory(),
        this.loadConfig(),
      ]);

      // Auto-refresh live trades every 30 s
      setInterval(() => { if (this.activeTab === 'trades' && this.tradeSubTab === 'open') this.loadLive(); }, 30000);
    },

    updateClock() {
      this.clock = new Date().toLocaleTimeString('en-US', {
        timeZone: 'America/New_York',
        hour12: true,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }) + ' ET';
    },

    // ── API helpers ──────────────────────────────────────────────
    async api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const res = await fetch(path, opts);
      if (!res.ok) {
        const err = await res.text();
        alert(`API error ${res.status}: ${err}`);
        throw new Error(err);
      }
      if (res.status === 204) return null;
      return res.json();
    },

    // ── Symbols (unified watchlist) ───────────────────────────────
    async loadSymbols() {
      this.symbols = await this.api('GET', '/api/symbols');
    },
    async addSymbol() {
      if (!this.newTicker.trim()) return;
      if (!this.newSymbolS1 && !this.newSymbolS2 && !this.newSymbolS3) return;
      await this.api('POST', '/api/symbols', {
        ticker: this.newTicker.trim().toUpperCase(),
        s1_enabled: this.newSymbolS1,
        s2_enabled: this.newSymbolS2,
        s3_enabled: this.newSymbolS3,
      });
      this.newTicker = '';
      await this.loadSymbols();
    },
    async toggleSymbol(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { active: !s.active });
      await this.loadSymbols();
    },
    async toggleS1(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { s1_enabled: !s.s1_enabled });
      await this.loadSymbols();
    },
    async toggleS2(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { s2_enabled: !s.s2_enabled });
      await this.loadSymbols();
    },
    async toggleS3(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { s3_enabled: !s.s3_enabled });
      await this.loadSymbols();
    },
    async deleteSymbol(id) {
      if (!confirm('Remove this symbol?')) return;
      await this.api('DELETE', `/api/symbols/${id}`);
      await this.loadSymbols();
    },

    // ── Indicators ───────────────────────────────────────────────
    async loadIndicators() {
      this.indicators = await this.api('GET', '/api/indicators');
    },
    async addIndicator() {
      if (!this.newIndicator.name.trim()) return;
      const key = this.newIndicator.key.trim()
        || this.newIndicator.name.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
      await this.api('POST', '/api/indicators', { ...this.newIndicator, key });
      this.newIndicator = { key: '', name: '', description: '', category: 'general', active: true };
      this.showAddIndicator = false;
      await this.loadIndicators();
    },
    async toggleIndicator(ind) {
      await this.api('PATCH', `/api/indicators/${ind.id}`, { active: !ind.active });
      await this.loadIndicators();
    },
    async deleteIndicator(id) {
      if (!confirm('Delete this indicator?')) return;
      await this.api('DELETE', `/api/indicators/${id}`);
      await this.loadIndicators();
    },

    // ── Groups ───────────────────────────────────────────────────
    async loadGroups() {
      this.groups = await this.api('GET', '/api/indicators/groups');
    },
    async addGroup() {
      if (!this.newGroup.name.trim()) return;
      const payload = {
        name: this.newGroup.name,
        logic_type: this.newGroup.logic_type,
        indicator_ids: this.newGroup.indicator_ids.map(Number),
      };
      await this.api('POST', '/api/indicators/groups', payload);
      this.newGroup = { name: '', logic_type: 'AND', indicator_ids: [] };
      this.showAddGroup = false;
      await this.loadGroups();
    },

    // ── Strategies ───────────────────────────────────────────────
    async loadStrategies() {
      this.strategies = await this.api('GET', '/api/indicators/strategies');
    },
    async addStrategy() {
      if (!this.newStrategy.name.trim()) return;
      const payload = {
        ...this.newStrategy,
        indicator_group_id: this.newStrategy.indicator_group_id
          ? Number(this.newStrategy.indicator_group_id)
          : null,
      };
      await this.api('POST', '/api/indicators/strategies', payload);
      this.newStrategy = { name: '', description: '', enabled: true, indicator_group_id: '' };
      this.showAddStrategy = false;
      await this.loadStrategies();
    },
    async toggleStrategy(s) {
      await this.api('PATCH', `/api/indicators/strategies/${s.id}`, { enabled: !s.enabled });
      await this.loadStrategies();
    },

    // ── Live indicator eval ───────────────────────────────────────
    async runEval() {
      if (!this.evalSymbol.trim()) return;
      this.evalLoading = true;
      this.evalResults  = [];
      this.evalGateStack = [];
      this.evalFirstBlocker = null;
      this.evalError = '';
      try {
        const sym = this.evalSymbol.trim().toUpperCase();
        const data = await this.api('GET', `/api/indicators/evaluate/${sym}?direction=${this.evalDirection}`);
        this.evalResults      = data.results      || [];
        this.evalAllPass      = data.all_pass;
        this.evalFullPass     = data.full_pass    ?? data.all_pass;
        this.evalGateStack    = data.gate_stack   || [];
        this.evalFirstBlocker = data.first_blocker ?? null;
        this.evalMeta = { bars_1m: data.bars_1m, bars_15m: data.bars_15m };
        this.evalSymbol = sym;
      } catch (e) {
        this.evalError = 'Could not fetch data — check Tradier token in .env';
      } finally {
        this.evalLoading = false;
      }
    },

    // ── Gate stack helpers ────────────────────────────────────────
    gateRowStyle(gate) {
      if (gate.pass) return 'background:#F0FDF4;';
      // Failed gate — is it the first blocker?
      if (this.evalFirstBlocker && gate.id === this.evalFirstBlocker.id)
        return 'background:#FFF1F2; border-left:3px solid #BE123C;';
      return 'background:#FFF7F7;';
    },

    // ── Trades ───────────────────────────────────────────────────
    async loadLive() {
      // Snapshot current P&L values before refreshing so we can show trend direction
      const snapshot = {};
      for (const t of this.liveTrades) {
        if (t.live_pnl != null) snapshot[t.id] = t.live_pnl;
      }
      this.liveTrades = await this.api('GET', '/api/trades/live');
      // Merge: keep prior snapshot, add any new values (first load stays in prevPnl too)
      this.prevPnl = { ...this.prevPnl, ...snapshot };
      // Always refresh orphan check alongside live trades
      await this.loadOrphans();
    },

    async loadOrphans() {
      this.orphanLoading = true;
      try {
        const data = await this.api('GET', '/api/trades/reconcile');
        this.orphanedPositions = data.orphaned_in_tradier || [];
      } catch (e) {
        this.orphanedPositions = [];
      } finally {
        this.orphanLoading = false;
      }
    },

    async adoptOrphan(symbol, qty, costPerUnit) {
      const costFmt = costPerUnit != null ? `$${costPerUnit.toFixed(4)}` : 'unknown';
      if (!confirm(
        `Adopt ${qty} contract(s) of ${symbol} into Ajoy?\n\n` +
        `Entry price: ${costFmt} per contract\n` +
        `Stop and take-profit levels will be computed from current settings.\n\n` +
        `Once adopted, this position will be managed by the scheduler just like any normal trade.`
      )) return;
      try {
        await this.api('POST', '/api/trades/orphan/adopt', {
          option_symbol: symbol,
          quantity: qty,
          cost_per_unit: costPerUnit,
        });
        // Refresh both open trades (now shows the adopted position) and orphan list
        await this.loadLive();
        await this.loadOrphans();
      } catch (e) {
        // error already shown by api()
      }
    },

    async closeOrphan(symbol, qty) {
      if (!confirm(`Close ${qty} contract(s) of ${symbol} in Tradier?\n\nThis will place a market sell order. No Ajoy record will be created.`)) return;
      try {
        await this.api('POST', '/api/trades/orphan/close', { option_symbol: symbol, quantity: qty });
        await this.loadOrphans();
      } catch (e) {
        // error already shown by api()
      }
    },

    // ── Level editor ─────────────────────────────────────────────
    openLevelEditor(t) {
      if (this.editingTradeId === t.id) {
        // toggle closed
        this.editingTradeId = null;
        return;
      }
      this.editingTradeId = t.id;
      this.editStopMode   = '$';
      this.editTpMode     = '$';
      this.editStopPrice  = t.stop_price != null ? Number(t.stop_price).toFixed(2) : '';
      this.editTpPrice    = t.tp2_price  != null ? Number(t.tp2_price).toFixed(2)  : '';
      this.editStopPct    = '';
      this.editTpPct      = '';
      this.editSaving     = false;
      this.editError      = '';
    },

    // Preview: dollar equivalent of a % input
    stopDollarPreview(entry) {
      const pct = parseFloat(this.editStopPct);
      if (isNaN(pct) || pct <= 0 || !entry) return '';
      return '= $' + (entry * (1 - pct / 100)).toFixed(2);
    },
    tpDollarPreview(entry) {
      const pct = parseFloat(this.editTpPct);
      if (isNaN(pct) || pct <= 0 || !entry) return '';
      return '= $' + (entry * (1 + pct / 100)).toFixed(2);
    },

    async saveLevels(t) {
      this.editError  = '';
      this.editSaving = true;
      const entry = t.entry_price;
      const payload = {};

      // Resolve stop: convert % → $ if needed
      if (this.editStopMode === '%') {
        const pct = parseFloat(this.editStopPct);
        if (!isNaN(pct) && pct > 0) payload.stop_price = parseFloat((entry * (1 - pct / 100)).toFixed(2));
      } else if (this.editStopPrice !== '') {
        payload.stop_price = parseFloat(this.editStopPrice);
      }

      // Resolve TP: convert % → $ if needed
      if (this.editTpMode === '%') {
        const pct = parseFloat(this.editTpPct);
        if (!isNaN(pct) && pct > 0) payload.tp2_price = parseFloat((entry * (1 + pct / 100)).toFixed(2));
      } else if (this.editTpPrice !== '') {
        payload.tp2_price = parseFloat(this.editTpPrice);
      }

      if (!Object.keys(payload).length) {
        this.editError  = 'Enter at least one value to update.';
        this.editSaving = false;
        return;
      }
      try {
        const updated = await this.api('PATCH', `/api/trades/${t.id}/levels`, payload);
        // Splice updated values back into the live list
        const idx = this.liveTrades.findIndex(x => x.id === t.id);
        if (idx !== -1) {
          this.liveTrades[idx].stop_price = updated.stop_price;
          this.liveTrades[idx].tp2_price  = updated.tp2_price;
        }
        this.editingTradeId = null;
      } catch (_) {
        this.editError = 'Save failed — check server log.';
      } finally {
        this.editSaving = false;
      }
    },

    async closeTrade(id) {
      if (!confirm('Manually close this position?')) return;
      await this.api('POST', '/api/trades/close', { trade_id: id });
      await this.loadLive();
      await this.loadClosed();
    },
    async loadClosed() {
      this.closedToday = await this.api('GET', '/api/history/today');
      this.todaySummary = await this.api('GET', '/api/history/summary/today');
    },
    async loadHistory() {
      this.history = await this.api('GET', '/api/history/last30');
      // Populate unique filter options
      const uniqueDates    = [...new Set(this.history.map(t => this.fmtDay(t.exit_time)))].filter(Boolean);
      const uniqueSymbols  = [...new Set(this.history.map(t => t.symbol))].filter(Boolean).sort();
      const uniqueReasons  = [...new Set(this.history.map(t => t.exit_reason))].filter(Boolean).sort();
      this.historyDates    = ['All', ...uniqueDates];
      this.historySymbols  = ['All', ...uniqueSymbols];
      this.historyReasons  = ['All', ...uniqueReasons];
      // Render chart: $nextTick ensures Alpine finishes updating x-show,
      // then a 20ms timeout lets the browser paint the canvas before drawing.
      this.$nextTick(() => setTimeout(() => this.renderPnlChart(), 20));
    },

    // ── History filtering ─────────────────────────────────────────
    filteredHistory() {
      const f = this.historyFilters;
      return this.history.filter(t => {
        if (f.date   !== 'All' && this.fmtDay(t.exit_time) !== f.date)   return false;
        if (f.symbol !== 'All' && t.symbol !== f.symbol)                  return false;
        if (f.dir    !== 'All' && t.direction !== f.dir)                  return false;
        if (f.reason !== 'All' && t.exit_reason !== f.reason)             return false;
        return true;
      });
    },

    // ── Cumulative P&L chart ──────────────────────────────────────
    renderPnlChart() {
      const canvas = document.getElementById('pnlChart');
      if (!canvas) return;
      if (!this.history.length) return;

      // Sort ascending by exit_time
      const sorted = [...this.history]
        .filter(t => t.exit_time)
        .sort((a, b) => new Date(a.exit_time) - new Date(b.exit_time));

      let cumulative = 0;
      const labels = [];
      const dataPoints = [];
      for (const t of sorted) {
        cumulative += (t.pnl || 0);
        labels.push(this.fmtDay(t.exit_time));
        dataPoints.push(parseFloat(cumulative.toFixed(2)));
      }

      const finalPnl = dataPoints[dataPoints.length - 1] || 0;
      const isPositive = finalPnl >= 0;
      const lineColor   = isPositive ? '#15803D' : '#BE123C';
      const fillColor   = isPositive ? 'rgba(21,128,61,0.10)' : 'rgba(190,18,60,0.10)';
      const pointColor  = isPositive ? '#15803D' : '#BE123C';

      if (this.pnlChart) { this.pnlChart.destroy(); this.pnlChart = null; }

      const ctx = canvas.getContext('2d');
      this.pnlChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'Cumulative P&L',
            data: dataPoints,
            borderColor: lineColor,
            backgroundColor: fillColor,
            fill: true,
            tension: 0.35,
            pointRadius: 4,
            pointHoverRadius: 6,
            pointBackgroundColor: pointColor,
            pointBorderColor: '#FFFFFF',
            pointBorderWidth: 1.5,
            borderWidth: 2,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#1C1035',
              titleColor: '#C4B5FD',
              bodyColor: '#FFFFFF',
              padding: 10,
              callbacks: {
                label: (ctx) => {
                  const v = ctx.parsed.y;
                  return ' ' + (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
                }
              }
            }
          },
          scales: {
            x: {
              grid: { color: '#F0EDF8' },
              ticks: { color: '#7C6FAA', font: { size: 11 }, maxTicksLimit: 10 },
              border: { color: '#DDD6EE' },
            },
            y: {
              grid: { color: '#F0EDF8' },
              border: { color: '#DDD6EE' },
              ticks: {
                color: '#7C6FAA',
                font: { size: 11 },
                callback: (v) => (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(0),
              }
            }
          }
        }
      });
    },

    // ── Trade helpers ────────────────────────────────────────────
    tradeStatus(t) {
      if (t.tp1_hit && t.be_stop_set) return 'BE Stop';
      if (t.tp1_hit) return 'TP1 Hit';
      return 'Active';
    },
    tradeStatusClass(t) {
      if (t.tp1_hit && t.be_stop_set) return 'trade-status-be';
      if (t.tp1_hit) return 'trade-status-tp1';
      return 'trade-status-active';
    },
    targetPct(t) {
      if (!t.tp2_price || !t.entry_price) return '–';
      const pct = ((t.tp2_price - t.entry_price) / t.entry_price * 100);
      return (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%';
    },

    // ── Exit reason badge class ───────────────────────────────────
    exitReasonClass(reason) {
      if (!reason) return 'er-gray';
      const r = reason.toUpperCase();
      if (r.includes('CUTOFF'))     return 'er-yellow';
      if (r.includes('TP2') || r.includes('TP1')) return 'er-green';
      if (r.includes('QUICK_LOSS')) return 'er-red';
      if (r.includes('VWAP'))       return 'er-red';
      if (r.includes('STOP'))       return 'er-red';
      if (r.includes('TREND'))      return 'er-gray';
      if (r.includes('EMA_CROSS'))  return 'er-yellow';
      return 'er-gray';
    },
    exitReasonLabel(reason) {
      if (!reason) return '–';
      return reason.replace(/_/g, ' ');
    },

    // ── Formatters ───────────────────────────────────────────────
    fmtPrice(v) {
      if (v == null) return '–';
      return '$' + Number(v).toFixed(2);
    },
    fmtDollar(v) {
      if (v == null) return '–';
      const n = Number(v);
      return (n >= 0 ? '+' : '') + '$' + Math.abs(n).toFixed(2);
    },
    // Ensure the ISO string is treated as UTC, not local time.
    // Pydantic now serialises with +00:00, but older DB rows may lack tz info.
    _toUtcDate(iso) {
      if (!iso) return null;
      // If there's no timezone indicator at all, append 'Z' (= UTC)
      const hasOffset = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso);
      return new Date(hasOffset ? iso : iso + 'Z');
    },
    fmtDate(iso) {
      if (!iso) return '–';
      return this._toUtcDate(iso).toLocaleString('en-US', {
        timeZone: 'America/New_York',
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
        hour12: true,
      });
    },
    fmtDay(iso) {
      if (!iso) return '–';
      return this._toUtcDate(iso).toLocaleDateString('en-US', {
        timeZone: 'America/New_York',
        month: 'short', day: 'numeric',
      });
    },
    fmtTime(iso) {
      if (!iso) return '–';
      return this._toUtcDate(iso).toLocaleTimeString('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit', minute: '2-digit',
        hour12: true,
      }) + ' ET';
    },
    trendStyle(trend) {
      if (trend === 'bullish') return 'color:#15803D';
      if (trend === 'bearish') return 'color:#BE123C';
      return 'color:#7C6FAA';
    },
    trendLabel(trend) {
      if (trend === 'bullish') return '▲ Bullish';
      if (trend === 'bearish') return '▼ Bearish';
      return '● Neutral';
    },

    // ── P&L Trend helpers ─────────────────────────────────────────
    // Compares current live_pnl to the value captured on the previous refresh.
    // Returns 'up' | 'down' | 'flat' (flat on first load or no change)
    _pnlDirection(t) {
      const prev = this.prevPnl[t.id];
      if (prev == null || t.live_pnl == null) return 'flat';
      if (t.live_pnl > prev + 0.005) return 'up';
      if (t.live_pnl < prev - 0.005) return 'down';
      return 'flat';
    },
    pnlTrendStyle(t) {
      const d = this._pnlDirection(t);
      if (d === 'up')   return 'color:#15803D';
      if (d === 'down') return 'color:#BE123C';
      return 'color:#7C6FAA';
    },
    pnlTrendLabel(t) {
      const d = this._pnlDirection(t);
      if (d === 'up')   return '▲';
      if (d === 'down') return '▼';
      return '●';
    },
    pnlTrendTitle(t) {
      const prev = this.prevPnl[t.id];
      if (prev == null) return 'First load — no prior snapshot';
      const diff = (t.live_pnl || 0) - prev;
      const sign = diff >= 0 ? '+' : '';
      return `vs prev refresh: ${sign}$${Math.abs(diff).toFixed(2)}`;
    },

    // ── Thesis tooltip ────────────────────────────────────────────
    // Shows the actual numbers behind the INTACT / AT RISK / BROKEN badge
    // so the trader can see exactly how far the stock is from VWAP.
    thesisTooltip(t) {
      const vwap = t.vwap_current;
      const stock = t.underlying_price;
      if (!vwap || !stock) return 'Thesis: no VWAP data yet';

      const diff_pct = ((stock - vwap) / vwap * 100).toFixed(2);
      const sign = diff_pct >= 0 ? '+' : '';
      const side = t.direction === 'PUT'
        ? (stock < vwap ? 'below VWAP ✓ correct side' : 'above VWAP ✗ wrong side')
        : (stock > vwap ? 'above VWAP ✓ correct side' : 'below VWAP ✗ wrong side');

      return `${t.symbol} $${stock.toFixed(2)} vs VWAP $${vwap.toFixed(2)} (${sign}${diff_pct}%) — ${side}\n\nINTACT = hold, let the bot manage it\nAT RISK = watch, could flip\nBROKEN = thesis invalidated — consider closing`;
    },

    // ── Settings / Config ─────────────────────────────────────────
    async loadConfig() {
      try {
        this.config = await this.api('GET', '/api/config');
        // Initialise save-state for each group
        this.configGroups.forEach(g => { this.configSaving[g.id] = 'idle'; });
      } catch (_) { /* non-fatal */ }
    },

    async saveConfigGroup(group) {
      this.configSaving[group.id] = 'saving';
      const payload = {};
      group.fields.forEach(f => {
        const v = this.config[f.key];
        if (v === undefined || v === null) return;
        // Booleans sent as "1"/"0"; everything else as string
        payload[f.key] = f.type === 'bool' ? (v ? '1' : '0') : String(v);
      });
      try {
        const result = await this.api('PATCH', '/api/config', payload);
        // Merge updated values back so the UI shows the server-confirmed values
        Object.assign(this.config, result.updated);
        this.configSaving[group.id] = 'saved';
        setTimeout(() => { this.configSaving[group.id] = 'idle'; }, 2500);
      } catch (_) {
        this.configSaving[group.id] = 'error';
        setTimeout(() => { this.configSaving[group.id] = 'idle'; }, 3000);
      }
    },

    // Format a config value for display (e.g. pct fields)
    configDisplayValue(field) {
      const v = this.config[field.key];
      if (v === undefined || v === null) return '–';
      return v;
    },

  };
}
