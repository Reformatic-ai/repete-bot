"""The `reclaim` leg: sector ranking, the base-then-reclaim trigger, the two
new rails, and the per-strategy universe.

Boundary pairs throughout, in the style of test_short_rails.py and
test_credit_gate.py: every blocked case sits beside a permitted one differing in
exactly the thing under test. A rail that refused everything would satisfy the
blocked half alone, and so would a strategy that never signalled — which is the
specific way this strategy can fail invisibly, since "never fires" and "fires
and finds nothing" produce identical gate output.

`reclaim` ships DISABLED. These tests pin its behaviour before it can trade.
"""
import pytest

import risk
import strategies
from strategies import reclaim


ACCOUNT = {"equity": 100_000.0}

SECTORS = {
    "Alpha": ["AA1", "AA2", "AA3", "AA4", "AA5"],
    "Beta": ["BB1", "BB2", "BB3", "BB4", "BB5"],
    "Gamma": ["GG1", "GG2", "GG3", "GG4", "GG5"],
    "Delta": ["DD1", "DD2", "DD3", "DD4", "DD5"],
}

# Scaled-down but PROPORTIONED like production (200 : 40 : 50/10). The trend
# SMA has to be long relative to the dip, or the average converges down onto the
# dip and the name stops being "below" it — which is a property of the geometry,
# not of the strategy, and a compressed fixture would fail for that reason alone.
PARAMS = {
    "sector_sma_period": 20,
    "laggard_sector_count": 2,
    "min_sector_constituents": 3,
    "trend_sma_period": 20,
    "base_sma_period": 4,
    "min_days_below": 5,
    "base_slope_bars": 3,
    "exit_buffer_pct": 3.0,
    "max_hold_days": 60,
}


def _bars(closes):
    return [{"open": c, "high": c, "low": c, "close": c, "volume": 1_000,
             "ts": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00"}
            for i, c in enumerate(closes)]


def _cfg(**risk_overrides):
    base = {"max_position_pct": 100.0, "max_open_positions": 0}
    base.update(risk_overrides)
    return {"risk": base, "sectors": SECTORS, "symbols": ["CORE1", "CORE2"]}


# ============================================================ sector_open_count

def test_sector_open_count_excludes_the_candidate_and_unmapped_names():
    cfg = _cfg()
    positions = {"AA1": {}, "AA2": {}, "BB1": {}, "CORE1": {}}
    # AA3's own sector has two others open; AA3 itself is not counted even when
    # already held, because adding to a held name is max_position_pct's job.
    assert risk.sector_open_count(cfg, "AA3", positions) == 2
    assert risk.sector_open_count(cfg, "AA1", positions) == 1
    assert risk.sector_open_count(cfg, "BB2", positions) == 1
    # An unmapped symbol has no sector, so it constrains nobody and is
    # constrained by nobody — this is what keeps the rail inert for the core
    # universe and lets it ship without re-gating any existing strategy.
    assert risk.sector_open_count(cfg, "CORE1", positions) == 0
    assert risk.sector_open_count(cfg, "UNKNOWN", positions) == 0


# =================================================== sector_concentration rail

def _sector_cfg(max_per_sector=3):
    return _cfg(sector_concentration={"enabled": True,
                                      "max_per_sector": max_per_sector})


def test_the_third_position_in_a_sector_is_permitted_and_the_fourth_is_not():
    """The boundary pair, at a cap of 3."""
    cfg = _sector_cfg(3)
    two_open = {"AA1": {"market_value": 1_000.0},
                "AA2": {"market_value": 1_000.0}}
    risk.pure_checks("buy", "AA3", 1, 1.0, ACCOUNT, two_open, cfg)   # 3rd: ok

    three_open = {**two_open, "AA3": {"market_value": 1_000.0}}
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", "AA4", 1, 1.0, ACCOUNT, three_open, cfg)
    assert e.value.rail == "sector_concentration"


def test_a_SHORT_position_occupies_a_sector_slot_exactly_as_a_long_does():
    """PINNED, NOT FIXED — §54, 2026-08-05. Read the whole comment before
    changing this test, because the assertion below is deliberately asserting
    something arguable.

    `sector_open_count` is `sum(1 for s in positions if ...)` (`risk.py:1150`).
    Sign never enters it, so a SHORT of AA1 fills a slot in Alpha exactly as a
    long does. Consequence, now that `xsmom` has a short leg and `reclaim`
    shares 23 of its 30 names: **the short leg starves reclaim's sector
    budget**, through a rail nobody names when discussing the interaction —
    everyone points at `direction_conflict`, which
    `test_reclaim_short_collision.py` shows cannot even fire through the
    ensemble.

    IS IT RIGHT? The rail's own message says "co-moving names are one bet",
    which is direction reasoning the code does not implement: a short and a long
    in the same sector are partly OFFSETTING, not one bet. So there is a real
    argument that this should count same-direction positions only.

    It is pinned rather than changed because changing a shipped rail needs its
    own registration, and changing it after §54 has run would void those
    numbers. What this test buys is that the behaviour is now a DECISION with a
    date on it rather than an artifact of `for s in positions`.

    The all-long twin is directly below, differing in exactly the sign.
    """
    cfg = _sector_cfg(3)
    with_a_short = {"AA1": {"market_value": 1_000.0},
                    "AA2": {"market_value": -1_000.0},   # SHORT
                    "AA3": {"market_value": 1_000.0}}
    assert risk.sector_open_count(cfg, "AA4", with_a_short) == 3
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", "AA4", 1, 1.0, ACCOUNT, with_a_short, cfg)
    assert e.value.rail == "sector_concentration"

    all_long = {s: {"market_value": 1_000.0} for s in ("AA1", "AA2", "AA3")}
    with pytest.raises(risk.RiskRejection) as twin:
        risk.pure_checks("buy", "AA4", 1, 1.0, ACCOUNT, all_long, cfg)
    assert twin.value.rail == e.value.rail

    # And the pair that shows the cap is a COUNT, not a sign test: drop to two
    # positions, one of them short, and the third entry is permitted.
    two = {"AA1": {"market_value": -1_000.0}, "AA2": {"market_value": 1_000.0}}
    risk.pure_checks("buy", "AA3", 1, 1.0, ACCOUNT, two, cfg)


def test_a_SHORT_entry_is_also_subject_to_the_sector_cap():
    """The other half: the cap applies to what a short OPENS, not only to what
    it occupies. `sector_concentration` is gated on ENTRY_ACTIONS, which is
    `("buy", "short")` — so a short arriving at a full sector is refused just as
    a buy is. Without this, adding a direction to the ensemble would have
    quietly exempted it from the cap, which is the precise shape of the bug
    ENTRY_ACTIONS was introduced to prevent."""
    cfg = _sector_cfg(3)
    full = {s: {"market_value": 1_000.0} for s in ("AA1", "AA2", "AA3")}
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("short", "AA4", 1, 1.0, ACCOUNT, full, cfg)
    assert e.value.rail == "sector_concentration"
    # Twin: two open, so the short is permitted. "Refuses every short" would
    # satisfy the half above on its own.
    two = {s: {"market_value": 1_000.0} for s in ("AA1", "AA2")}
    risk.pure_checks("short", "AA4", 1, 1.0, ACCOUNT, two, cfg)


def test_the_cap_is_per_sector_not_global():
    """Three names in Alpha must not constrain a first name in Beta — otherwise
    the rail is just a smaller max_open_positions wearing a sector label."""
    cfg = _sector_cfg(3)
    positions = {"AA1": {}, "AA2": {}, "AA3": {}}
    risk.pure_checks("buy", "BB1", 1, 1.0, ACCOUNT, positions, cfg)


def test_the_cap_never_blocks_an_exit():
    """A full sector must still be closable. A rail that refuses to REDUCE risk
    is the inversion this codebase refuses everywhere."""
    cfg = _sector_cfg(3)
    positions = {"AA1": {"market_value": 1_000.0},
                 "AA2": {"market_value": 1_000.0},
                 "AA3": {"market_value": 1_000.0}}
    risk.pure_checks("sell", "AA1", 1, 1.0, ACCOUNT, positions, cfg)


def test_an_unmapped_symbol_is_refused_not_waved_through():
    """INVERTED 2026-11-04 (round-two plan, B4). This test was
    `test_the_cap_is_inert_for_unmapped_symbols`: when it was written the core
    universe carried no sector map, and an unmapped name passed the cap
    uncounted — the property that let the rail ship without re-gating the
    core strategies. Since 2026-11-02 every core name is mapped, and an entry
    the cap cannot count is now REFUSED with its own rail, because "unmapped"
    and "checked and under the cap" looked identical from outside (the
    October AVGO/QQQ/XLK cluster). tests/test_sector_coverage.py owns the
    rest of it."""
    cfg = _sector_cfg(1)
    positions = {"CORE1": {"market_value": 1_000.0},
                 "CORE2": {"market_value": 1_000.0}}
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", "CORE3", 1, 1.0, ACCOUNT, positions, cfg)
    assert e.value.rail == "sector_unmapped"


def test_disabling_the_cap_restores_the_prior_behaviour():
    cfg = _cfg(sector_concentration={"enabled": False, "max_per_sector": 1})
    positions = {"AA1": {}, "AA2": {}, "AA3": {}}
    risk.pure_checks("buy", "AA4", 1, 1.0, ACCOUNT, positions, cfg)


# ====================================================== direction_conflict rail

def test_buying_a_name_held_short_is_refused_and_a_flat_name_is_not():
    cfg = _cfg()
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", "AA1", 1, 1.0, ACCOUNT,
                         {"AA1": {"market_value": -5_000.0}}, cfg)
    assert e.value.rail == "direction_conflict"
    risk.pure_checks("buy", "AA1", 1, 1.0, ACCOUNT, {}, cfg)


def test_shorting_a_name_held_long_is_refused_and_a_flat_name_is_not():
    cfg = _cfg()
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("short", "AA1", 1, 1.0, ACCOUNT,
                         {"AA1": {"market_value": 5_000.0}}, cfg)
    assert e.value.rail == "direction_conflict"
    risk.pure_checks("short", "AA1", 1, 1.0, ACCOUNT, {}, cfg)


def test_adding_in_the_SAME_direction_is_still_allowed():
    """The rail must catch OPPOSITE directions only. Blocking an add would make
    it a duplicate of max_position_pct, and a worse one."""
    cfg = _cfg()
    risk.pure_checks("buy", "AA1", 1, 1.0, ACCOUNT,
                     {"AA1": {"market_value": 5_000.0}}, cfg)
    risk.pure_checks("short", "AA1", 1, 1.0, ACCOUNT,
                     {"AA1": {"market_value": -5_000.0}}, cfg)


def test_exits_are_never_direction_conflicts():
    """`sell` closes a long and `cover` closes a short; both are EXITS and are
    governed by the desync guards, not by this rail."""
    cfg = _cfg()
    risk.pure_checks("sell", "AA1", 1, 1.0, ACCOUNT,
                     {"AA1": {"market_value": 5_000.0}}, cfg)
    risk.pure_checks("cover", "AA1", 1, 1.0, ACCOUNT,
                     {"AA1": {"market_value": -5_000.0}}, cfg)


# ================================================================ sector ranking

def _flat_then(sym_level):
    """Bars whose last close sits `sym_level`% away from a flat SMA."""
    base = [100.0] * 20
    return _bars(base + [100.0 * (1 + sym_level / 100.0)])


def test_sectors_rank_by_median_depth_and_only_the_bottom_N_are_laggards():
    all_bars = {}
    for sym in SECTORS["Alpha"]:
        all_bars[sym] = _flat_then(-30)     # deepest
    for sym in SECTORS["Beta"]:
        all_bars[sym] = _flat_then(-20)
    for sym in SECTORS["Gamma"]:
        all_bars[sym] = _flat_then(-10)
    for sym in SECTORS["Delta"]:
        all_bars[sym] = _flat_then(+10)     # shallowest

    ctx = reclaim.prepare(all_bars, PARAMS, {"sectors": SECTORS})
    assert [s for s, _ in ctx["ranked"]] == ["Alpha", "Beta", "Gamma", "Delta"]
    # laggard_sector_count = 2: the boundary pair is 2nd vs 3rd.
    assert ctx["laggards"] == {"Alpha", "Beta"}
    assert "Gamma" not in ctx["laggards"]


def test_a_sector_with_too_few_usable_names_is_not_ranked_at_all():
    """Ranking on what survives would let a data outage manufacture a laggard:
    nine names drop out, the three left happen to be weak, and the strategy
    buys into a sector nothing measured."""
    all_bars = {s: _flat_then(-30) for s in SECTORS["Alpha"][:2]}   # 2 < 3
    for sym in SECTORS["Beta"]:
        all_bars[sym] = _flat_then(-5)
    ctx = reclaim.prepare(all_bars, PARAMS, {"sectors": SECTORS})
    assert "Alpha" not in ctx["scores"]
    assert "Beta" in ctx["scores"]


# =========================================================== the entry trigger

def _dive_then_pop():
    """Below trend, based ABOVE its short SMA, but that SMA is FALLING.

    Isolates the slope half of the base test: `close > base SMA` passes and
    `base SMA rising` fails, so only the slope check can block. The obvious
    fixture — a flat tail — fails the `close > base SMA` check first and lets a
    broken slope check survive, which is exactly what mutation testing caught.
    """
    return _bars([100.0] * 30 + [70.0] * 8 + [60.0, 60.0, 72.0] + [120.0])


def _reclaim_series(days_below=8, cross=True, rising_base=True):
    """A name that sits below its own SMA, bases, then crosses back above.

    Built so each knob moves exactly one entry condition.
    """
    head = [100.0] * 30                 # establishes the trend SMA level
    dip = [70.0] * days_below           # below the trend SMA for N bars
    if rising_base:
        tail = [71.0, 73.0, 75.0]       # short SMA turns up, still below trend
    else:
        tail = [70.0, 70.0, 70.0]       # flat: no base
    last = 120.0 if cross else 72.0     # decisively above / still below
    return _bars(head + dip + tail + [last])


def _ctx(laggards=("Alpha",)):
    return {"laggards": set(laggards),
            "scores": {"Alpha": -12.0, "Beta": -1.0},
            "sectors": {s: sec for sec, syms in SECTORS.items() for s in syms},
            "ranked": [("Alpha", -12.0), ("Beta", -1.0)]}


def test_a_full_setup_buys():
    sig = reclaim.generate("AA1", _reclaim_series(), PARAMS, holding=False,
                           cross_section=_ctx())
    assert sig.action == "buy", sig.reason


def test_no_buy_without_the_reclaim_itself():
    """Same series, still below the line on the final bar. This is the pair for
    'the cross happens on THIS bar'."""
    sig = reclaim.generate("AA1", _reclaim_series(cross=False), PARAMS,
                           holding=False, cross_section=_ctx())
    assert sig.action == "hold"


def test_no_buy_without_a_base():
    """A flat tail: price never gets above its own short SMA, so the decline
    has not stalled at all."""
    sig = reclaim.generate("AA1", _reclaim_series(rising_base=False), PARAMS,
                           holding=False, cross_section=_ctx())
    assert sig.action == "hold"
    assert "still falling" in sig.reason


def test_no_buy_when_the_base_sma_is_still_falling():
    """The slope half, isolated. Price IS above its short SMA — it dived and
    popped — but the SMA is lower than it was, so no base has formed. A falling
    knife crosses its trend average too, on the way down through it.

    Written because a mutation that disabled the slope check SURVIVED against
    the flat-tail fixture above: that one is stopped by the `close > base SMA`
    check first, so it never exercised the slope comparison at all.
    """
    sig = reclaim.generate("AA1", _dive_then_pop(), PARAMS, holding=False,
                           cross_section=_ctx())
    assert sig.action == "hold"
    assert "not rising" in sig.reason, sig.reason


def test_min_days_below_boundary_pair():
    """ONE series, two params: it is below trend for exactly 11 consecutive
    bars, so a requirement of 11 buys and 12 holds.

    Varying the series instead (a shorter dip) let a disabled `min_days_below`
    check SURVIVE mutation — the short-dip fixture is blocked by the base slope
    test first, so it never proved anything about this gate.
    """
    bars = _reclaim_series(days_below=8)      # 11 consecutive bars below trend
    ok = reclaim.generate("AA1", bars, {**PARAMS, "min_days_below": 11},
                          holding=False, cross_section=_ctx())
    assert ok.action == "buy", ok.reason
    short_of_it = reclaim.generate("AA1", bars,
                                   {**PARAMS, "min_days_below": 12},
                                   holding=False, cross_section=_ctx())
    assert short_of_it.action == "hold"
    assert "out of favour" in short_of_it.reason, short_of_it.reason


def test_a_non_laggard_sector_never_buys():
    """The identical setup, in a sector that is not among the laggards."""
    sig = reclaim.generate("AA1", _reclaim_series(), PARAMS, holding=False,
                           cross_section=_ctx(laggards=("Beta",)))
    assert sig.action == "hold"
    assert "beaten-down" in sig.reason


def test_an_unmapped_symbol_holds_rather_than_raising():
    sig = reclaim.generate("NOPE", _reclaim_series(), PARAMS, holding=False,
                           cross_section=_ctx())
    assert sig.action == "hold"
    assert "sector map" in sig.reason


# ==================================================================== the exit

def test_the_exit_fires_only_once_price_breaks_the_buffer():
    """Boundary pair on `exit_buffer_pct`: just inside holds, just outside
    sells. Re-crossing the line by a cent is noise, and the bracket stop already
    handles a real break."""
    inside = _bars([100.0] * 40 + [98.0])      # -2% vs a ~100 SMA, buffer 3%
    outside = _bars([100.0] * 40 + [95.0])     # -5%
    hold = reclaim.generate("AA1", inside, PARAMS, holding=True,
                            cross_section=_ctx())
    assert hold.action == "hold"
    sell = reclaim.generate("AA1", outside, PARAMS, holding=True,
                            cross_section=_ctx())
    assert sell.action == "sell"


def test_the_exit_does_not_need_the_cross_section():
    """Ownership routes an exit to this strategy even when it is disabled and
    even when no cross-section was prepared. Requiring one would strand the
    position."""
    outside = _bars([100.0] * 40 + [95.0])
    sig = reclaim.generate("AA1", outside, PARAMS, holding=True,
                           cross_section=None)
    assert sig.action == "sell"


def test_max_hold_days_releases_a_stalled_position():
    params = {**PARAMS, "max_hold_days": 2}
    bars = _bars([100.0] * 40 + [101.0])
    entry = "2026-01-01T00:00:00+00:00"
    sig = reclaim.generate("AA1", bars, params, holding=True,
                           cross_section=_ctx(), entry_ts=entry)
    assert sig.action == "sell" and "max" in sig.reason


def test_reclaim_declares_it_needs_the_entry_timestamp():
    """max_hold_days is measured from entry_ts, and the dispatch only passes it
    to modules that declare NEEDS_ENTRY_TS. Without this flag the rule would
    silently never fire."""
    assert reclaim.NEEDS_ENTRY_TS is True


# ================================================================== the universe

def test_reclaim_trades_the_sector_universe_and_others_trade_core():
    cfg = {"symbols": ["CORE1", "CORE2"], "sectors": SECTORS,
           "strategies": {"reclaim": {"enabled": True, "universe": "sectors"},
                          "meanrev": {"enabled": True}}}
    assert strategies.universe_for(cfg, "reclaim") == {
        s for syms in SECTORS.values() for s in syms}
    assert strategies.universe_for(cfg, "meanrev") == {"CORE1", "CORE2"}
    assert strategies.in_universe(cfg, "reclaim", "AA1")
    assert not strategies.in_universe(cfg, "reclaim", "CORE1")
    assert not strategies.in_universe(cfg, "meanrev", "AA1")


def test_an_unrecognised_universe_key_trades_nothing():
    """Fail-safe polarity: a typo must make a strategy trade NOTHING, not fall
    back to a universe it was never gated on. Preflight refuses this config
    outright; this is the runtime backstop."""
    cfg = {"symbols": ["CORE1"], "sectors": SECTORS,
           "strategies": {"reclaim": {"enabled": True, "universe": "sectorz"}}}
    assert strategies.universe_for(cfg, "reclaim") == set()


def test_the_shipped_config_keeps_reclaim_disabled_and_off_the_core_universe():
    import yaml
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    r = cfg["strategies"]["reclaim"]
    assert r["enabled"] is False
    assert r["universe"] == "sectors"
    # The core universe must be untouched by this work — that is the whole
    # reason per-strategy universes exist.
    assert len(cfg["symbols"]) == 38
    core = set(cfg["symbols"])
    sector_syms = {s for syms in cfg["sectors"].values() for s in syms}
    assert not (core & sector_syms) - core   # sanity: overlap is a subset of core


# ================================================================== preflight
#
# Every malformation below is SILENT at runtime — it does not raise, it quietly
# changes which names a strategy may buy or how many the cap allows. Each is
# paired with the shipped config passing, so a check that rejected everything
# would not satisfy these.

def _shipped():
    import yaml
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def _sector_fails(cfg):
    import preflight
    return [f for f in preflight.run(cfg)
            if "sector" in f or "universe" in f]


def test_the_shipped_sector_config_passes_preflight():
    assert _sector_fails(_shipped()) == []


def test_preflight_refuses_a_symbol_in_two_sectors():
    """Ambiguous membership makes sector_open_count answer differently
    depending on which lookup wins, so the cap becomes evadable."""
    import copy
    cfg = copy.deepcopy(_shipped())
    cfg["sectors"]["Energy"].append("NVDA")      # already in Technology
    assert any("two sectors" in f for f in _sector_fails(cfg))


def test_preflight_refuses_an_unknown_universe_key():
    """universe_for() empties a universe on a typo rather than widening it, so
    the strategy stops trading with nothing in the logs to explain why."""
    import copy
    cfg = copy.deepcopy(_shipped())
    cfg["strategies"]["reclaim"]["universe"] = "sectorz"
    assert any("not a known universe" in f for f in _sector_fails(cfg))


def test_preflight_refuses_a_sectors_universe_with_no_sector_map():
    import copy
    cfg = copy.deepcopy(_shipped())
    del cfg["sectors"]
    assert any("no sectors" in f for f in _sector_fails(cfg))


def test_preflight_refuses_an_empty_sector():
    import copy
    cfg = copy.deepcopy(_shipped())
    cfg["sectors"]["Energy"] = []
    assert any("non-empty" in f for f in _sector_fails(cfg))


def test_preflight_refuses_a_concentration_cap_below_one():
    """0 would refuse EVERY entry in a mapped sector — an outage wearing a
    rail's name. `True` is rejected explicitly because bool is an int and would
    otherwise sail through as a cap of 1."""
    import copy
    for bad in (0, -1, True, None, 2.5):
        cfg = copy.deepcopy(_shipped())
        cfg["risk"]["sector_concentration"]["max_per_sector"] = bad
        assert any("max_per_sector" in f for f in _sector_fails(cfg)), bad
    # and the paired half: a legitimate cap passes
    cfg = copy.deepcopy(_shipped())
    cfg["risk"]["sector_concentration"]["max_per_sector"] = 3
    assert _sector_fails(cfg) == []


def test_no_symbol_appears_in_two_sectors():
    """A symbol in two sectors would make sector_open_count ambiguous and let
    the concentration cap be evaded by whichever lookup won."""
    import yaml
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    seen = {}
    for sector, syms in cfg["sectors"].items():
        for s in syms:
            assert s not in seen, f"{s} is in both {seen.get(s)} and {sector}"
            seen[s] = sector
