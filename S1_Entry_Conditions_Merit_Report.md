# S1 (VWAP Pullback) — Entry Condition Merit Report

*July 7, 2026 · Evidence: 87 closed live S1 trades with entry context (Jun 8 – Jul 7), 111,059 log lines covering the gate funnel, and code review. No external market-data connector was available for independent backtesting, and `backtest.py` had no cached data, so this report combines your own live evidence with day-trading domain judgment. Verdicts on individual gates are therefore strongest where your data speaks directly (the VWAP-distance gates) and softest where only block counts exist (the confirmation layers).*

---

## The entry funnel, measured

Across the log history, the scanner evaluated symbols continuously and the gates blocked in this order and volume: 4,434 scans ended at "waiting for pullback to VWAP" (the band — this is the scanner idling correctly, not a filter judgment), 1,211 rejections for price on the wrong VWAP side, 545 for the 15-min trend not yet confirmed two bars, and 75 for a neutral trend. Of the 1,383 scans that produced a valid L1 signal, the confirmation stack then removed most: 809 blocked by L2 (bounce not confirmed), 415 by the dual-timeframe EMA alignment, 399 by L3 (momentum candle), 175 by the QQQ regime gate, 140 by VWAP slope, and just 21 by the IV filter. Behavioral guards (cooldowns, daily caps, chase guard) blocked a further ~1,100 scans.

The picture: L1 defines the setup, L2/L3/EMA-alignment together throw away roughly 85% of valid L1 signals, and the remaining layers are light-touch insurance.

---

## The one finding that matters most: pullback depth

For every closed trade, the signed distance between the underlying entry price and VWAP was reconstructed. This is the strongest entry-quality predictor in your data by a wide margin:

| Distance from VWAP at entry | Trades | Win rate | Total PnL | Avg/trade |
|---|---|---|---|---|
| 0.0 – 0.2% (at VWAP) | 13 | 62% | −$250 | −$19 |
| 0.2 – 0.4% | 10 | 40% | −$233 | −$23 |
| **0.4 – 0.7%** | **31** | **65%** | **+$590** | **+$19** |
| 0.7 – 1.0% | 14 | 29% | −$421 | −$30 |
| > 1.0% (extended) | 19 | 37% | −$252 | −$13 |

The interpretation is clean and consistent with how VWAP pullbacks behave: entries **too close to VWAP** (< 0.4%) have no directional resolution yet — note the 62% "win rate" at 0.0–0.2% still loses money because the wins are tiny and the losses full-size. Entries **too far** (> 0.7%) aren't pullback entries at all — the bounce already happened and you're chasing it, paying an extended option price with the natural retracement pointed at your stop. The 0.4–0.7% zone is the genuine bounce zone, and it's the only segment of S1 that made money.

Two actions were taken directly from this table. First, a real bug: `VWAP_MIN_CLEARANCE_PCT=0.4%` was set in `.env`, documented in the code comments, and displayed in the Settings UI — but the live `check_entry_signal()` never enforced it (only the June 22 backup file did). The 23 sub-0.4% entries that cost −$483 walked through a gate everyone believed was closed. This is now fixed and unit-tested. Second, `VWAP_BAND_PCT` was tightened from 1.4% to 0.8%, eliminating the losing 0.7%+ chase zone. Between them, these two changes fence entries into the only historically profitable band.

---

## Verdict on each condition

**Layer 1 — 15-min EMA(21) trend, 2 consecutive confirming bars.** *Keep — necessary, not sufficient.* This is the strategy's premise and it can't be removed, but understand what it is: price-above-a-lagging-average is a weak trend definition that stays "bullish" deep into deterioration. The 2-bar confirmation (545 blocks) is a sound guard against fresh-flip entries. The trend-quality problem this layer can't solve — "is today even a trending day?" — is now the chop gate's job, which is the right separation of concerns.

**Price on the correct VWAP side.** *Keep — definitional.* A bullish bounce off VWAP requires price above VWAP; the 1,211 blocks are correct rejections, not lost opportunities.

**Pullback band + minimum clearance (the 0.4–0.7% donut).** *This is where S1's edge actually lives.* The distance table above is the direct evidence. With the clearance bug fixed and the band tightened, this gate pair is now the strategy's most data-supported component. Watch one side effect: the tighter donut will reduce trade count meaningfully — that is the intent, but if it drops to near-zero on trending days, widen the band to 0.9–1.0% before touching the clearance floor.

**Layer 2 — bounce confirmation (closes above VWAP, moving away).** *Keep at 1 bar — sound microstructure.* This is the only layer that distinguishes "still falling toward VWAP" from "has actually bounced," which is the difference between catching a knife and catching a bounce. At the original 2 bars (plus L3) it made entries 3+ bars late — the layers-fight-each-other problem — but at the current 1 bar it earns its place. Biggest post-L1 blocker (809), and most of those blocks are correct: price mid-drift toward VWAP fails it.

**Layer 3 — momentum candle (green + rising close).** *Mostly redundant — first candidate to drop.* With L2 at 1 bar, both layers now demand the last completed close exceed the prior close; L3's only additional demand is candle color, and the color of a single 1-minute candle is close to coin-flip noise in intraday research. Its 399 blocks overlap heavily with L2's. It's cheap and keeps out dojis, so it isn't hurting badly — but if the tightened band leaves you starved for entries, disable L3 first and expect little quality loss.

**Dual-timeframe EMA alignment (9/21 on 15-min and 1-min).** *The 15-min half is fine; the 1-min half is suspect.* The 15-min fast/slow check adds a modest earlier-warning layer over L1. The 1-min leg, however, was added reactively after a single trade (the NOW PUT), fires on 1-minute EMA spreads barely above its 0.1% noise margin, and duplicates what L2/L3 already verify on the same timeframe. It contributed to 415 blocks with no measurable counterfactual benefit. Second candidate for removal if entry frequency is too low.

**Layer 4 — VWAP slope.** *Keep — cheap and conceptually correct.* Same-session order-flow direction is real information that the multi-day 15-min EMA can't see. 140 blocks, low overlap with other layers, sensible threshold. No change recommended.

**Layer 5 — QQQ regime gate (±0.2% from VWAP).** *Keep, but don't expect it to carry weight.* The honest evidence is unflattering: S1 CALLs lost −$510 *with this gate active*, so at ±0.2% it wasn't catching what mattered. It's nearly free (no extra API call) and directionally sound, and the chop gate now covers the flat-market case it missed. If you tune anything here, raise the threshold to ~0.3% so "bullish regime" means something.

**Layer 6 — IV filter (175%).** *Keep as tail insurance; it is not an edge.* 21 skips in a month of scanning — at 175% it only excludes extreme premium (earnings-adjacent, meme spikes). That's precisely what you want blocked, and precisely how rarely you want a filter firing. Tightening toward ~120% is defensible but low priority.

**Cooldowns and daily caps.** *Keep — these are anti-tilt guards for a bot.* The 60-minute stop cooldown, TP cooldown, chase guard, and 2-loss/2-trade per-symbol caps blocked ~1,100 scans. NVDA's −$471 across 11 trades is what repeat-entry churn costs without them. They don't create edge; they stop edge from being churned away.

**New gates (structural R/R ≥ 1.2, chop filter).** *Expected to become the top-two conditions — currently unproven live.* The R/R gate is the only entry condition that reasons about the payoff rather than the pattern, which addresses S1's actual historical failure (payoff asymmetry, not signal accuracy). The chop gate addresses the strongest macro pattern in your results (profitable trending weeks, −$380 chop weeks). Both need live confirmation.

---

## Summary ranking

Carrying the edge: the 0.4–0.7% pullback donut, L2 bounce direction, the structural R/R gate, and the chop filter. Necessary scaffolding: L1 trend + side check, cooldowns/caps. Cheap insurance: L4 slope, L6 IV, L5 regime (weakest of the three). Probable noise: L3 momentum candle and the 1-min leg of EMA alignment — both are redundant with L2 on the same timeframe, and they're the levers to pull if the tightened band starves entry flow.

## Caveats, honestly stated

Eighty-seven trades over one month is a small sample, and every PnL figure was shaped by the *old* exit engine (noise-band VWAP exits, unprotected first 15 minutes) — so a "losing" entry bucket partly reflects bad exits, not purely bad entries. Block counts measure how often a gate fires, not whether the blocked trades would have lost; the true test of L3 and the EMA-alignment leg requires either ablation backtesting (needs historical bar data cached) or a few weeks of logging blocked-entry snapshots and paper-scoring them. Re-run the distance table after 3–4 weeks on the new engine — if the 0.4–0.7% edge holds under structural exits, it's real.

## Changes applied with this report

`VWAP_MIN_CLEARANCE_PCT` enforcement bug fixed in `check_entry_signal()` (with a regression test), and `VWAP_BAND_PCT` tightened 1.4% → 0.8% in `.env`. Everything else above is a recommendation, not a change.
