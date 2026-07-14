# S2 (EMA Pullback) — Lever Merit Report

*July 8, 2026 · Method note: unlike the S1 report, this one deliberately leans on trading judgment rather than trade history. S2's live record (43 trades) was generated under a different exit engine, a broken freshness filter, the phantom-VWAP regime gate, and before the strict PUT gate — so its numbers describe a strategy that no longer exists. Where a historical fact is timeless (microstructure costs, not signal quality), it's cited; otherwise each lever is judged on its mechanism. Every verdict below is a hypothesis the next few clean weeks will confirm or kill.*

---

## What S2 is, structurally

S2 is a *momentum-continuation* strategy: wait for a fresh 5-minute trend, buy the first 1-minute pullback to the trend's own moving average, trigger on a breakout of the pullback bar. Its conceptual advantage over S1 is the trigger: S1 enters on "one rising close" (weak evidence), while S2 demands price *break the pullback bar's range* — a genuine micro-breakout with directional commitment already visible. Its conceptual disadvantage is fragility: the pattern is a rigid two-bar sequence scanned on a clock, so it recognizes only one shape of pullback. Keep both traits in mind — most of the lever judgments below trace back to one or the other.

---

## The levers, judged

**Step 1 — the four-condition 5-min trend filter (EMA9>EMA21 · close>VWAP · EMA9 slope↑ · EMA21 slope↑).**
*Sound core with one strict knob and one decorative one.* EMA9>EMA21 plus close-above-VWAP is a legitimate double confluence: trend structure *and* price above the session's average participant. Of the two slope conditions, EMA9-rising is nearly decorative — after a fresh cross (which the freshness gate separately requires), the fast EMA is rising almost by definition. EMA21-rising is the real gatekeeper: the slow EMA lags, so demanding it already rising *while the cross is still young* narrows entries to sharp impulse moves where the whole structure turned quickly. That's a defensible taste — impulse beginnings are the best pullback candidates — but understand it's the strictest condition in the stack and the first suspect if S2 barely trades. One technical note: the slope comparisons are strict `>` with no epsilon; at a genuinely flat EMA this flips on floating-point-scale noise. Cosmetic, but worth an epsilon eventually. The per-candle caching is good engineering — the trend verdict can't flap between candle closes.

**Step 1b — cross freshness (same-day only, ≤40 bars).**
*Now well-designed; keep.* After today's session-aware fix this lever finally expresses its true intent: only trade trends *born this morning*. For 1–2 DTE options that's not just signal hygiene, it's theta logic — a trend that started yesterday has already spent the cheap part of its move, and you'd be paying today's decay for yesterday's momentum. The 40-bar cap is now effectively "any same-day cross," which is correct given the entry window; the calendar boundary does the real work.

**Step 1c — 15-min alignment gate, asymmetric (CALL: not-opposed · PUT: confirmed bearish).**
*The best judgment call in the stack.* The asymmetry matches how equities actually behave: indexes drift upward, so a 5-min downtrend inside a neutral 15-min chart is usually a dip being bought, while a 5-min uptrend inside a neutral 15-min chart is often the start of something. Requiring positive proof of a bearish higher timeframe before shorting — rather than mere absence of a bullish one — is the correct burden of proof for counter-drift trades. The kill switch (`S2_PUTS_ENABLED`) backing it is good risk governance. This lever earns its place on mechanism alone; the live PUT record that motivated it was just the messenger.

**Step 2 — 1-min pullback to the 5-min EMA9 (bars[-2] touch, 0.10% tolerance).**
*Right idea, brittle recognition.* Anchoring the pullback to the trend's own EMA9 is coherent — you're buying the level that defines the trend's rhythm. The brittleness: only the bar immediately before the trigger may be the touch. A pullback that touches EMA9, then needs *two* bars to gather itself before breaking out, is invisible — bars[-2] is a recovery bar that never touched. The 30-second scan cadence partially compensates (each new bar re-frames the window), but the pattern still recognizes only prompt reactions. The improvement, if entry flow proves thin, is "touch within the last 2–3 bars" rather than exactly bars[-2] — it widens recognition without loosening quality. Not urgent; noted as the designed-in blind spot.

**Step 3 — confirmation candle (bullish body · close > pullback high · close > EMA9).**
*The strongest lever in S2 — this is the strategy's edge, such as it is.* Breaking the pullback bar's range is the classic continuation trigger: it means the dip found a buyer with enough conviction to reclaim the whole retracement, and you enter *with* that flow rather than predicting it. The close-back-above-EMA9 requirement filters weak bounces that stall under the level. If everything else in S2 were stripped away, this trigger plus a trend filter would still be a strategy; nothing else in the stack can say that.

**Spread filter — currently 30% of mid.**
*The worst number in either strategy's configuration, full stop.* This isn't a signal question, it's arithmetic: entering at mid on a 30%-spread contract means the position marks −15% the instant it fills — your quick-loss threshold consumed by friction before the underlying moves a cent. No entry signal, however good, survives paying that toll repeatedly. The code's own default is 10%. This should go back to 10–12%, and a minimum premium (~$1.00) belongs beside it — sub-dollar contracts are where 30% spreads live, and their percentage noise triggers every premium-based mechanism (quick-loss, clamps, trails) on spread wobble alone. This is the single change most likely to move S2's economics; flagged here per your instruction not to touch anything yet.

**Volume filter — confirmation bar ≥ 20-bar average.**
*Reasonable, unproven, mildly suspicious.* The intent — breakouts on volume are more real — has decades of support at daily timescales; at 1-minute resolution volume is spiky enough that "≥ average" rejects roughly half of all bars almost by coin flip. It's the kind of filter that looks wise and may be randomly halving your entry count. Keep it (the logic isn't wrong), but it's the second lever to relax — to ~0.8× average — if S2 trades too rarely, and the first to ablate once you have enough clean data to measure it.

**Shared gates — structural R/R ≥ 1.2 and the chop filter.**
*Top tier, same as for S1.* One S2-specific note on each. The R/R gate has a natural synergy with S2's timing: a *fresh* morning trend usually has its session extreme still far away, so good S2 entries should clear it easily — if you see frequent "no room to target" blocks on S2, that's the freshness thesis failing, not the gate misbehaving. The chop gate covers S2's worst-case day perfectly: 5-min EMA crosses are exactly what range-bound days manufacture in quantity, all of them fake.

**Behavioral guards — 20-min cooldown, 2 trades/day/symbol, S2-only daily loss breaker, TP-chase guard.**
*Keep all; they're anti-churn, not edge.* The one distinctive item is the S2-only loss breaker ($300) — a strategy-level circuit breaker is good governance for a strategy still proving itself, and it should stay until S2 has a clean profitable month.

**Contract selection — delta 0.30–0.55, nearest non-0DTE expiry.**
*Appropriate for the thesis.* A momentum-continuation trade wants delta exposure to a move that should happen within the hour; ATM-ish short-dated contracts are the right instrument. The real problem in this department is liquidity (the spread filter above), not the delta window.

**The exit side, briefly, because two levers define entry quality in hindsight:** the structure exit (two 1-min closes back through EMA9) is the mirror of the entry thesis and reacts at the timescale the entry lives on — conceptually right. And the structural stop for S2 sits just under the pullback low/EMA9, which is a *small* underlying distance; expect the 8% minimum-stop clamp to bind often. That's fine — it exists for exactly this — but it means S2's realized losers will cluster near −8 to −12%, so the R/R math works only if the trail lets winners reach +15% and beyond. Watch that ratio.

---

## Summary ranking

Carrying the edge: the Step 3 breakout trigger, the structural R/R gate, the chop filter, and the asymmetric 15-min gate. Sound scaffolding: the 5-min quad filter (EMA21-slope is the strictness dial), session-aware freshness, Step 2's pullback anchor, the behavioral guards. Actively harmful as configured: the 30% spread ceiling — fix when you're ready, alongside a $1 minimum premium. Unproven and worth watching: the volume filter, and Step 2's rigid two-bar recognition.

## What would falsify these judgments

If S2 barely trades in the next two weeks, loosen in this order: volume filter → EMA21-slope condition → Step 2 touch-window — and log which one releases flow. If S2 trades but losers cluster at the −8% clamp with winners dying at +10–12%, the problem isn't entries at all; it's that 5-min structure distances are too small for 2-DTE premium noise, and the honest responses are fewer/larger setups (10-min EMA structure) or slightly longer-dated contracts. And if PUTs stay rare even on red days, check whether the strict 15-min gate is demanding bearishness the 15-min EMA can't confirm until the move is over — that gate's burden of proof is right in principle but its speed is untested.
