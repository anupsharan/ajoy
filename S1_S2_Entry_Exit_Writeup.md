# Strategy Entry & Exit Logic — S1 and S2

---

## July 2026 Redesign — Structural Levels & Chop Filter

Two engine-level changes apply to **both** strategies (see `.env`):

**Structural (chart-based) stop/target levels** (`STRUCTURAL_LEVELS_ENABLED=1`).
Stops and targets are no longer flat percentages of option premium. The stop anchors to the setup's invalidation point on the *underlying* chart (S1: below the pullback low / VWAP; S2: below the pullback low / 5-min EMA9, mirrored for PUTs) and the target anchors to the session swing high/low. Both are translated to option prices via the contract's delta. Two gates come with this: entries with underlying reward/risk below `STRUCT_MIN_REWARD_RISK` (1.2) are skipped — no more entering with the stock 3 cents under its session high — and the option stop distance is clamped to 8–30% of premium. Position size = `RISK_PER_TRADE` dollars at the structural stop, so risk per trade stays constant. When delta is unavailable the old percentage levels are used as fallback.

**Chop-day filter** (`CHOP_FILTER_ENABLED=1`). Both strategies need a trending day; live results were profitable only in trending weeks. New entries (S1 + S2) are blocked while QQQ's session range is below `CHOP_MIN_RANGE_RATIO` (50%) of its daily ATR(14). Evaluated from 10:30 ET onward.

**Limit-order exits** (`EXIT_LIMIT_ORDERS_ENABLED=1`). Entries already save the half-spread with a limit at the mid; exits used to give it back by market-selling at the bid. Patient exits (TP2, RUNNER, TRAILING_STOP, STRUCT_EXIT, EMA_CROSS, TREND_REVERSAL, CUTOFF, MANUAL) now try a limit at the mid for `EXIT_LIMIT_TIMEOUT_SECONDS` (12 s), then cancel and market-sell. Partial limit fills are booked at their exact fill price and only the remainder is market-sold. Urgent exits (STOP, QUICK_LOSS, VWAP_BREAK) always market-sell immediately — chasing a falling market with a limit costs more than the half-spread saves.

**Runner mode** (`RUNNER_MODE_ENABLED=1`). The structural TP normally exits at the session swing high — but a breakout *through* that level with momentum is exactly when an option can pay far more than the target. When the bid comes within `RUNNER_PROXIMITY_PCT` (5%) of the TP and the last completed 1-min candle still shows momentum in the trade direction (same definition as the L3 entry candle), the TP is waived (broker resting TP cancelled) and the stop switches to a dedicated trail `RUNNER_TRAIL_PCT` (8%) below the price, ratcheting up only. If momentum has faded when price reaches the target, the TP fires normally. Runner exits are labeled `RUNNER` so their contribution is separately measurable.

---

## Strategy 1 — VWAP Pullback (S1)

S1 trades options on stocks that are trending on the 15-minute chart and have pulled back close to VWAP. The idea: the trend is your friend; wait for a retest of VWAP, confirm the bounce, then enter.

### Entry — 6-Layer Gate Stack

All six layers must pass in sequence. Any failure exits early.

**Layer 1 — Trend + VWAP Pullback Signal (1-min + 15-min bars)**

Three conditions are AND-gated:

- *15-min trend*: EMA-21 on 15-min bars must be bullish (price above EMA) or bearish (price below EMA). Neutral = no trade. The trend must also be confirmed by N consecutive 15-min bars all on the correct EMA side (configurable, prevents entries on a freshly-flipped EMA).
- *EMA slope filter* (`EMA_SLOPE_FILTER_ENABLED=1`, Jul 2026): the EMA-21 itself must be rising for CALLs / falling for PUTs over the last `EMA_SLOPE_LOOKBACK` (2) completed 15-min bars. Price above a flattening or rolling-over EMA21 is a stale trend, not a tradeable one — the pattern behind S1's losing CALLs (−$510 live). Checks the EMA's direction rather than price's position, so genuine VWAP pullbacks pass untouched. (EMA-21 is deliberately kept as the trend anchor instead of EMA-9: on a 15-min chart EMA-9 hugs price, and a normal pullback would dip below it exactly at the entry zone.)
- *Price side*: For a CALL, current 1-min price must be above VWAP. For a PUT, below VWAP. Hard gate — no band tolerance.
- *Pullback zone*: Price must be within `VWAP_BAND_PCT` (1.4%) of VWAP. Too far away = not a pullback yet. Too close = no directional conviction.

**Layer 2 — Bounce Confirmation (1-min bars)**

The last `BOUNCE_BARS_REQUIRED` (1, reduced from 2 in Jul 2026) completed 1-min bars must:
- All close on the correct VWAP side
- Each close must be *moving away* from VWAP (strictly rising for CALL, strictly falling for PUT)

Reduced to 1 because two confirmation bars plus the Layer 3 momentum candle meant entries landed 3+ bars after the bounce — buying the local top ~0.5% from VWAP, with the old exit band then firing on any normal retest.

This catches the difference between a stock that is still drifting toward VWAP (bar closes getting closer to it) versus one that has genuinely bounced off it.

**Layer 3 — Momentum Candle (1-min bars)**

The last completed 1-min bar must show directional conviction:
- CALL: bar is green (close > open) AND close is higher than the prior bar's close
- PUT: bar is red (close < open) AND close is lower than the prior bar's close

A doji or a green bar that doesn't close above the prior bar fails this check.

**Layer 4 — VWAP Slope (1-min bars)**

The intraday VWAP slope (computed over the last N bars) must agree with the direction. A rising VWAP allows CALLs; a falling VWAP allows PUTs. Flat VWAP passes through.

**Layer 5 — Market Regime / QQQ Gate**

QQQ's own VWAP-vs-price relationship is used as a broad market filter. If QQQ is in a bearish regime, CALL entries on non-QQQ symbols are blocked (and vice versa). Configurable — can be disabled.

**Layer 6 — IV Filter**

If the selected option's implied volatility exceeds `IV_MAX_THRESHOLD` (175%), the entry is skipped. High IV means inflated premiums that are unlikely to expand further on a move.

### Entry Execution

- Option selected: nearest ATM strike, next expiry (0–1 DTE preferred)
- Order type: limit at the mid-price
- Position sizing: fixed dollar risk per trade, capped at budget per contract

### Trade Levels (structural — Jul 2026)

| Level | Anchor (underlying) | Option translation |
|-------|--------------------|--------------------|
| Stop loss | min(pullback low, VWAP) × (1 − 0.1%) — CALL; mirrored for PUT | entry − |delta| × stop distance, clamped to −8%…−30% of premium |
| Take profit | session swing high (CALL) / low (PUT) | entry + |delta| × target distance |
| Entry gate | — | skipped if underlying reward/risk < 1.2 |

Fallback when delta unavailable: stop −19%, TP +22% of premium (`.env`).

---

### Exit — Priority Order

Exits are evaluated on every management tick (every 30 seconds). The first condition that fires wins.

**1. Quick Loss (0–15 min window)**
If the option drops 25%+ within the first 15 minutes of entry, exit immediately. This is the emergency brake for high-gamma options that can lose 25–30% before the VWAP break threshold triggers.

**2. Hard Stop Loss**
Exit when option mid-price ≤ stop price. Stop is suppressed for the first `STOP_LOSS_MIN_HOLD_MINUTES` after entry so early bid-ask noise doesn't prematurely stop out. Labeled `STOP` if at original level, `TRAILING_STOP` if the stop was already raised.

**3. Profit Target (TP)**
Exit when bid price ≥ target price. Uses bid (not mid) so the target only fires when you can actually receive it.

**4. VWAP Break** *(disabled under structural levels — `STRUCT_DISABLE_VWAP_BREAK=1`)*
Legacy exit: underlying crosses VWAP against the trade by more than `VWAP_EXIT_BAND_PCT` (0.3%). Disabled because the 0.3% band sits inside normal underlying noise — an ordinary VWAP retest was killing valid trades. The structural stop (anchored below the pullback low / VWAP) replaces it at a level derived from chart structure.

**5. Trailing Stop (two stages)**

Both stages activate only after the trade has been open for 15 minutes:

- *Stage 1 — Breakeven lock*: Once the option is ≥10% above entry, raise the stop to entry × 1.02 (locks in ~2% to cover commission).
- *Stage 2 — Dynamic trail*: Once the option is ≥16% above entry, trail the stop at 8% below the current option price. The floor rises as the price rises; it never moves down.

---

---

## Strategy 2 — EMA Pullback (S2)

S2 trades options on a shorter timeframe. It looks for the 5-minute EMA trend to be established, waits for a 1-minute pullback to the 5-min EMA9, then enters on a 1-minute confirmation candle that breaks the pullback bar's range. Think of it as a mini version of S1, one timeframe lower.

### Entry — 3-Step Sequential Pattern + Pre-filters

**Step 1a — 5-min Trend Filter**

All four conditions must hold simultaneously (not just EMA crossover):

| Direction | EMA9 vs EMA21 | Price vs VWAP | EMA9 slope | EMA21 slope |
|-----------|--------------|--------------|------------|-------------|
| CALL | EMA9 > EMA21 | Close > VWAP | Rising (↑) | Rising (↑) |
| PUT | EMA9 < EMA21 | Close < VWAP | Falling (↓) | Falling (↓) |

Result is cached until a new completed 5-min candle appears — the trend can't flip mid-candle.

**Step 1b — 15-min Alignment Gate (higher-timeframe filter, asymmetric)**

Once the 5-min direction is established, the 15-min EMA trend is checked — with different rules per side:

- CALL: blocked only if the 15-min trend is bearish. Neutral is allowed (early session, consolidation).
- PUT (strict mode, `S2_PUT_15M_STRICT=1`, default on): requires the 15-min trend to be *confirmed bearish*. Neutral or missing 15-min data blocks the entry.
- `S2_PUTS_ENABLED=0` disables S2 PUT entries entirely (kill switch).

Why asymmetric: live results (Jun 8 – Jul 7, 2026) showed S2 PUTs went 4W/14L for −$554 while CALLs were net positive. Almost every losing PUT fired on a 5-min dip inside a larger 15-min uptrend — the old symmetric gate let them through because a neutral 15-min allowed both directions. PUTs now need positive confirmation of a downtrend, not just the absence of an uptrend.

**Step 2 — 1-min Pullback to 5-min EMA9**

The bar *before* the current bar (bars[-2]) must have touched or pierced the 5-min EMA9 level:
- CALL: pullback bar's Low ≤ EMA9, or close within 0.10% of EMA9
- PUT: pullback bar's High ≥ EMA9, or close within 0.10% of EMA9

This enforces that the stock actually pulled back to the EMA — not just approached it.

**Step 3 — 1-min Confirmation Candle**

The most recently completed 1-min bar (bars[-1]) must break the pullback bar's range with conviction:
- CALL: bullish bar (close > open) AND close > prior bar's High
- PUT: bearish bar (close < open) AND close < prior bar's Low

A weak recovery that doesn't clear the prior bar's high/low fails. This is the trigger.

**Post-selection Filters (after option contract is chosen)**

- *Spread filter*: bid/ask spread must be ≤ 10% of mid. Wide spreads mean slippage will eat the edge.
- *Volume filter*: last completed 1-min bar volume must be ≥ 20-bar rolling average. Thin bars signal poor order flow.

### Trade Levels (structural — Jul 2026)

| Level | Anchor (underlying) | Option translation |
|-------|--------------------|--------------------|
| Stop loss | min(pullback low, 5m-EMA9) × (1 − 0.1%) — CALL; mirrored for PUT | entry − |delta| × stop distance, clamped to −8%…−30% of premium |
| Take profit | session swing high (CALL) / low (PUT) | entry + |delta| × target distance |
| Entry gate | — | skipped if underlying reward/risk < 1.2 |

Fallback when delta unavailable: stop −19% of premium, TP disabled (signal exit only).

---

### Exit — Priority Order

S2 has its own exit engine evaluated on every management tick.

**1. Hard Stop Loss**
Exit when bid price ≤ stop price. Uses bid (not mid) to prevent the stop from triggering while the mid is above the stop but the bid has already dropped through it due to spread. No minimum hold time by default (`S2_STOP_LOSS_MIN_HOLD_MINUTES=0`).

**2. Trailing Stop (breakeven → trail cascade)**

- *Breakeven*: Once the option is ≥13% above entry, move the stop to entry price. The 13% threshold (raised from 10%) provides buffer for the bid-ask spread — at 10% the breakeven stop was exiting at a net loss because we exit at bid but the stop was set at the mid entry price.
- *Full trail*: Once the option is ≥20% above entry, trail the stop at 8% below the current mid price (raised from 5%, and from a disastrous 1% in `.env` — the exit fills at the bid, so tight trails were converting winners into losses).

**3. Structure Exit (signal exit — replaced EMA cross, Jul 2026)**

Exit when the last `S2_STRUCTURE_EXIT_BARS` (2) completed 1-min bars close back through the 5-min EMA9 — the level the entry bounced off — by at least `S2_STRUCTURE_EXIT_MARGIN_PCT` (0.05%). Reacts within 2–3 minutes of the thesis breaking.

Why replaced: the old 5-min EMA9/21 cross needed 10–15 minutes to flip after price reversed — on short-dated options the position had already lost 15–20%, so the hard stop always fired first (live data: 2 EMA_CROSS exits in 43 trades, both losers). Set `S2_STRUCTURE_EXIT_ENABLED=0` to restore the legacy cross exit.

---

## Key Differences at a Glance

| | S1 — VWAP Pullback | S2 — EMA Pullback |
|---|---|---|
| **Timeframe** | 15-min trend, 1-min entry | 5-min trend, 1-min entry |
| **Entry trigger** | Price pulls back to VWAP band | Price pulls back to 5-min EMA9 |
| **Confirmation** | 2 rising/falling bars above/below VWAP + momentum candle | 1-min candle breaks pullback bar's high/low |
| **Higher-TF gate** | 15-min EMA must be confirmed for N bars | CALL: 15-min must not oppose · PUT: 15-min must be confirmed bearish (strict) |
| **Market filter** | QQQ regime gate | None (only stock-level EMA) |
| **Scan interval** | Every 60 seconds | Every 30 seconds (shorter to catch 2-bar window) |
| **Breakeven trigger** | +10% → entry + 2% | +13% → entry price |
| **Stop loss** | structural: below pullback low / VWAP (delta-translated) | structural: below pullback low / 5m-EMA9 (delta-translated) |
| **Take profit** | structural: session swing high/low (delta-translated) | structural: session swing high/low (delta-translated) |
| **Entry R/R gate** | skip if underlying R/R < 1.2 | skip if underlying R/R < 1.2 |
| **Signal exit** | — (VWAP break disabled under structural levels) | 1-min closes through 5m-EMA9 (structure exit) |
| **Chop filter** | QQQ range ≥ 50% of ATR(14) required | same (shared gate) |
| **Best candidates** | Liquid large-caps with strong VWAP respect | Trending mid/large-caps with clean EMA structure |
