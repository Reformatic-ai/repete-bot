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
