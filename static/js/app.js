/* a-joy Alpine.js application */

function ajoy() {
  return {
    // ── Navigation ──────────────────────────────────────────────
    activeTab: 'trades',
    tabs: [
      { id: 'symbols',    label: 'Symbols (S1)' },
      { id: 'ema_cross',  label: 'EMA Cross (S2)' },
      { id: 'indicators', label: 'Indicators' },
      { id: 'trades',     label: 'Trades' },
      { id: 'settings',   label: 'Settings' },
    ],
    // ── Trade sub-tabs ───────────────────────────────────────────
    tradeSubTab: 'open',

    // ── Clock ────────────────────────────────────────────────────
    clock: '',

    // ── Symbols (S1 — VWAP pullback) ─────────────────────────────
    symbols: [],
    newTicker: '',

    // ── Symbols S2 (EMA crossover) ────────────────────────────────
    symbolsS2: [],
    newTickerS2: '',

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
    editStopPrice:  '',
    editTpPrice:    '',
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
          { key: 'stop_loss_pct',    label: 'Stop Loss (decimal)',  hint: '0.27 = -27% from entry',          type: 'number', step: 0.01 },
          { key: 'take_profit_pct',  label: 'Take Profit (decimal)',hint: '0.35 = +35% from entry',          type: 'number', step: 0.01 },
          { key: 'broker_stop_enabled', label: 'Broker-Side Stop',  hint: 'Resting stop order at Tradier — protects position if bot goes down', type: 'bool' },
        ],
      },
      {
        id: 'entry',
        label: 'Entry Filters',
        fields: [
          { key: 'vwap_band_pct',            label: 'VWAP Band — Normal',      hint: 'Pullback tolerance when QQQ is flat (0.009 = 0.9%)',  type: 'number', step: 0.001 },
          { key: 'vwap_min_clearance_pct', label: 'VWAP Min Clearance',      hint: 'Stock must be at least this far from VWAP on the correct side — blocks AT RISK entries (0.002 = 0.2%). Set 0 to disable.', type: 'number', step: 0.001 },
          { key: 'ema_period',              label: 'EMA Period',              hint: 'Trend direction (default 21)',                                                    type: 'number', step: 1 },
          { key: 'ema_consecutive_bars',  label: 'EMA Confirm Bars',       hint: 'Consecutive bars on correct EMA side',                                           type: 'number', step: 1 },
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
          { key: 'vwap_exit_band_pct',          label: 'VWAP Exit Band',             hint: 'VWAP_BREAK fires when underlying passes VWAP by this % (0.003 = 0.3%)', type: 'number', step: 0.001 },
          { key: 'trend_reversal_min_hold_minutes', label: 'Reversal Min Hold (min)', hint: 'Suppress TREND_REVERSAL for N min after entry (only when profitable)', type: 'number', step: 1 },
          { key: 'trend_reversal_confirm_bars',     label: 'Reversal Confirm Bars',   hint: '1 = single bar triggers exit, 2 = need 2 consecutive bars',             type: 'number', step: 1 },
          { key: 'trend_reversal_cooldown_minutes', label: 'Reversal Cooldown (min)', hint: 'Re-entry cooldown after a TREND_REVERSAL exit',                         type: 'number', step: 1 },
        ],
      },
      // ── Strategy 2 settings ──────────────────────────────────────
      {
        id: 's2_core',
        label: 'S2 — EMA Cross: Core',
        fields: [
          { key: 's2_enabled',         label: 'S2 Enabled',           hint: 'Master switch — enable the EMA crossover strategy scanner',  type: 'bool' },
          { key: 's2_max_open_trades', label: 'Max Open Trades',       hint: 'Max concurrent S2 positions',                               type: 'number', step: 1 },
          { key: 's2_ema_fast',        label: 'EMA Fast Period',       hint: '1-min and 5-min fast EMA (default 9)',                      type: 'number', step: 1 },
          { key: 's2_ema_slow',        label: 'EMA Slow Period',       hint: '1-min and 5-min slow EMA (default 21)',                     type: 'number', step: 1 },
          { key: 's2_volume_confirm',  label: 'Volume Confirm',        hint: 'Require trigger bar volume > previous bar volume',           type: 'bool' },
          { key: 's2_cooldown_minutes',label: 'Cooldown (min)',        hint: 'Re-entry wait after stop or EMA cross exit on this symbol', type: 'number', step: 1 },
        ],
      },
      {
        id: 's2_sizing',
        label: 'S2 — EMA Cross: Sizing & Window',
        fields: [
          { key: 's2_amount_per_trade',         label: 'Premium Budget Cap ($)',   hint: 'Max USD premium per S2 trade',                         type: 'number', step: 10 },
          { key: 's2_risk_per_trade',           label: 'Risk Per Trade ($)',       hint: 'USD at risk at stop — sizes qty = risk / (premium × stop%)', type: 'number', step: 10 },
          { key: 's2_trading_start_time',       label: 'Start Time (ET)',          hint: 'No S2 entries before this time',                       type: 'time' },
          { key: 's2_last_entry_time',          label: 'Last Entry Time (ET)',     hint: 'No new S2 entries after this',                         type: 'time' },
          { key: 's2_trading_end_time',         label: 'End / Force-Close (ET)',   hint: 'All S2 positions closed here',                         type: 'time' },
        ],
      },
      {
        id: 's2_exits',
        label: 'S2 — EMA Cross: Exit Levels',
        fields: [
          { key: 's2_stop_loss_pct',              label: 'Stop Loss (decimal)',       hint: '0.10 = -10% from entry',                               type: 'number', step: 0.01 },
          { key: 's2_stop_loss_min_hold_minutes', label: 'Min Hold Before Stop (min)',hint: 'Suppress hard stop for N minutes after entry (0 = fires immediately)', type: 'number', step: 1 },
          { key: 's2_take_profit_pct',            label: 'Take Profit (decimal)',     hint: 'Auto-set TP at entry: 0.14 = +14%. Set 0 to disable (exit on EMA cross only)', type: 'number', step: 0.01 },
          { key: 's2_breakeven_pct',              label: 'Breakeven Trigger',        hint: 'Move stop to entry at this gain (0.10 = +10%)',         type: 'number', step: 0.01 },
          { key: 's2_trail_pct',                  label: 'Trail Start',              hint: 'Begin trailing stop at this gain (0.20 = +20%)',        type: 'number', step: 0.01 },
          { key: 's2_trail_from_current_pct',     label: 'Trail Distance',           hint: 'Stop = current_price × (1 - this). 0.05 = trail 5% below current', type: 'number', step: 0.01 },
        ],
      },
    ],

    // ── Init ─────────────────────────────────────────────────────
    async init() {
      this.updateClock();
      setInterval(() => this.updateClock(), 1000);

      await Promise.all([
        this.loadSymbols(),
        this.loadSymbolsS2(),
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

    // ── Symbols ──────────────────────────────────────────────────
    async loadSymbols() {
      this.symbols = await this.api('GET', '/api/symbols');
    },
    async addSymbol() {
      if (!this.newTicker.trim()) return;
      await this.api('POST', '/api/symbols', { ticker: this.newTicker.trim().toUpperCase() });
      this.newTicker = '';
      await this.loadSymbols();
    },
    async toggleSymbol(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { active: !s.active });
      await this.loadSymbols();
    },
    async deleteSymbol(id) {
      if (!confirm('Remove this symbol?')) return;
      await this.api('DELETE', `/api/symbols/${id}`);
      await this.loadSymbols();
    },

    // ── Symbols S2 (EMA crossover) ────────────────────────────────
    async loadSymbolsS2() {
      this.symbolsS2 = await this.api('GET', '/api/symbols?strategy=S2');
    },
    async addSymbolS2() {
      if (!this.newTickerS2.trim()) return;
      await this.api('POST', '/api/symbols', {
        ticker: this.newTickerS2.trim().toUpperCase(),
        strategy: 'S2',
      });
      this.newTickerS2 = '';
      await this.loadSymbolsS2();
    },
    async toggleSymbolS2(s) {
      await this.api('PATCH', `/api/symbols/${s.id}`, { active: !s.active });
      await this.loadSymbolsS2();
    },
    async deleteSymbolS2(id) {
      if (!confirm('Remove this symbol from S2?')) return;
      await this.api('DELETE', `/api/symbols/${id}`);
      await this.loadSymbolsS2();
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
      this.editStopPrice  = t.stop_price != null ? Number(t.stop_price).toFixed(2) : '';
      this.editTpPrice    = t.tp2_price  != null ? Number(t.tp2_price).toFixed(2)  : '';
      this.editSaving     = false;
      this.editError      = '';
    },

    async saveLevels(t) {
      this.editError  = '';
      this.editSaving = true;
      const payload = {};
      if (this.editStopPrice !== '') payload.stop_price = parseFloat(this.editStopPrice);
      if (this.editTpPrice   !== '') payload.tp2_price  = parseFloat(this.editTpPrice);
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
