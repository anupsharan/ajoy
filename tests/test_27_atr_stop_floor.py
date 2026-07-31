"""
ATR floor on the structural stop — Jul 30 2026 (ORCL #206, INTC #207, #208).

−$248 in one session, 0/3.  None of it was an exit bug: every fill landed on
or better than its trigger.  The fault was that the stop distance had no
relationship to how far the stock routinely moves:

    ORCL  #206  stop 0.78% of stock | 5-min ATR 0.77%  ->  1.02x one candle
    INTC  #207  stop 0.96%          |            1.18% ->  0.82x
    INTC  #208  stop 0.79%          |            1.19% ->  0.66x   <- STRUCTURAL

#208 is the one that matters: it ran under structural levels with R/R 1.90 and
still died, because STRUCT_MIN_REWARD_RISK compares reward to risk and
STRUCT_MIN_STOP_PCT is a floor in PREMIUM terms.  Neither can see the ATR.

S2 is structurally exposed: it enters at the top of the confirmation candle
while its invalidation (the pullback low) sits ~0.7-0.9% below, so every S2
trade has a sub-ATR stop by construction.

The fix: stop distance >= struct_min_stop_atr_mult x ATR(5-min).  Widen, then
RE-JUDGE R/R against the wider stop, then re-size so dollar risk is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.strategy import (
    aggregate_bars,
    compute_structural_levels,
    intraday_atr,
)
from app.services.tradier import Bar


def _bars_1m(n: int, *, start: float = 100.0, rng: float = 0.10,
             step: float = 0.0) -> list[Bar]:
    """n 1-min bars, each spanning `rng`, drifting by `step` per bar."""
    t0 = datetime(2026, 7, 30, 9, 30)
    out = []
    for i in range(n):
        base = start + i * step
        out.append(Bar(time=t0 + timedelta(minutes=i), open=base,
                       high=base + rng / 2, low=base - rng / 2,
                       close=base, volume=1000))
    return out


# ===========================================================================
# aggregate_bars — the 1-min -> 5-min fold
# ===========================================================================

def test_aggregate_bars_builds_ohlc_from_the_group():
    bars = _bars_1m(5, start=100.0, rng=0.10)
    out = aggregate_bars(bars, 5)
    assert len(out) == 1
    b = out[0]
    assert b.open == bars[0].open
    assert b.close == bars[-1].close
    assert b.high == max(x.high for x in bars)
    assert b.low == min(x.low for x in bars)
    assert b.volume == sum(x.volume for x in bars)


def test_aggregate_bars_drops_the_partial_trailing_bucket():
    """
    A forming bar's high/low are not real yet — including it understates range
    and would quietly shrink the ATR, i.e. shrink the very floor being built.
    """
    out = aggregate_bars(_bars_1m(7), 5)
    assert len(out) == 1, "partial 2-bar bucket must not become a 5-min bar"


def test_aggregate_bars_buckets_on_wall_clock_not_arrival_order():
    """Buckets must line up with the broker's own 09:30 / 09:35 boundaries."""
    bars = _bars_1m(10)
    bars = bars[2:]                      # feed starts at 09:32, mid-bucket
    out = aggregate_bars(bars, 5)
    # 09:32-09:34 is a partial bucket and is dropped; 09:35-09:39 is whole.
    assert len(out) == 1
    assert out[0].time.minute == 35


def test_intraday_atr_returns_zero_without_enough_history():
    """Must fail OPEN — 0.0 means 'unknown', and the caller must not block."""
    assert intraday_atr(_bars_1m(20), 5, 14) == 0.0
    assert intraday_atr([], 5, 14) == 0.0


def test_intraday_atr_measures_real_range():
    # 75 one-min bars -> 15 five-min bars -> ATR over 14
    atr = intraday_atr(_bars_1m(75, rng=0.10), 5, 14)
    assert atr > 0
    # Each 5-min bucket spans the same 0.10 as its members (flat drift).
    assert atr == pytest.approx(0.10, abs=0.01)


# ===========================================================================
# The floor itself
# ===========================================================================

_BASE = dict(direction="CALL", option_entry=2.24, delta=0.54,
             underlying_entry=92.77, target_underlying=94.10)


def test_stop_inside_one_atr_is_pushed_out():
    """INTC #208: chart stop 0.73 away, ATR 1.10 — must widen to 1.10."""
    r = compute_structural_levels(
        **_BASE, stop_underlying=92.04, min_stop_distance=1.10,
        min_reward_risk=1.2, max_stop_pct=0.30,
    )
    assert r.ok, r.skip_reason
    assert r.atr_widened is True
    assert r.stop_underlying == pytest.approx(92.77 - 1.10, abs=0.01)


def test_stop_already_beyond_the_floor_is_left_alone():
    """The floor is a minimum, never a target — a wide chart stop stays wide."""
    r = compute_structural_levels(
        **_BASE, stop_underlying=91.00, min_stop_distance=0.50,
        min_reward_risk=0.1, max_stop_pct=0.90,
    )
    assert r.ok, r.skip_reason
    assert r.atr_widened is False
    assert r.stop_underlying == pytest.approx(91.00, abs=0.01)


def test_widened_stop_shrinks_position_size_not_dollar_risk():
    """
    The whole point: a wider stop must cost contracts, not more money.
    risk_pct is what the scheduler divides risk_per_trade by, so a 1.5x wider
    stop must produce a ~1.5x larger risk_pct.
    """
    narrow = compute_structural_levels(
        **_BASE, stop_underlying=92.04, min_stop_distance=0.0,
        min_reward_risk=0.1, max_stop_pct=0.90)
    wide = compute_structural_levels(
        **_BASE, stop_underlying=92.04, min_stop_distance=0.73 * 1.5,
        min_reward_risk=0.1, max_stop_pct=0.90)
    assert wide.risk_pct == pytest.approx(narrow.risk_pct * 1.5, rel=0.02)

    def qty(risk_pct, risk_dollars=120.0, premium=2.24):
        return int(risk_dollars / (premium * 100 * risk_pct))

    assert qty(wide.risk_pct) < qty(narrow.risk_pct)


def test_reward_risk_is_rejudged_against_the_WIDER_stop():
    """
    A wider stop is a worse trade and must be re-scored as one.  Inheriting the
    pre-widening ratio would let a 1.90 R/R wave through a setup that is really
    1.01 — precisely the flattering number INTC #208 was entered on.
    """
    # chart stop 0.73 -> R/R 1.33/0.73 = 1.82 (passes)
    # ATR stop  1.32 -> R/R 1.33/1.32 = 1.01 (must now fail)
    r = compute_structural_levels(
        **_BASE, stop_underlying=92.04, min_stop_distance=1.32,
        min_reward_risk=1.2, max_stop_pct=0.90,
    )
    assert not r.ok
    assert "reward/risk" in r.skip_reason
    assert r.reward_risk == pytest.approx(1.01, abs=0.02)


def test_thin_contract_is_skipped_not_silently_clamped():
    """
    If honouring the floor would cost more than max_stop_pct, SKIP.  Clamping
    would hand back the sub-ATR stop the floor exists to prevent while
    reporting a healthy risk_pct — protected-looking and not protected.
    """
    r = compute_structural_levels(
        direction="CALL", option_entry=0.90, delta=0.54,
        underlying_entry=92.77, target_underlying=96.00,
        stop_underlying=92.04, min_stop_distance=1.10,
        min_reward_risk=1.2, max_stop_pct=0.30,
    )
    assert not r.ok
    assert "too thin" in r.skip_reason


def test_non_atr_path_keeps_its_original_clamping():
    """
    Regression guard: the skip above must fire ONLY when the ATR floor is what
    pushed the stop over.  A naturally-wide chart stop keeps the pre-existing
    clamp — this change must not quietly alter unrelated S1 behaviour.
    """
    r = compute_structural_levels(
        direction="CALL", option_entry=0.90, delta=0.54,
        underlying_entry=92.77, target_underlying=96.00,
        stop_underlying=91.67,          # 1.10 away from the chart itself
        min_stop_distance=0.0,          # floor disabled
        min_reward_risk=1.2, max_stop_pct=0.30,
    )
    assert r.ok, r.skip_reason
    assert r.risk_pct == pytest.approx(0.30)   # clamped, not skipped


def test_floor_disabled_reproduces_the_old_behaviour_exactly():
    off = compute_structural_levels(**_BASE, stop_underlying=92.04,
                                    min_stop_distance=0.0,
                                    min_reward_risk=1.2, max_stop_pct=0.30)
    none = compute_structural_levels(**_BASE, stop_underlying=92.04,
                                     min_reward_risk=1.2, max_stop_pct=0.30)
    assert (off.ok, off.stop_price, off.tp_price) == (none.ok, none.stop_price, none.tp_price)
    assert off.atr_widened is False


def test_put_side_widens_upward():
    r = compute_structural_levels(
        direction="PUT", option_entry=2.24, delta=-0.54,
        underlying_entry=92.77, target_underlying=91.00,
        stop_underlying=93.10, min_stop_distance=1.10,
        min_reward_risk=0.1, max_stop_pct=0.90,
    )
    assert r.ok, r.skip_reason
    assert r.atr_widened is True
    assert r.stop_underlying == pytest.approx(92.77 + 1.10, abs=0.01)


# ===========================================================================
# Replay of the three trades that caused this
# ===========================================================================

@pytest.mark.parametrize("name,entry_u,stop_u,target_u,premium,delta,atr", [
    # ORCL #206 — R/R was already ~0.5, structural rejects it on that alone
    ("ORCL #206", 126.41, 125.42, 126.90, 2.44, 0.48, 0.97),
    # INTC #207 — likewise
    ("INTC #207", 93.40, 92.50, 93.93, 2.50, 0.54, 1.10),
])
def test_the_two_morning_losers_are_rejected(name, entry_u, stop_u, target_u,
                                             premium, delta, atr):
    r = compute_structural_levels(
        direction="CALL", option_entry=premium, delta=delta,
        underlying_entry=entry_u, stop_underlying=stop_u,
        target_underlying=target_u,
        min_stop_distance=atr * 1.0,
        min_reward_risk=1.2, max_stop_pct=0.30,
    )
    assert not r.ok, f"{name} should not have been entered"


def test_intc_208_gets_a_stop_at_least_one_candle_wide():
    """
    The trade that ran under structural levels and still lost.  At 1.0x ATR it
    is still allowed (R/R 1.21 clears 1.20) but its stop moves 92.04 -> 91.67,
    a full 5-min candle instead of two-thirds of one.
    """
    r = compute_structural_levels(
        **_BASE, stop_underlying=92.04,
        min_stop_distance=1.10, min_reward_risk=1.2, max_stop_pct=0.35,
    )
    assert r.ok, r.skip_reason
    assert r.atr_widened is True
    dist = _BASE["underlying_entry"] - r.stop_underlying
    assert dist / 1.10 == pytest.approx(1.0, abs=1e-6) or dist / 1.10 > 1.0
    assert r.stop_price < 1.92, "option stop must sit below the old $1.92"


# ===========================================================================
# Wiring — the setting exists, is exposed, and reaches both strategies
# ===========================================================================

def test_setting_exists_and_defaults_to_one_atr():
    from app.config import Settings
    for k in ("struct_min_stop_atr_mult", "struct_stop_atr_minutes",
              "struct_stop_atr_period"):
        assert k in Settings.model_fields
    assert Settings().struct_min_stop_atr_mult == 1.0


def test_both_strategies_pass_the_floor_in():
    """
    S1 and S2 must BOTH apply it.  S2 is the one that needs it most (it enters
    at the top of the confirmation candle), so a fix wired only into S1 would
    miss the trade that prompted this.
    """
    import inspect
    from app.services import scheduler as sched

    for fn in (sched._attempt_entry, sched._attempt_entry_s2):
        src = inspect.getsource(fn)
        assert "min_stop_distance=" in src, f"{fn.__name__} has no ATR floor"
        assert "_stop_atr(" in src, f"{fn.__name__} never measures ATR"


def test_missing_atr_fails_open():
    """A symbol without enough bars must still be tradeable, not blocked."""
    from app.services.scheduler import _stop_atr
    assert _stop_atr(_bars_1m(10), "TEST") == 0.0     # 0 -> floor disabled


def test_ui_exposes_the_atr_floor():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static" / "js" / "app.js").read_text()
    assert "struct_min_stop_atr_mult" in js, "no Settings field for the ATR floor"
    html = (Path(__file__).parent.parent / "app" / "templates" / "index.html").read_text()
    import re
    m = re.search(r"app\.js\?v=(\d+)", html)
    assert m and int(m.group(1)) >= 29, "bump the cache-buster after editing app.js"
