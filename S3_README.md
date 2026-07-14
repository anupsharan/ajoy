# S3 — Ask-Wall Breakout Scalper (Stocks · Moomoo OpenD)

Long-only US-stock breakout scalping. **Division of labor:**

- **Market data** — Moomoo OpenD at `127.0.0.1:11111`: tick-by-tick `TICKER`
  + ten-level `ORDER_BOOK` (LV2), 1-min/5-min K-lines.
- **Execution** — **Tradier** by default (`S3_BROKER=tradier`), reusing the
  app's existing credentials. Sandbox vs live is controlled by
  `S3_USE_SANDBOX` (`inherit` = follow the global `USE_SANDBOX`; `1`/`0`
  force S3 to sandbox/live **independently of S1/S2** — so S3 can run live
  while the options strategies stay wherever they are, or vice versa).
  Set `S3_BROKER=moomoo` to route orders through OpenD instead (then
  `S3_TRD_ENV`/`S3_TRADE_PWD` apply).

Trades the **S1 watchlist symbols** (`symbols` table, `active AND
s1_enabled`) from **9:40–11:00 ET**. Records into the existing `trades`
table with `strategy_name="S3"` so they appear in the normal
Trades/History UI.

Because Tradier exposes no order-event stream here, live orders are POLLED
(~1 Hz) and each snapshot is replayed through
`OrderManager.on_order_snapshot`, which synthesizes idempotent incremental
fills from monotonic `exec_quantity` deltas. That adds up to ~1 s of fill-
detection latency vs Moomoo's push — acceptable for the OMS since stops are
evaluated on every tick regardless.

## Quick start

```bash
uv sync                          # installs moomoo-api (data SDK)
# .env: S3_ENABLED=1. Orders go to Tradier: USE_SANDBOX=1 → paper,
#       USE_SANDBOX=0 → LIVE (same switch as S1/S2).
uv run python run.py             # engine starts from FastAPI lifespan
curl localhost:8000/api/s3/status   # state, brokers, capabilities, data quality
```

At startup the engine logs a **capability report** (SDK present, quote/trade
connection, trade unlock, LV2 order book, ticker push, modify_order,
bracket/OCO). If anything required is missing it idles safely — it never
takes down S1/S2.

## API assumptions (verified, not invented)

- SDK: `moomoo` package (fallback `futu`), `OpenQuoteContext` for data.
  `OpenSecTradeContext(filter_trdmarket=TrdMarket.US, security_firm=FUTUINC)`
  is opened ONLY when `S3_BROKER=moomoo`; in the default Tradier mode the
  Moomoo side is strictly data-only (no trade context, no unlock).
- Tradier execution uses `POST /accounts/{id}/orders` with `class=equity`,
  `type=limit`, `duration=day`; cancel via `DELETE /orders/{id}`; status,
  positions and buying power via the existing REST endpoints.
- Subscriptions: `SubType.ORDER_BOOK`, `SubType.TICKER`, `SubType.K_1M`,
  `SubType.K_5M` with `subscribe_push=True`. LV2 (10-level) US entitlement
  required for `ORDER_BOOK`.
- **No native bracket/OCO** exists for US equities in the Moomoo OpenAPI.
  Probed at startup (`supports_bracket`), and the OMS runs a race-safe
  software OCO instead (see below).
- REAL trading requires `unlock_trade(S3_TRADE_PWD)`. Missing/rejected
  password → signal-only mode, logged loudly.
- Aggressor side is inferred from the trade price vs the prevailing NBBO
  held immediately before the print — never from display colors or the
  SDK's `ticker_direction`.
- Session VWAP is computed cumulatively from 1-min turnover/volume (the SDK
  exposes no session-VWAP field). Halts are *inferred* (static book + silent
  tape for `S3_HALT_QUIET_SEC`) since pushes carry no halt flag; suspected
  halts block entries.

## Signal pipeline

```
SDK callbacks → bounded queue (drop-oldest, counted) → single engine thread
  MarketDataNormalizer   stale/dup/out-of-seq/crossed/locked/empty rejection
  TapeAnalyzer           aggressor inference, buy-rate windows, imbalance
  OrderBookAnalyzer      robust baseline → wall lifecycle → BreakoutSignal
  StrategyEngine         flow acceleration + 5-min VWAP/EMA context → EntryPlan
  RiskManager            every gate + sizing
  PositionManager/OMS    tranches, R exits, hard stop, software OCO
```

**Wall detection.** Rolling median/MAD baseline (P75 fallback when MAD=0)
per (symbol, side, level-band, time-of-day bucket). A wall = ask level within
the top `S3_WALL_MAX_LEVEL` showing ≥ `S3_WALL_ABS_MIN_SHARES` **and**
≥ `S3_WALL_REL_MULT` (5×) the baseline, persisting ≥ `S3_WALL_MIN_PERSIST_SEC`
across ≥ `S3_WALL_MIN_UPDATES` book updates.

**Consumption vs withdrawal.** Every wall-size reduction is matched against
aggressive-buy prints at/through the wall price within `S3_MATCH_WINDOW_MS`:

    consumption_ratio = matched aggressive-buy volume / initial wall size
    pull_ratio        = unmatched reduction           / initial wall size

Entry requires `consumption_ratio ≥ 0.60`, `pull_ratio ≤ 0.35`, the best ask
advancing beyond the former wall, a print ≥ wall + `S3_CONFIRM_TICKS`,
accelerating buy rate + imbalance, and 5-min context (price above VWAP,
EMA9 not against). Cancelled liquidity is treated as **liquidity
withdrawal** — no intent is attributed, ever.

**Entry order.** Marketable limit at `ask + S3_ENTRY_SLIPPAGE_TICKS` (strict
cap, cancelled after `S3_ENTRY_TIMEOUT_SEC`). Never a market order; never a
passive bid parked 2–3 cents under the wall.

## Risk & trade management

`shares = floor(S3_MAX_RISK_DOLLARS / (entry − structural stop))`, then
capped by buying power (`S3_MAX_BP_FRACTION`), notional, 1-min-volume
participation (`S3_MAX_PARTICIPATION`), per-symbol and portfolio limits.
Stops outside `[S3_MIN_STOP_TICKS, S3_MAX_STOP_PCT]` reject the trade.

- ~50% on the confirmed breakout; the rest only if the post-entry micro-high
  breaks within `S3_SCALE_WINDOW_SEC` (3 s), flow stays supportive, and
  combined worst-case risk stays within the original limit. Never averages
  down.
- Stop = structural invalidation (former wall / 1-min micro-swing low) −
  (spread + pad ticks + `S3_STOP_VOL_MULT`·σ), tick-snapped. Every partial
  fill is protected from the moment it lands. The stop only ratchets **up**.
- `R = VWAP entry − initial stop`. ⅓ off at +1R (stop → breakeven+costs
  after the fill *confirms*), ⅓ at +2R, runner exits on a completed 1-min
  close below EMA9. Integer allocation, remainders to the runner.
  Flatten at `S3_FLATTEN_TIME`.
- **Fast-failure exits (before TP1):** stagnation — no new post-entry high
  for `S3_STAGNATION_EXIT_SEC` (75 s) → exit; reclaim failure — any print a
  full tick below the former wall → exit immediately instead of riding to
  the lower hard stop.
- **Iceberg-reload veto:** a wall that refreshes upward by
  ≥ `S3_RELOAD_VETO_FRAC` (25%) of its initial size after ≥10% consumption
  is a seller with more behind it — the wall is discarded and the price
  level blocked from re-detection for `S3_RELOAD_COOLDOWN_SEC` (120 s).

**Software OCO** (no native bracket): stop-fire cancels resting targets and
sells freed shares as each cancel confirms; the OMS blocks any state where
outstanding sells exceed the position (oversell guard), dedupes deals by
`deal_id`, enforces monotonic fill quantities, and ignores stale
cancel-after-fill pushes.

## Safety systems

Max daily loss, consecutive-loss halt, per-symbol trade caps, cooldowns,
spread/slippage caps, stale-data rejection, suspected-halt blocking,
`S3_KILL_SWITCH` (hot — flattens and halts), graceful shutdown (cancel
orders, flatten, then close). On disconnect: entries blocked → reconnect +
resubscribe (exponential backoff) → reconcile broker orders/fills/positions
into local state → resume **only** after reconciliation succeeds. Position
mismatches adopt broker truth and are logged as errors.

## Replay

All normalized events + decisions stream to `s3_data/s3_YYYYMMDD.jsonl`:

```bash
python -m app.services.s3.replay_engine s3_data/s3_20260711.jsonl
```

runs the identical pipeline against a SimBroker (no OpenD) and prints
signals/trades/P&L — use it to tune thresholds.

## Settings

All thresholds live in `app/config.py` (`s3_*`, documented inline), are
editable in **Settings → Strategy 3** (hot-reload, written back to `.env`),
except connection/credentials (`S3_OPEND_HOST/PORT`, `S3_TRD_ENV`,
`S3_TRADE_PWD`) which are `.env`-only and never exposed via `/api/config`.
`S3_ENABLED` needs an app restart to start/stop the engine thread.
