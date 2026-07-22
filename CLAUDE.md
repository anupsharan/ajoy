# AJOY — Project Context & Session Handoff

> **Read this first.** This file onboards a fresh AI session (any model) into the full
> project context. It is the distilled memory of an intensive July 2026 collaboration
> that redesigned this bot. Keep it updated when major changes land.
> Last updated: **2026-07-20** (evening, after the ghost-trade post-mortem).

---

## 1. What this is

**Ajoy** is a personal intraday options-trading bot, trading **LIVE REAL MONEY**
(`USE_SANDBOX=0`) on Tradier for user **Anup** (single MacBook, Pacific timezone).
FastAPI + APScheduler + SQLite (`ajoy.db`) + Alpine.js dashboard (port 8000).
~$1,500 account. Small size: ~$75 risk/trade (recently halved from $150).

- **S1 "vwap_pullback"** — options; 15-min EMA21 trend + pullback to session VWAP, 1-min entry.
- **S2 "ema_cross"** — options; 5-min EMA9/21 trend + 1-min pullback to 5m-EMA9, breakout entry.
- **S3** — stocks via Moomoo, user's separate parallel project (`app/services/s3/`),
  currently `S3_ENABLED=0`. **Not built in these sessions — don't refactor it.**

Key files: `app/services/scheduler.py` (entry/exit orchestration, ~3k lines),
`app/services/strategy.py` (S1 + shared math), `app/services/strategy_ema.py` (S2),
`app/services/tradier.py` (API client), `app/routers/trades.py` (UI endpoints incl.
close/mark-closed/levels), `app/config.py` (pydantic Settings ← `.env`),
`static/js/app.js` (Settings-page field definitions in `configGroups`),
`app/templates/index.html` (dashboard), `guardian.py` (EOD cron safety close),
`S1_S2_Entry_Exit_Writeup.md` (strategy doc), two merit reports (`S1_…`/`S2_Entry_Conditions_Merit_Report.md`).

## 2. Timezone & session basics (constant confusion source)

- Log timestamps = **Pacific**. UI/trade times = **ET**. Bar times from Tradier = ET-naive.
- Entry windows: **both strategies 11:00–12:30 ET** (user pulled S1's last-entry from 13:00 to 12:30). Force-close: S1 15:50 ET, S2 15:30 ET.
- Guardian cron should run **15:55 ET = 12:55 PT** (`55 12 * * 1-5`) — cron is LOCAL PT.
- `date.today()` on this machine is PT; bar dates are ET — always derive "session" from
  `bars[-1].time.date()`, never the wall clock (see `session_bars()`).

## 3. The July 2026 redesign — what exists now and why

Original system lost −$1,009 over June 8–Jul 7 (87 S1 + 43 S2 trades, ~48% win rate,
avg win < avg loss). Root diagnosis: **exit engine inverted the payoff** (capped winners,
fat losers) and several measurement bugs. Everything below was built and tested in July:

**Entry-side (both strategies unless noted):**
- **Structural levels** — stop at chart invalidation (pullback low/VWAP for S1,
  pullback low/5m-EMA9 for S2) and target at session swing high/low, both translated
  to option prices via delta; clamped 8%–30% of premium. **R/R gate**: skip entries
  with underlying reward/risk < 1.2. Sizing = risk\_per\_trade at the structural stop.
  Fallback to percentage levels only when delta missing.
- **Chop gate** (shared): block ALL entries while QQQ **true range incl. overnight gap**
  < 50% of its ATR(14). Gap-aware since Jul 9 (plain range misread gap-and-go days).
- **Energy floor** (per-strategy toggles; currently S2-only): symbol's own true range
  must be ≥ 50% of its own ATR(14) — flat symbols can't continue pullbacks.
- **Volatility ceiling** (both ON): block when symbol range > 2.5× own ATR — post-event
  names (HOOD) whip premium ±25%/candle.
- **Contract floors**: min premium $1.00 (sub-$1 = penny quantization + fat spreads),
  spread ≤ 12% of mid (S1 gained a spread check it never had).
- **S1**: EMA-slope filter (EMA21 itself must rise for CALLs/fall for PUTs, 2-bar lookback)
  — blocks stale trends; bounce confirmation reduced to 1 bar; VWAP band tightened to
  0.8% with 0.4% min clearance (the 0.4–0.7% "donut" was the only profitable zone);
  min-clearance was configured but **never enforced** until fixed Jul 8.
- **S2**: strict PUT gate (PUT needs *confirmed bearish* 15-min; neutral blocks);
  session-aware cross freshness (cross must be from TODAY, ≤40 bars); volume filter
  softened to 0.8× the 20-bar average (1.0 rejected 34 of 51 valid signals in one day).
- **PUT kill switches** both strategies (`S1_PUTS_ENABLED` / `S2_PUTS_ENABLED`) — both
  currently **ON**; they are the July-28 review levers (see §6).

**Exit-side:**
- **S2 structure exit** — 2 consecutive 1-min closes through the 5m-EMA9 (margin 0.15%,
  **10-min min-hold** — entry sits AT the EMA9, so no-grace scratched valid trades).
  Replaced the old 5-min EMA-cross exit (10–15 min lag, never beat the stop).
- **Runner mode** — near TP with momentum, waive the (bot-set) TP and trail 8% below
  price; activation floor = entry × (1 + `RUNNER_FLOOR_LOCK_PCT`=3%). **Never waives a
  human-set TP** (`tp_manual` column). Exit reason `RUNNER`.
- **Limit-at-mid exits** — patient exits (TP2/RUNNER/TRAILING_STOP/STRUCT_EXIT/
  TREND_REVERSAL/CUTOFF/MANUAL) try limit@mid 12s then market; partial fills booked
  exactly. Urgent (STOP/QUICK_LOSS/VWAP_BREAK) go straight to market.
- **Broker disaster stop** — resting GTC stop at bot-stop × 0.92 (8% buffer). Bot handles
  normal exits; broker stop only fills if bot is dead or move gaps a tick. Broker TP is
  **skipped while a stop rests** (Tradier rejects two sells on the same contracts).
- Stop suppression windows: hard stop min-hold 5 min, quick-loss (−18%) armed 5–15 min.
- S1's old VWAP_BREAK band exit is **disabled** under structural levels (fired inside noise).
- Honest labels: `original_stop_price` column stored at entry; STOP vs TRAILING_STOP
  decided against it (was mislabeling every structural stop-out).

**Measurement bugs fixed (crucial history):**
1. **Phantom VWAP** (Jul 8): 1-min fetches include a +4-day buffer; S1's "session VWAP"
   was a multi-day blend → systematic wrong-side PUTs and a fake QQQ regime. Fixed via
   `session_bars()` in every VWAP consumer (L1, regime, adaptive band, L4, indicators,
   trades-router thesis). **June data is contaminated by this** — treat pre-Jul-15
   analytics as unreliable.
2. Chop gate gap-blindness (Jul 9). 3. Freshness calendar-blindness (Jul 8).
4. Exit-label lie (fixed Jul 14 via original_stop). 5. Sandbox startup-banner lie (cosmetic).

**Reliability layer:** ghost-position reconciliation (rejected sell + position absent →
close DB from actual broker fill), mark-closed uses real fills + cancels zombie orders,
levels-edit validation fixed for profit-locked trades (stop may sit above entry),
guardian runs migrations first + records fills, logs rotate daily (14 days kept,
`ajoy.log.YYYY-MM-DD`), pytest never writes the live log.

## 4. Incident log (what bit us — don't re-learn these)

- **Jul 8 NFLX/AAPL**: phantom-VWAP entries. User's observation ("S1 and S2 disagree")
  found it. Believe the chart over the gate; audit calculations.
- **Jul 9 SOFI #137**: bot died mid-day; disaster stop concept born. Manual Tradier
  closes need real-fill reconciliation (was booked +$30, actually −$50 → patched).
- **Jul 13 GOOGL #140**: runner ate a manual TP and trailed below entry → `tp_manual`
  + floor≥entry fixes.
- **Jul 16–17 HOOD −$153**: hyper-ATR post-event name → volatility ceiling.
- **Jul 20 "ghost trades" −$373**: **S2 scanner checked clock but not calendar** — ran
  SATURDAY inside its 11:00–12:30 window on Friday's stale data, placed weekend limit
  orders that filled Monday 9:30 open. Fixed: `is_market_open()` guard in S2 scan +
  startup sweep cancels ALL resting buy orders. (User rotated API key + removed
  Antigravity IDE during the investigation — both fine but neither was the cause.)

## 5. Current config posture (see `.env` for authority)

Live, half-size, both directions, evaluation mode:
`RISK_PER_TRADE=75`, `S2_RISK_PER_TRADE=60` (user-set), max 1 S1 + 2 S2 concurrent,
windows 11:00–12:30 ET, chop 0.5 / energy floor 0.5 (S2) / ceiling 2.5 (both),
min premium $1.00, spread 12%, runner on (floor lock 3%), structure exit 10-min hold,
strict S2 PUT gate on, all PUT switches ON. Broker stop ON (8% buffer), broker TP ON
but auto-skipped while stop rests. `.env` is heavily commented with the rationale and
date of every change — **read those comments before touching values.**

## 6. THE STANDING AGREEMENT (most important section)

The system has cost real money; the user's patience is spent. As of Jul 20:

1. **No new features or gates until the July 28 review.** The machinery is sound;
   every recent loss was at a designed level with honest books.
2. **July 28 review** (~10 clean sessions since Jul 15): compute per-slice expectancy —
   CALLs vs PUTs, per strategy, runner-vs-waived-TP scoreboard, entry-hour, R/R buckets,
   chop-gate tally. Only trades with `entry_time >= 2026-07-15`, excluding
  `strategy_name='adopted_orphan'`, are clean.
3. **Decision rules pre-committed**: PUTs stay negative → flip the PUT kill switches.
   NO slice positive → **stop live trading, move to paper** until a slice proves out.
   The 11:00–12:30 window itself (chosen from contaminated June data, sits in the
   midday mean-reversion band) is a legitimate suspect — test alternatives in paper only.
4. Interim clean-engine scorecard (Jul 15–20): ~18 trades, ~40% win, ≈ −$250;
   **CALLs+runners ≈ positive, PUTs ≈ −$300** — the one robust split so far.

## 7. Working conventions with Anup

- **Answer briefly** — he says "answer in short to save tokens" often; default to compact.
  He reads charts well and catches real anomalies (his observations found 3 major bugs).
- **Evidence thresholds before tuning**: 1 bad trade = note it; 2–4 recurrences = named
  pattern with a tally; only then build. He'll sometimes push to tune after one loss —
  hold the line, explain, offer the reversible config option.
- Every fix ships with: config toggle where sensible, loud log lines, `.env` comment
  with date + incident, Settings-UI field, regression test named after the incident
  (e.g., "SOFI #137 regression"), and a full-suite run.
- He edits values via the Settings page (hot-applies + rewrites `.env`); **code changes
  need a bot restart** — always tell him which.
- Post-mortems: reconstruct from log + DB before judging; quantify; credit what worked;
  name what didn't. He responds well to "here's what the log says" over speculation.

## 8. Mechanics a new session must know

- **Test suite** (~530 tests): the mounted folder can't run sqlite tests directly.
  Pattern: copy to a scratch dir, rewrite hardcoded `/tmp/ajoy_` DB paths, split runs
  (suite ~40s+ vs shell timeout):
  ```
  cp -r app tests static .env <scratch>/ && cd <scratch>
  sed -i 's|/tmp/ajoy_|<scratch>/tmp_ajoy_|g' tests/*.py
  pytest tests/test_0*.py tests/test_1[0-4]*.py -q -p no:cacheprovider
  pytest tests/test_1[5-9]*.py tests/test_2*.py -q -p no:cacheprovider
  ```
  Also: pre-existing order-sensitivity — run test_09 before test_15 if together.
- **Live DB**: read-only OK via `sqlite3.connect('file:ajoy.db?mode=ro', uri=True)`;
  writes fail while the bot runs (give the user a one-liner to run locally instead).
- **Migrations**: append idempotent `ALTER TABLE` strings in `app/database.py::_migrate`
  (currently at v12: …, runner_mode, tp_manual, original_stop_price). Test DBs get the
  schema via `create_all`. Verify against a **copy** of live `ajoy.db` before shipping.
- **Settings UI**: fields live in `static/js/app.js::configGroups`; shared (S1+S2) groups
  get ids in the `['structural','chop','runner','energy']` list (green styling in
  index.html). **Bump the cache-buster** `app.js?v=N` in `index.html` on every app.js edit
  (currently v23). `node --check static/js/app.js` before shipping.
- **Config API** is generic — any Settings field is automatically GET/PATCH-able;
  UI exposure is just the configGroups entry.
- Logs: `ajoy.log` (today) + `ajoy.log.YYYY-MM-DD` (14 days). Forensics = grep these;
  they solved every incident. Funnel lines: `[L1]…[L6]`, `[S2-*]`, `[chop-gate]`,
  `[energy-gate]`, `[vol-ceiling]`, `Structural levels:`, `Trade OPENED/CLOSED`, `[ORPHAN]`.
- The user occasionally has parallel work in the repo (S3, UI tweaks) — expect files to
  change outside your session; `git log` is sparse (user commits rarely; working tree
  is the truth).

## 9. Open items / watch-list (post-review candidates, DO NOT build early)

- ATR-slope veto (exhausted big-range trends — ORCL-type), 1–2 observations.
- Lunch-reversal pattern (entries 11:00–12:15 stopped by ~12:30 counter-move): ~6 tallies;
  interacts with the window question.
- Thesis-transition logging (P(loss | AT_RISK)) — user interested; log, don't auto-close
  (auto-close = rebuilding the removed VWAP_BREAK noise exit).
- Config snapshot per trade (attribution) — deferred repeatedly, still worth it someday.
- True OCO orders; "runner scoreboard" once ≥10 runner exits; relative-strength filter
  (don't buy the day's weakest name); guardian cron time (user must fix crontab to
  `55 12 * * 1-5` — verify he did).

---
*If you (the assistant) change strategy behavior, add a dated line to §3/§4/§5 and keep
§6 current. This document is the project's memory across sessions.*
