# AJOY — Project Context & Session Handoff

> **Read this first.** This file onboards a fresh AI session (any model) into the full
> project context. It is the distilled memory of an intensive July 2026 collaboration
> that redesigned this bot. Keep it updated when major changes land.
> Last updated: **2026-07-25** (multi-account support built; see §6c).

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
- **Jul 24 AMZN PS #176 −$76**: first PS trade, two lessons. (1) 6-min stop hold +
  6% scalp stop = the only live protection is the broker disaster stop 8% BELOW —
  planned $28 risk realized −$76 (−16.6%); PS hold cut to 2 min. (2) PS state
  signals (trend/thesis) were true but STALE — entered a PUT mid-bounce off the
  session low (big green 5-min candle); the 1-min red-bar guard is too weak.
  Bounce-entry tally hit 2 the SAME DAY (INTC PS #181 −$21, same pattern) →
  bounce guards built: PS blocked if last completed 5-min bar is green OR price
  > 0.5% above session low (PUT_SCALP_NO_GREEN_5M_ENABLED /
  PUT_SCALP_MAX_BOUNCE_FROM_LOW_PCT, UI fields, app.js v26, suite 547).
  Also Jul 24: S2 INTC PUT −$92 — user had flipped S2_PUTS_ENABLED thinking it
  was a master switch; reverted, S1/S2 stay CALL-only.  Deeper timing insight
  (named, not yet built): with entries starting ~11:00 ET, ALL short styles
  enter after the morning downmove is spent, into lunch stabilization — CALLs
  don't suffer this because uptrends resume afternoons.  If PS keeps losing
  even with bounce guards, the answer is an earlier short window or no shorts,
  not more entry filters.

## 5. Current config posture (see `.env` for authority)

Live, half-size, both directions, evaluation mode:
`RISK_PER_TRADE=75`, `S2_RISK_PER_TRADE=60` (user-set), max 1 S1 + 2 S2 concurrent,
windows 11:00–12:30 ET, chop 0.5 / energy floor 0.5 (S2) / ceiling 2.5 (both),
min premium $1.00, spread 12%, runner on (floor lock 3%), structure exit 10-min hold,
strict S2 PUT gate on, S1/S2 PUT switches OFF (Jul 23) but PUT Scalp mode ON (§6b).
**Jul 30 2026 (evening) — ATR FLOOR ON THE STOP.** The structural revert below was
only half the fix and the day proved it: ORCL #206 −$92, INTC #207 −$94, **INTC #208 −$62
which ran entirely under structural levels with R/R 1.90 and still died**. Day −$248, 0/3.
Stop distance vs each symbol's own 5-min ATR: **1.02× / 0.82× / 0.66× one candle**.
`STRUCT_MIN_REWARD_RISK` compares reward to risk; `STRUCT_MIN_STOP_PCT` is a floor in
*premium* terms — **nothing in the system compared risk to volatility.** S2 is structurally
exposed: it enters at the top of the confirmation candle while the pullback low sits
0.7–0.9% below, so every S2 stop is sub-ATR by construction. Built `STRUCT_MIN_STOP_ATR_MULT`
(default **1.0**): stop distance must be ≥ N × ATR(5-min, 14) of the underlying, measured by
folding the 1-min bars both strategies already hold (no extra Tradier call on the entry path).
Three design points that carry the correctness: (1) R/R is **re-judged after** widening — a
wider stop is a worse trade and must be re-scored, not inherit the flattering pre-widening
ratio; (2) if the floored stop needs more than `STRUCT_MAX_STOP_PCT` the trade is **skipped,
not clamped** — clamping would hand back the sub-ATR stop the floor exists to prevent while
reporting a healthy risk_pct; (3) missing ATR **fails open** (0.0 = floor off) so data
scarcity never blocks a trade. Sizing already divides by `struct.risk_pct`, so a wider stop
costs contracts, not dollars. Log line now carries `stop 1.10 = 1.00×ATR(1.10) [WIDENED by
ATR floor]` so the next post-mortem is a grep, not an evening of arithmetic. app.js **v29**,
`tests/test_27_atr_stop_floor.py` (20, mutation-tested 3 ways), suite **663**.
Expect fewer and smaller trades — replaying the day, ORCL #206 and INTC #207 are rejected
outright and #208's stop moves $1.92 → ~$1.72.
**Standing agreement status: the CALL-only engine is at −$74 over 31 trades since Jul 24
(was +$174 before Jul 30). §6's hard edge — negative over ~2 weeks → paper — is live.**

**Jul 30 2026 — STRUCTURAL LEVELS BACK ON** (ORCL #206 −$92 S1, INTC #207 −$94 S2).
Not an exit bug: both fills landed on the trigger (ORCL $2.01→$1.98, INTC $2.03→$2.03),
only 10 Tradier timeouts all day (vs 810 on Jul 27) and **zero PoolTimeout** — the Jul 27
pool-exhaustion hypothesis is dead, it was ReadTimeout/ConnectTimeout. The fault was
geometry: a fixed −14% option stop is 0.57% (ORCL) / 0.69% (INTC) of the STOCK once run
back through delta = **0.74× and 0.59× ONE 5-min ATR candle**. Both entries were a few
tenths below their SESSION HIGH, so reconstructed R/R was ≈0.5 — `STRUCT_MIN_REWARD_RISK`
1.2 would have skipped both outright. Aggregate since Jul 24 (29 S1+S2, 16 wins, −$12):
**under 10 min → 2/9, −$329; 10 min+ → 14/20, +$317**, and 6 of 11 stop-outs fired between
5.0–8.5 min, i.e. the instant the 5-min suppression lifts. Set `STRUCTURAL_LEVELS_ENABLED=1`.
**Coupled change — do not separate:** `RUNNER_TRAIL_PCT` 0.035→**0.08** and
`RUNNER_FLOOR_LOCK_PCT` 0.14→**0.03**. Those two were tuned for a FIXED +18% TP; with a
variable structural TP a 0.14 floor sits ABOVE the arming point for any TP under ~16.3%
(`runner_floor` is a `max()`, so it lands above the mid and the next manage tick stops the
trade out instantly — booking a small winner but killing the runner and mislabelling it a
STOP), and once the floor stops masking it a 3.5% premium trail is only ~0.14% of the
underlying. 0.08/0.03 is the pairing measured WITH structural on (Jul 23: runners 4/1, +$207).
Also noted, not built: **S2 has no max-extension gate** (S1 has `VWAP_BAND_PCT`) — INTC
entered 0.78% above its 5m-EMA9 on the confirmation candle's close. And S1's
`VWAP_MIN_CLEARANCE_PCT` blocked ORCL 4× at 0.16–0.38% then fired at 0.689%, i.e. on a range
day the clearance gate *guarantees* you buy the swing high. Both are candidates, not decisions.
Two tests were silently reading window/levels config from `.env` and are now pinned
(`test_10_trading_window::test_valid_midday_no_lunch`, `test_24`'s `_patch_all_layers`).

**Jul 30 2026**: added `S1_ENABLED` (Settings → Risk & Sizing, first field) — S1 finally
has a master toggle like S2/S3/PS. OFF = no NEW S1 entries; open S1 positions are still
managed to their exit. ANDed with the per-account S1 flag. app.js **v28**,
`tests/test_26_s1_master_switch.py` (11), suite **643**.
Broker stop ON (8% buffer), broker TP ON
but auto-skipped while stop rests. `.env` is heavily commented with the rationale and
date of every change — **read those comments before touching values.**

## 6. THE STANDING AGREEMENT — REVIEW EXECUTED EARLY (Jul 23)

After CRM −$140 (Jul 23), the user pulled the review forward: *"Do whatever you
wanted to do now. No point in waiting till 28th."* Final clean-engine numbers
(Jul 15–23, 21 trades, excl. adopted_orphan):

| Slice | n | W/L | Total | avg W / avg L |
|---|---|---|---|---|
| **CALLs (both)** | 10 | 5/5 | **+$155** | $63 / −$32 ← positive, 2:1 payoff |
| **Runner exits** | 5 | 4/1 | **+$207** | best mechanism in the system |
| **S1 PUTs** | 11 | 3/8 | **−$509** | $34 / −$76 ← the entire deficit |
| S2 PUTs | 0 | — | — | strict gate blocked all (correctly) |

**Root cause of PUT failure** (named Jul 22-23): pullback-style PUT entries suffer
adverse selection — on true trend-down days no pullback forms (no entry, e.g. Jul 23
morning: 464 "no pullback" + 277 "too far" blocks); on choppy down days pullbacks
DO form and are killed by the midday bounce (7 lunch-reversal tallies). Direction
wasn't the problem; the entry style is structurally mistimed for shorts.

**DECISIONS EXECUTED Jul 23:**
1. `S1_PUTS_ENABLED=0`, `S2_PUTS_ENABLED=0` — PUT side dead in .env (user must also
   flip both toggles in Settings UI for immediate effect / restart applies .env).
2. System continues **live, CALL-only** — the CALL slice earned it (+$155 at these sizes).
3. **Hard edge stands**: if CALLs go negative over the next ~2 weeks, stop live
   trading entirely → paper.
4. Still NO new features. Post-review candidates in §9 need their own evidence.
5. A short-side re-design (breakdown/momentum entries, not pullbacks) is a
   **paper-only** experiment if the user ever wants shorts again.

Note: user reverted/kept `RISK_PER_TRADE=150` (never halved) and made own tweaks
(MAX_OPEN_TRADES=2, window 11:00–12:45, runner 0.03/0.05, ORPHAN_STOP_EXCLUDED_SYMBOLS).
The working tree is the truth — always re-read `.env` before reasoning about config.

**"LAST TRY" CONFIGURATION (Jul 23 evening, user-directed):** after the PUT
verdict the user requested one final live configuration, CALL-only:
1. **Fixed levels replace structural**: `STRUCTURAL_LEVELS_ENABLED=0`,
   TP +21% / SL −17% both strategies (R/R 1.24 baked in).  The old
   VWAP_BREAK-resurrection coupling was fixed — `STRUCT_DISABLE_VWAP_BREAK=1`
   now keeps that noise exit dead regardless of structural on/off.
2. **Runner guarantees +18%**: proximity 0.025 (arms ~+18% vs the 21% TP),
   `RUNNER_FLOOR_LOCK_PCT=0.18` — any trade reaching +18% exits ≥ +18% or runs.
3. **Marketable-limit urgent exits**: STOP/QUICK_LOSS/VWAP_BREAK/SIGNAL_FADE
   sell via limit at bid × 0.97, 6 s timeout → market.  Caps the CRM/COIN-style
   velocity slippage (~$36 each).
4. **SIGNAL_FADE exit** (new, user-requested): every manage tick recomputes the
   dashboard's Stock Trend + Thesis; when BOTH oppose the trade (S1:
   completed-bar 15-min trend flipped AND underlying beyond exit band on wrong
   side of session VWAP; S2: 5-min filter fully validates the opposite
   direction) → immediate exit via marketable limit, `signal_conflict_time`
   stamped (migration v13).  Both-signals + bar-close trend is the noise guard
   distinguishing this from the removed VWAP_BREAK exit.
If THIS configuration loses over ~2 weeks, the agreed endpoint is paper/stop —
there is no "last last try".

## 6b. PUT SCALP MODE "PS" (built Jul 23 evening, user-directed)

User observed PUTs "go up 6-7% then reverse" and, with market sentiment negative,
wanted shorts back — as scalps. This is the breakdown-style short entry the PUT
post-mortem called for (NOT a pullback → no adverse selection), so it was built:

- **Entry**: Stock Trend (completed-bar 15-min EMA bearish) AND Thesis (underlying
  below session VWAP beyond `max(VWAP_EXIT_BAND_PCT,0.3%)`) — SIGNAL_FADE's two
  signals required at entry — plus red last-1-min momentum bar, QQQ regime not
  bullish, chop gate, vol ceiling, min premium, **own 8% spread gate** (12% would
  eat the 8% TP). Scanner `scan_for_put_scalp` (S1 cadence, market-open guarded).
- **Levels** (Jul 23 evening tune): fixed TP +11% / SL −6%; stop suppressed 2 min
  (was 6 — see Jul 24 AMZN #176 in §4; quick-loss + broker disaster stop stay live). **Runner arms at +7% GAIN** (not
  TP-proximity — passes proximity_pct=1.0 into should_activate_runner), trails 2%,
  **floor entry+5%** — user observed entries run +5-7% then reverse; the floor
  converts that exact pattern into a locked win instead of a round-trip loss.
- **Slots/size**: `PUT_SCALP_MAX_OPEN=1` **additive** to MAX_OPEN_TRADES (a scalp
  can't squeeze out a CALL); risk $75 (half); 30-min per-symbol cooldown after ANY
  PS exit (the signal is a STATE, not an event — no cooldown = machine-gun).
- **Isolation**: `strategy_name="put_scalp"`, red "PS" badge in UI; S1/S2 PUT
  switches stay 0; analytics must NEVER mix PS with the CALL-only verdict.
  Note: shared cooldown helpers are cross-strategy (a PS stop also cools S1 on
  that symbol for COOLDOWN_MINUTES — accepted, conservative).
- **Managed by the S1 manage branch** (falls through) with `_is_ps` overrides for
  runner params + stop hold; SIGNAL_FADE works inverted for PUTs automatically.
- Refactor: S1's fill-poll/cancel-race block extracted to `_await_entry_fill()`
  (single implementation of the ghost-trade guard, shared S1+PS).
- Config block in `.env` (PUT_SCALP_*), Settings UI group "PUT Scalp Mode (PS)",
  app.js cache-buster **v25**, tests `tests/test_22_put_scalp.py` (10), suite 545.
- PS is an experiment with its own verdict; it does NOT extend the §6 CALL edge.
  If PS bleeds, kill `PUT_SCALP_ENABLED` alone.

## 6c. MULTI-ACCOUNT SUPPORT (built Jul 25 2026, user-directed)

The bot was hard-wired to the single Tradier account in `.env`.  It now trades
**a list of accounts**, each with its own token + account number, its own
strategy enrolment, and its own sizing/slots.  User's ask: *"add multiple
accounts … I should also [have] a capability wherein I can pick and choose
between strategies and account."*

**Data model** — new `accounts` table (migration **v14**, `app/models.py::Account`):
credentials (`account_number`, `api_token`, optional `data_api_token`,
`use_sandbox`), state (`enabled`, `is_primary`, `sort_order`, `notes`),
four strategy flags (`s1/s2/s3/put_scalp_enabled`) and ten nullable sizing
overrides (`risk_per_trade`, `amount_per_trade`, `max_open_trades`,
`max_daily_loss` and the S2/PS equivalents).  **NULL = inherit the global
`.env` value.**  `trades.account_id` records which account holds each position.

**Startup migration** is automatic and idempotent (`seed_default_account`,
run from the lifespan): the `.env` account becomes account #1 "Primary",
`is_primary=1`, and **every existing trade is backfilled to it** — including
anything still OPEN at upgrade time, which otherwise would have been managed
by nobody.  Nothing to do by hand; the old `.env` credentials keep working.

**The core abstraction: the account rides on the client.**
`app/services/accounts.py` defines the immutable `AccountView`; each account
gets its own `TradierClient` carrying `client.ajoy_account`.  Scheduler code
reads it through three helpers — `_acct(client)`, `_s(client, "risk_per_trade")`
(override → global), `_aid(client)` (id to stamp on a new Trade).  This is why
the refactor didn't have to thread an `acct` argument through ~40 functions.
**Back-compat contract:** a client with no account (every mock in the test
suite, and any pre-existing caller) resolves to `legacy_view()`, whose
`id is None` → no account filtering and global settings, i.e. the exact old
behaviour.  That is what kept all 578 pre-existing tests green, unmodified.

**What is per-account vs global** (deliberate, user-chosen):
- **Per account**: which strategies run, risk/trade, premium cap, max open
  slots, daily-loss caps — plus every DB gate: open-slot counts, the
  per-symbol "already open" check, all cooldowns, per-symbol daily counts.
  Two accounts are separate books; an AMZN position in one must never block
  the same signal in another.
- **Global** (shared, still in Settings/.env): entry windows, chop/energy/vol
  gates, structural levels, runner, exit tuning, PUT switches, and the
  **master switches** `S2_ENABLED` / `PUT_SCALP_ENABLED` / `S3_ENABLED`.
  Account flags are **ANDed** with those masters — killing `PUT_SCALP_ENABLED`
  still kills PS everywhere (§6b's escape hatch is intact).

**Loop semantics** — `_for_each_account(job, flag, fn)` runs each scanner once
per enabled account and **isolates failures**: a revoked token in one account
logs an error and the others still scan.  Two rules that matter:
1. `manage_open_trades` iterates **ALL** accounts, not just enabled ones —
   disabling stops NEW entries but must never abandon an open position.
2. "Every account disabled" returns `[]` (a deliberate stop); an *empty or
   missing table* falls back to the `.env` account (pre-migration installs).

**Guardian sweeps every account** (including disabled ones — a paused account
can still hold a position opened before it was paused).  Migrations now run
*before* the account list is read, and one failing account can't abort the
others.  This was the sharpest correctness risk in the change: a single-account
guardian would have left accounts 2..N open overnight.

**UI** — new **Accounts** tab: add/edit/pause accounts, a per-account strategy
pill row (S1 / S2 / PS / S3), sizing overrides (blank = inherit), a **Test**
button that verifies credentials against Tradier, and delete (blocked while
the account holds an open trade — disable instead).  Tokens are **write-only**:
the API returns `••••abcd` and a PATCH without a token leaves it unchanged.
The Trades tab gains an account filter + a per-account strip (open count,
P&L today, strategies) and an ACCOUNT column — **all hidden while only one
account exists**, so a single-account setup looks unchanged.  app.js **v27**.

**Also changed:** SQLite busy timeout 5 s → **30 s** (more concurrent readers
now; a lock must delay an exit, never fail it); the two one-shot startup jobs
merged into one sequential `_startup_tasks` (they were contending over the DB
once each iterated every account); account list cached 3 s with explicit
invalidation on edit.  `/api/trades/reconcile` now sweeps every account and
tags each row — it returns 502 only when **every** account fails, so one
unreachable account can't hide another's real positions.

**Tests**: `tests/test_23_multi_account.py` (30) — the isolation primitives:
slot/cooldown/P&L scoping, client routing, credential-change eviction, seeding
+ backfill, disabled-account semantics, sizing arithmetic, token masking.
`tests/test_24_multi_account_e2e.py` (10) — the **money path**: drives the real
`_attempt_entry` / `_close_trade` with two accounts on two mocked brokers and
asserts on the ORDERS each broker received (one signal → two correctly-sized
buys; A's slot cap doesn't block B; the sell goes back to the account that
HOLDS the position).  Suite **618**.

Two harness lessons worth keeping:
- test_23/24 re-point `app.database` globals **inside their fixture only**.
  Doing it at import time (as test_09/test_15 do) leaked into test_15 and broke
  23 tests.  Don't add more import-time hijacks.
- test_24's mock broker is **stateful** (cancelled orders report CANCELED).
  A naive mock answering "filled" to every `get_order_status` makes
  `_close_trade` believe the resting stop won the cancel race, so it books the
  close *without placing a sell* — an exit test then passes while asserting
  nothing.  This bit me while writing it.

**UI verified in a real browser** (`uitest.py`, optional dev tool — needs
Playwright, not collected by pytest): all five tabs render, the Accounts tab
lists accounts with masked tokens, the edit panel and add-form templates
render, and the account filter + ACCOUNT column appear with 2 accounts and stay
hidden with 1.  Zero app-caused JS console errors.  Run `python uitest.py two`
/ `one` after touching app.js or index.html.

**Not changed on purpose:** the watchlist stays shared (one symbol list, the
existing s1/s2/s3 flags); S3's own engine was not refactored (§1 rule);
no strategy behaviour was touched — this is plumbing, and the §6 CALL-only
verdict and the §6b PS experiment stand exactly as they were.

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
  pytest tests/test_1[5-9]*.py tests/test_2*.py tests/test_s3.py -q -p no:cacheprovider
  ```
  (299 + 309 = **608** as of Jul 25.)
  Also: pre-existing order-sensitivity — run test_09 before test_15 if together.
- **Live DB**: read-only OK via `sqlite3.connect('file:ajoy.db?mode=ro', uri=True)`;
  writes fail while the bot runs (give the user a one-liner to run locally instead).
- **Migrations**: append idempotent `ALTER TABLE` strings in `app/database.py::_migrate`
  (currently at **v14**: …, tp_manual, original_stop_price, signal_conflict_time,
  trades.account_id). Test DBs get the schema via `create_all`. Verify against a
  **copy** of live `ajoy.db` before shipping.
- **Accounts**: rows in the `accounts` table, managed from the Accounts tab —
  NOT in `.env`. `.env` still holds the credentials that seed account #1 on
  first startup. Per-account overrides are nullable columns; NULL = inherit
  the global setting. See §6c.
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
