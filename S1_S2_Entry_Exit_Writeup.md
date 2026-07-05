# Strategy Entry & Exit Logic — S1 and S2

---

## Strategy 1 — VWAP Pullback (S1)

S1 trades options on stocks that are trending on the 15-minute chart and have pulled back close to VWAP. The idea: the trend is your friend; wait for a retest of VWAP, confirm the bounce, then enter.

### Entry — 6-Layer Gate Stack

All six layers must pass in sequence. Any failure exits early.

**Layer 1 — Trend + VWAP Pullback Signal (1-min + 15-min bars)**

Three conditions are AND-gated:

- *15-min trend*: EMA-21 on 15-min bars must be bullish (price above EMA) or bearish (price below EMA). Neutral = no trade. The trend must also be confirmed by N consecutive 15-min bars all on the correct EMA side (configurable, prevents entries on a freshly-flipped EMA).
- *Price side*: For a CALL, current 1-min price must be above VWAP. For a PUT, below VWAP. Hard gate — no band tolerance.
- *Pullback zone*: Price must be within `VWAP_BAND_PCT` (1.4%) of VWAP. Too far away = not a pullback yet. Too close = no directional conviction.

**Layer 2 — Multi-bar Bounce Confirmation (1-min bars)**

The last `BOUNCE_BARS_REQUIRED` (2) completed 1-min bars must both:
- All close on the correct VWAP side
- Each close must be *moving away* from VWAP (strictly rising for CALL, strictly falling for PUT)

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

### Trade Levels

| Level | Formula | Default |
|-------|---------|---------|
| Stop loss | entry × (1 − 0.20) | −20% |
| Take profit | entry × (1 + 0.22) | +22% |

---

### Exit — Priority Order

Exits are evaluated on every management tick (every 30 seconds). The first condition that fires wins.

**1. Quick Loss (0–15 min window)**
If the option drops 25%+ within the first 15 minutes of entry, exit immediately. This is the emergency brake for high-gamma options that can lose 25–30% before the VWAP break threshold triggers.

**2. Hard Stop Loss**
Exit when option mid-price ≤ stop price. Stop is suppressed for the first `STOP_LOSS_MIN_HOLD_MINUTES` after entry so early bid-ask noise doesn't prematurely stop out. Labeled `STOP` if at original level, `TRAILING_STOP` if the stop was already raised.

**3. Profit Target (TP)**
Exit when bid price ≥ target price. Uses bid (not mid) so the target only fires when you can actually receive it.

**4. VWAP Break**
Exit when the underlying crosses VWAP against the trade direction by more than `VWAP_EXIT_BAND_PCT` (0.3%). The exit band is tighter than the entry band (1.4%) — it triggers well before the hard stop on high-gamma options.

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

**Step 1b — 15-min Alignment Gate (higher-timeframe filter)**

Once the 5-min direction is established, the 15-min EMA trend must not oppose it:
- 5-min says PUT + 15-min is bullish → blocked
- 5-min says CALL + 15-min is bearish → blocked
- 15-min neutral → allowed (early session, consolidation)

This was added after observing S2 entering PUT trades while the 15-min chart was clearly bullish — the 5-min was having a minor dip inside a larger uptrend.

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

### Trade Levels

| Level | Formula | Default |
|-------|---------|---------|
| Stop loss | entry × (1 − 0.21) | −21% |
| Take profit | entry × (1 + 0.24) | +24% |

---

### Exit — Priority Order

S2 has its own exit engine evaluated on every management tick.

**1. Hard Stop Loss**
Exit when bid price ≤ stop price. Uses bid (not mid) to prevent the stop from triggering while the mid is above the stop but the bid has already dropped through it due to spread. No minimum hold time by default (`S2_STOP_LOSS_MIN_HOLD_MINUTES=0`).

**2. Trailing Stop (breakeven → trail cascade)**

- *Breakeven*: Once the option is ≥13% above entry, move the stop to entry price. The 13% threshold (raised from 10%) provides buffer for the bid-ask spread — at 10% the breakeven stop was exiting at a net loss because we exit at bid but the stop was set at the mid entry price.
- *Full trail*: Once the option is ≥20% above entry, trail the stop at 5% below the current mid price.

**3. EMA Cross (signal exit)**

If the 5-min EMA9 and EMA21 cross in the opposite direction of the trade — e.g. EMA9 crosses below EMA21 while holding a CALL — the position is closed. This is a signal-based exit: the same signal that got you in is now telling you the trend has reversed.

---

## Key Differences at a Glance

| | S1 — VWAP Pullback | S2 — EMA Pullback |
|---|---|---|
| **Timeframe** | 15-min trend, 1-min entry | 5-min trend, 1-min entry |
| **Entry trigger** | Price pulls back to VWAP band | Price pulls back to 5-min EMA9 |
| **Confirmation** | 2 rising/falling bars above/below VWAP + momentum candle | 1-min candle breaks pullback bar's high/low |
| **Higher-TF gate** | 15-min EMA must be confirmed for N bars | 15-min EMA must not oppose 5-min direction |
| **Market filter** | QQQ regime gate | None (only stock-level EMA) |
| **Scan interval** | Every 60 seconds | Every 30 seconds (shorter to catch 2-bar window) |
| **Breakeven trigger** | +10% → entry + 2% | +13% → entry price |
| **Stop loss** | −20% | −21% |
| **Take profit** | +22% | +24% |
| **Signal exit** | VWAP break (underlying) | EMA cross (opposite direction) |
| **Best candidates** | Liquid large-caps with strong VWAP respect | Trending mid/large-caps with clean EMA structure |
