"""Every name the bot may enter has a sector, so the sector cap can count it.

`risk.sector_open_count` returns 0 for an unmapped symbol, by design. Until
2026-11-02, 15 of the 38 core names were unmapped (the `sectors:` map only held
the names that ranked into `reclaim`'s list), so the cap watched NVDA and AMD
but not AVGO, QQQ or XLK. On 2026-10-09..14 the book bought all five; all five
stopped out on 10-15/16 for -$1,527. Nothing failed, because "unmapped" and
"checked and under the cap" look the same from outside.

These tests run against the SHIPPED config, so adding a symbol to `symbols:`
(or enabling a strategy with its own universe) without giving it a sector
fails here instead of quietly opening the same hole again.
"""
import copy

import pytest
import yaml

import risk
import strategies
from strategies.base import sector_of

ACCOUNT = {"equity": 100_000.0}

#: The owner-approved assignment for the 15 names `sectors:` leaves out
#: (2026-11-02 plan, workstream A). Pinned so a later edit that moves one of
#: them is a visible test change, not a silent re-bucketing.
APPROVED = {
    "AVGO": "Technology", "CRM": "Technology", "QQQ": "Technology",
    "XLK": "Technology",
    "META": "Communication Services", "GOOGL": "Communication Services",
    "TSLA": "Consumer Cyclical",
    "V": "Financial Services", "XLF": "Financial Services",
    "ABBV": "Healthcare", "XLV": "Healthcare",
    "XLE": "Energy",
    "SPY": "Broad market", "DIA": "Broad market", "IWM": "Broad market",
}


def _shipped():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def _enterable(cfg):
    """Every symbol an ENABLED strategy may enter, plus the core list itself."""
    names = set(cfg["symbols"])
    for name, params in (cfg.get("strategies") or {}).items():
        if (params or {}).get("enabled"):
            names |= strategies.universe_for(cfg, name)
    return names


def test_every_symbol_the_bot_may_enter_has_a_sector():
    cfg = _shipped()
    unmapped = sorted(s for s in _enterable(cfg) if sector_of(cfg, s) is None)
    assert unmapped == [], (
        f"no sector for {unmapped}: risk.sector_open_count counts them as 0, "
        f"so the sector cap cannot see them. Add each to `core_sectors:` "
        f"(or `sectors:` if it belongs in reclaim's universe).")


def test_the_check_above_can_fail():
    """The twin: drop one name from the map and the coverage check catches it.
    A coverage test that passes on any config is the `ci.yml` 'check that
    cannot fail' this repo has been caught by before."""
    cfg = _shipped()
    cfg["core_sectors"]["Technology"].remove("AVGO")
    assert "AVGO" in _enterable(cfg)
    assert sector_of(cfg, "AVGO") is None


def test_the_fifteen_core_names_carry_the_approved_sectors():
    cfg = _shipped()
    assert {s: sector_of(cfg, s) for s in APPROVED} == APPROVED


def test_core_sector_names_reuse_the_sectors_names():
    """A fund must land in the SAME bucket as its constituents (XLK with NVDA),
    so a typo like "Tech" would split the count and halve the cap. Only
    "Broad market" is new."""
    cfg = _shipped()
    known = set(cfg["sectors"]) | {"Broad market"}
    assert set(cfg["core_sectors"]) <= known


def test_reclaims_universe_is_unchanged():
    """`core_sectors:` feeds the cap only. reclaim ranks `sectors:`, and none
    of the 15 (eight of them ETFs, which preflight bars from `sectors:`) may
    leak into it."""
    cfg = _shipped()
    assert not set(APPROVED) & strategies.sector_universe(cfg)
    assert not set(APPROVED) & strategies.universe_for(cfg, "reclaim")


def test_the_shipped_config_passes_the_sector_preflight():
    import preflight
    fails = [f for f in preflight.run(_shipped())
             if "sector" in f or "universe" in f]
    assert fails == []


@pytest.mark.parametrize("block,sector,symbol", [
    ("core_sectors", "Energy", "NVDA"),      # NVDA is in sectors: Technology
    ("sectors", "Energy", "AVGO"),           # AVGO is in core_sectors: Technology
    ("core_sectors", "Healthcare", "QQQ"),   # QQQ twice inside core_sectors
])
def test_preflight_refuses_a_symbol_in_two_sectors_across_both_maps(
        block, sector, symbol):
    import preflight
    cfg = copy.deepcopy(_shipped())
    cfg[block][sector].append(symbol)
    assert any("two sectors" in f for f in preflight.run(cfg))


def test_the_cap_now_refuses_a_fourth_tech_name_from_the_core_list():
    """The October shape: NVDA and AMD were already counted; AVGO, QQQ and XLK
    were not. With all five mapped, three open tech names refuse the fourth."""
    cfg = _shipped()
    three = {s: {"market_value": 5_000.0} for s in ("NVDA", "AMD", "AVGO")}
    assert risk.sector_open_count(cfg, "QQQ", three) == 3
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", "QQQ", 1, 1.0, ACCOUNT, three, cfg)
    assert e.value.rail == "sector_concentration"
    # Twin: two open, so the third is permitted (the cap is 3, not 2).
    two = {s: {"market_value": 5_000.0} for s in ("NVDA", "AVGO")}
    risk.pure_checks("buy", "XLK", 1, 1.0, ACCOUNT, two, cfg)


# ---- 2026-11-04 (round-two plan, B4): the two gaps the map left ------------
#
# 1. An UNMAPPED entry used to SKIP the count — `sector_open_count` returns 0
#    for it and `pure_checks` only counted `if sector is not None`, so the
#    entry passed looking checked. It is now refused, with its own rail.
# 2. "Broad market" (SPY, DIA, IWM) was mapped but could never bind: three
#    names under a global cap of 3. It now carries its own cap of 1 through
#    `max_per_sector_overrides`.

UNMAPPED = "PLTR"   # a plausible news nomination, in neither map


def test_the_unmapped_example_really_is_unmapped():
    cfg = _shipped()
    assert sector_of(cfg, UNMAPPED) is None
    assert UNMAPPED not in _enterable(cfg)


def test_an_unmapped_entry_is_refused_with_its_own_rail():
    cfg = _shipped()
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("buy", UNMAPPED, 1, 1.0, ACCOUNT, {}, cfg)
    assert e.value.rail == "sector_unmapped"
    assert UNMAPPED in str(e.value) and "core_sectors" in str(e.value)
    # Twin: a mapped name on the same empty book goes through.
    risk.pure_checks("buy", "NVDA", 1, 1.0, ACCOUNT, {}, cfg)


def test_a_short_entry_is_refused_too():
    """ENTRY_ACTIONS, not just "buy": a short opens a position the cap must
    count as much as a long does."""
    with pytest.raises(risk.RiskRejection) as e:
        risk.pure_checks("short", UNMAPPED, 1, 1.0, ACCOUNT, {}, _shipped())
    assert e.value.rail == "sector_unmapped"


def test_an_unmapped_position_can_still_be_closed():
    """Exits are never blocked — an unmapped name already held (a nomination
    bought before this rail existed) must still be sellable."""
    held = {UNMAPPED: {"market_value": 5_000.0}}
    risk.pure_checks("sell", UNMAPPED, 1, 1.0, ACCOUNT, held, _shipped())


def test_the_rail_is_off_when_the_cap_is_off():
    """`enabled: false` turns the whole sector rail off, unmapped check
    included — the refusal belongs to the cap, it is not a new universe
    filter."""
    cfg = copy.deepcopy(_shipped())
    cfg["risk"]["sector_concentration"]["enabled"] = False
    risk.pure_checks("buy", UNMAPPED, 1, 1.0, ACCOUNT, {}, cfg)


def test_the_unmapped_rail_is_registered_and_censused():
    """Registered in PURE_RAILS so the ledger's rail census records it, and
    ordered BEFORE the cap it protects, because an unmapped symbol has no
    sector for the cap to look up."""
    assert "sector_unmapped" in risk.PURE_RAILS
    assert (risk.PURE_RAILS.index("sector_unmapped")
            < risk.PURE_RAILS.index("sector_concentration"))
    c = risk.rail_census("buy", UNMAPPED, 1, 1.0, ACCOUNT, {}, _shipped())
    assert c["sector_unmapped"] is True
    assert c["sector_concentration"] is None      # never asked, not "cleared"
    assert risk.first_refusing_rail(c) == "sector_unmapped"
    mapped = risk.rail_census("buy", "NVDA", 1, 1.0, ACCOUNT, {}, _shipped())
    assert mapped["sector_unmapped"] is False


def test_the_simulator_refuses_it_through_the_same_function():
    """The simulator calls `risk.pure_checks` for every entry (divergence #1's
    closure), so it cannot disagree with live about an unmapped name. Pinned by
    source, because no snapshot entry can reach an unmapped core name today."""
    import inspect

    import backtest as bt
    assert "risk.pure_checks(" in inspect.getsource(bt)
    assert 'rail="sector_unmapped"' in inspect.getsource(risk.pure_checks)


def test_broad_market_is_capped_at_one():
    cfg = _shipped()
    assert risk.sector_cap(cfg["risk"]["sector_concentration"],
                           "Broad market") == 1
    spy = {"SPY": {"market_value": 5_000.0}}
    for second in ("DIA", "IWM"):
        with pytest.raises(risk.RiskRejection) as e:
            risk.pure_checks("buy", second, 1, 1.0, ACCOUNT, spy, cfg)
        assert e.value.rail == "sector_concentration"
        assert "Broad market" in str(e.value) and "max 1" in str(e.value)
    # Twin: the first broad fund on a book without one goes through...
    risk.pure_checks("buy", "DIA", 1, 1.0, ACCOUNT,
                     {"NVDA": {"market_value": 5_000.0}}, cfg)
    # ...and adding to SPY itself is max_position_pct's question, not this one.
    risk.pure_checks("buy", "SPY", 1, 1.0, ACCOUNT, spy, cfg)


def test_the_override_does_not_touch_other_sectors():
    """Technology still caps at the global 3: two open, the third is allowed,
    and a broad fund open does not count toward it."""
    cfg = _shipped()
    assert risk.sector_cap(cfg["risk"]["sector_concentration"],
                           "Technology") == 3
    book = {s: {"market_value": 5_000.0} for s in ("NVDA", "AMD", "SPY")}
    risk.pure_checks("buy", "AVGO", 1, 1.0, ACCOUNT, book, cfg)


def test_without_the_override_broad_market_falls_back_to_the_global_cap():
    """The negative control: remove the override and DIA beside SPY passes,
    which is the gap the override closes."""
    cfg = copy.deepcopy(_shipped())
    del cfg["risk"]["sector_concentration"]["max_per_sector_overrides"]
    risk.pure_checks("buy", "DIA", 1, 1.0, ACCOUNT,
                     {"SPY": {"market_value": 5_000.0}}, cfg)


@pytest.mark.parametrize("over,needle", [
    ({"Broad markt": 1}, "neither"),               # typo caps nothing
    ({"Broad market": 0}, "int >= 1"),
    ({"Broad market": True}, "int >= 1"),           # bool is an int in Python
    ({"Broad market": 4}, "only tighten"),          # above the global 3
    (["Broad market"], "mapping"),
])
def test_preflight_refuses_a_bad_override(over, needle):
    import preflight
    cfg = copy.deepcopy(_shipped())
    cfg["risk"]["sector_concentration"]["max_per_sector_overrides"] = over
    fails = [f for f in preflight.run(cfg) if "overrides" in f]
    assert any(needle in f for f in fails), fails


def test_preflight_accepts_the_shipped_override():
    import preflight
    assert [f for f in preflight.run(_shipped()) if "overrides" in f] == []
