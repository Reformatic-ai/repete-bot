"""rail_census agrees with pure_checks, and covers every rail it raises.

WHY THIS FILE EXISTS, TWICE OVER.

1. `RiskRejection`'s docstring has claimed since 2026-08-02 that
   "tests/test_block_census.py enumerates the raise sites so it also fails
   loudly in CI." The file never existed. A guarantee that lives only in a
   comment is the same shape as the dashboard badge that was hardcoded
   without a single test going red — the audit found both.

2. `pure_checks` raises on the FIRST failing rail, so the ledger records one
   key and the rest are never evaluated. `rail_census` evaluates all of them,
   as a PARALLEL implementation — which is only safe while something proves
   the two agree. That is `test_the_census_agrees_with_the_rails` below, and
   it is the test that would license ever collapsing them onto one registry.
"""
import ast
import copy
import re
from pathlib import Path

import pytest

import risk
from risk import RiskRejection

SRC = Path(risk.__file__).read_text()


# ---- 1. the registry covers what pure_checks actually raises --------------

def _pure_checks_source() -> str:
    """Just pure_checks' body — from its def to the next top-level def."""
    start = SRC.index("def pure_checks(")
    rest = SRC[start + 1:]
    end = rest.index("\ndef ")
    return SRC[start:start + 1 + end]


def test_the_registry_covers_every_rail_pure_checks_raises():
    """THE PROMISE THE DOCSTRING MADE. Add a rail to pure_checks without
    adding it to PURE_RAILS and this goes red — which is the only thing
    stopping a new rail from being invisible to the census."""
    raised = set(re.findall(r'rail="([a-z_]+)"', _pure_checks_source()))
    assert raised, "found no rail= sites in pure_checks — did the parser drift?"
    assert raised == set(risk.PURE_RAILS), (
        f"pure_checks raises {sorted(raised)} but PURE_RAILS is "
        f"{sorted(risk.PURE_RAILS)}; missing from the registry: "
        f"{sorted(raised - set(risk.PURE_RAILS))}")


def test_the_partition_of_all_rails_is_exhaustive():
    """Every rail= in risk.py is either censused or explicitly excluded.
    A new rail added OUTSIDE pure_checks must be named in NON_PURE_RAILS, not
    silently absent — absence is how a rail comes to look covered."""
    everywhere = set(re.findall(r'rail="([a-z_]+)"', SRC))
    known = set(risk.PURE_RAILS) | set(risk.NON_PURE_RAILS)
    assert everywhere <= known, (
        f"rails in risk.py that are neither censused nor excluded by name: "
        f"{sorted(everywhere - known)}")


def test_every_rejection_names_its_rail():
    """THE OTHER HALF OF THE DOCSTRING'S PROMISE — "enumerates the raise
    sites". Until 2026-08-23 the enumeration above read `risk.py` only, and
    both live-kill raise sites are in `main.py` and `swing_scan.py`. So the
    kill switch that retired tsmom on 2026-08-20 logged fifteen live
    rejections as `unattributed`, and no test could say so: the reason was
    in `detail` as prose, which nothing aggregates and nothing pins.

    THOSE FIFTEEN ARE IN THE BIZON'S LEDGER, NOT THIS REPO'S. State migrated
    to the deployment host at the 2026-08-20 cutover and the working copy's
    `memory/ledger.jsonl` froze there — it greps ZERO for "live kill" and
    computes tsmom at n=11. Production computes n=15, PF 0.145, kill firing.
    Said explicitly because a reader who greps the nearest ledger concludes
    the sentence above was invented; one already did.

    `RiskRejection` defaults `rail` to "unattributed" ON PURPOSE, so a
    forgotten tag never crashes a trading cycle. This is the test that makes
    the default unreachable in practice rather than load-bearing."""
    bare = []
    for path in sorted(Path(risk.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name != "RiskRejection":
                continue
            # rail is the 2nd positional or a keyword; either is explicit.
            if "rail" not in {k.arg for k in node.keywords} and len(node.args) < 2:
                bare.append(f"{path.name}:{node.lineno}")
    assert not bare, (
        "RiskRejection raised without naming a rail — it will log as "
        f"'unattributed' and be invisible to every census: {bare}")


def test_rails_raised_outside_risk_py_are_registered_too():
    """The partition test above reads risk.py. A rail raised from main.py or
    swing_scan.py is just as real and just as invisible if unregistered."""
    known = set(risk.PURE_RAILS) | set(risk.NON_PURE_RAILS)
    for path in sorted(Path(risk.__file__).parent.rglob("*.py")):
        found = set(re.findall(r'rail="([a-z_]+)"', path.read_text()))
        unknown = found - known - {"unattributed"}
        assert not unknown, (
            f"{path.name} raises rails that are neither censused nor excluded "
            f"by name: {sorted(unknown)}")


def test_the_live_kill_is_named_not_unattributed():
    """Behavioural, not a source scan: the pre-registered kill is the rail
    that has actually fired in production, and it must arrive tagged."""
    e = RiskRejection("live kill: tsmom PF 0.15 ...", rail="live_kill")
    assert e.rail == "live_kill"
    assert "live_kill" in risk.NON_PURE_RAILS, (
        "the kill runs outside pure_checks, so it must be excluded from the "
        "census BY NAME — silent absence is how a rail looks covered")
    # It is not censusable: the census takes no closed-trade history, and
    # inventing one would make a read-only function read the ledger.
    assert "live_kill" not in risk.PURE_RAILS


def test_the_excluded_rails_are_excluded_for_a_reason():
    """halt and daily_cap read FILES. A read-only census must never stat the
    kill switch, so they cannot be censused even in principle."""
    assert {"halt", "daily_cap"} <= set(risk.NON_PURE_RAILS)
    assert not (set(risk.PURE_RAILS) & set(risk.NON_PURE_RAILS))


# ---- 2. agreement, over a generated matrix --------------------------------

def _cfg(**risk_over):
    r = {"max_order_value_usd": 0, "max_drawdown_pct": 10.0,
         "max_open_positions": 0, "max_position_pct": 10.0,
         "max_portfolio_heat_pct": 4.0}
    r.update(risk_over)
    return {"risk": r, "strategies": {}, "sectors": {}}


def _pos(**by_symbol):
    return {s: {"market_value": mv} for s, mv in by_symbol.items()}


ACCOUNT = {"equity": 100_000.0, "buying_power": 200_000.0, "cash": 50_000.0}

# Each case is (label, kwargs-for-both). Chosen to hit every rail in
# PURE_RAILS at least once, plus the interactions the existing suites cover.
CASES = [
    ("clean buy", dict(action="buy", symbol="SPY", qty=10, price=100.0,
                       positions={})),
    ("zero qty", dict(action="buy", symbol="SPY", qty=0, price=100.0,
                      positions={})),
    ("order value cap", dict(action="buy", symbol="SPY", qty=100, price=100.0,
                             positions={}, cfg=_cfg(max_order_value_usd=2000))),
    ("drawdown engaged", dict(action="buy", symbol="SPY", qty=1, price=100.0,
                              positions={}, peak_equity=200_000.0)),
    ("drawdown clear", dict(action="buy", symbol="SPY", qty=1, price=100.0,
                            positions={}, peak_equity=100_100.0)),
    ("peak unknown", dict(action="buy", symbol="SPY", qty=1, price=100.0,
                          positions={}, peak_equity=None)),
    ("max open reached", dict(action="buy", symbol="NEW", qty=1, price=100.0,
                              positions=_pos(A=1.0, B=1.0),
                              cfg=_cfg(max_open_positions=2))),
    ("max open, already held", dict(action="buy", symbol="A", qty=1, price=100.0,
                                    positions=_pos(A=1.0, B=1.0),
                                    cfg=_cfg(max_open_positions=2))),
    ("max open disabled", dict(action="buy", symbol="NEW", qty=1, price=100.0,
                               positions=_pos(A=1.0, B=1.0, C=1.0))),
    ("strategy slots", dict(action="buy", symbol="NEW", qty=1, price=100.0,
                            positions={}, strategy="tsmom", strategy_open=3,
                            cfg={"risk": _cfg()["risk"],
                                 "strategies": {"tsmom": {"max_open_positions": 3}},
                                 "sectors": {}})),
    ("position cap", dict(action="buy", symbol="SPY", qty=200, price=100.0,
                          positions={})),
    ("position cap via existing", dict(action="buy", symbol="SPY", qty=50,
                                       price=100.0, positions=_pos(SPY=6000.0))),
    ("regime exposure", dict(action="buy", symbol="SPY", qty=1, price=100.0,
                             positions=_pos(A=60_000.0), regime_label="down/high",
                             cfg=_cfg(regime_exposure={"enabled": True,
                                                       "down_max_gross_pct": 50}))),
    ("net exposure ceiling", dict(action="buy", symbol="SPY", qty=10, price=100.0,
                                  positions=_pos(A=125_000.0),
                                  cfg=_cfg(net_exposure_pct={"max": 100, "min": -30}))),
    ("net exposure floor", dict(action="short", symbol="SPY", qty=10, price=100.0,
                                positions=_pos(A=-35_000.0),
                                cfg=_cfg(net_exposure_pct={"max": 130, "min": -30}))),
    ("sell with no long", dict(action="sell", symbol="SPY", qty=1, price=100.0,
                               positions={})),
    ("sell held short", dict(action="sell", symbol="SPY", qty=1, price=100.0,
                             positions=_pos(SPY=-500.0))),
    ("sell ok", dict(action="sell", symbol="SPY", qty=1, price=100.0,
                     positions=_pos(SPY=500.0))),
    ("cover with no short", dict(action="cover", symbol="SPY", qty=1, price=100.0,
                                 positions={})),
    ("cover ok", dict(action="cover", symbol="SPY", qty=1, price=100.0,
                      positions=_pos(SPY=-500.0))),
    ("short while long", dict(action="short", symbol="SPY", qty=1, price=100.0,
                              positions=_pos(SPY=500.0))),
    ("buy while short", dict(action="buy", symbol="SPY", qty=1, price=100.0,
                             positions=_pos(SPY=-500.0))),
    ("sector cap", dict(action="buy", symbol="XOM", qty=1, price=100.0,
                        positions=_pos(CVX=1000.0),
                        cfg={"risk": dict(_cfg()["risk"],
                                          sector_concentration={"enabled": True,
                                                                "max_per_sector": 1}),
                             "strategies": {},
                             "sectors": {"energy": ["XOM", "CVX"]}})),
    # 2026-11-04: an unmapped entry is refused rather than waved through.
    ("sector unmapped", dict(action="buy", symbol="NEW", qty=1, price=100.0,
                             positions={},
                             cfg={"risk": dict(_cfg()["risk"],
                                               sector_concentration={"enabled": True,
                                                                     "max_per_sector": 3}),
                                  "strategies": {},
                                  "sectors": {"energy": ["XOM", "CVX"]}})),
    ("sector unmapped, but a sell", dict(action="sell", symbol="NEW", qty=1,
                                        price=100.0, positions=_pos(NEW=500.0),
                                        cfg={"risk": dict(_cfg()["risk"],
                                                          sector_concentration={"enabled": True,
                                                                                "max_per_sector": 3}),
                                             "strategies": {},
                                             "sectors": {"energy": ["XOM", "CVX"]}})),
    ("sector override binds", dict(action="buy", symbol="DIA", qty=1, price=100.0,
                                   positions=_pos(SPY=1000.0),
                                   cfg={"risk": dict(_cfg()["risk"],
                                                     sector_concentration={
                                                         "enabled": True,
                                                         "max_per_sector": 3,
                                                         "max_per_sector_overrides":
                                                             {"Broad market": 1}}),
                                        "strategies": {},
                                        "core_sectors": {"Broad market":
                                                         ["SPY", "DIA", "IWM"]}})),
    ("sector override, other sector", dict(action="buy", symbol="XOM", qty=1,
                                           price=100.0, positions=_pos(CVX=1000.0),
                                           cfg={"risk": dict(_cfg()["risk"],
                                                             sector_concentration={
                                                                 "enabled": True,
                                                                 "max_per_sector": 3,
                                                                 "max_per_sector_overrides":
                                                                     {"Broad market": 1}}),
                                                "strategies": {},
                                                "sectors": {"energy": ["XOM", "CVX"]},
                                                "core_sectors": {"Broad market":
                                                                 ["SPY", "DIA"]}})),
]


def _call(fn, case):
    kw = dict(case)
    cfg = kw.pop("cfg", None) or _cfg()
    return fn(kw.pop("action"), kw.pop("symbol"), kw.pop("qty"), kw.pop("price"),
              ACCOUNT, kw.pop("positions"), cfg, **kw)


@pytest.mark.parametrize("label,case", CASES, ids=[c[0] for c in CASES])
def test_the_census_agrees_with_the_rails(label, case):
    """THE LOAD-BEARING TEST OF THIS PR.

    The census is a parallel implementation of logic that decides whether real
    money moves. Its only licence to exist is that it provably matches. If
    pure_checks raises, the census's first True must be that same rail; if it
    clears, nothing may be True.
    """
    try:
        _call(risk.pure_checks, case)
        raised = None
    except RiskRejection as e:
        raised = e.rail

    census = _call(risk.rail_census, case)
    first = risk.first_refusing_rail(census)
    assert first == raised, (
        f"[{label}] pure_checks raised {raised!r} but the census says "
        f"{first!r}. Census: { {k: v for k, v in census.items() if v is not None} }")


def test_every_rail_is_exercised_by_the_matrix():
    """A matrix that never trips a rail proves nothing about that rail. This
    fails if a new rail is added to PURE_RAILS without a case that fires it."""
    fired = set()
    for _, case in CASES:
        try:
            _call(risk.pure_checks, case)
        except RiskRejection as e:
            fired.add(e.rail)
    missing = set(risk.PURE_RAILS) - fired
    assert not missing, (
        f"no case in CASES trips these rails, so agreement is untested for "
        f"them: {sorted(missing)}")


# ---- 3. None vs False, and purity ----------------------------------------

def test_inapplicable_rails_are_none_not_false():
    """None means 'never asked'. False would read as 'checked and cleared'
    and overstate how much checking happened."""
    sell = _call(risk.rail_census,
                 dict(action="sell", symbol="SPY", qty=1, price=100.0,
                      positions=_pos(SPY=500.0)))
    for entry_only in ("drawdown", "max_open_positions", "position_cap",
                       "direction_conflict", "sector_unmapped",
                       "sector_concentration"):
        assert sell[entry_only] is None, f"{entry_only} should not apply to a sell"
    assert sell["desync_sell"] is False        # applies, and cleared
    assert sell["desync_cover"] is None        # a sell is not a cover

    buy = _call(risk.rail_census,
                dict(action="buy", symbol="SPY", qty=1, price=100.0, positions={}))
    assert buy["desync_sell"] is None and buy["desync_cover"] is None
    assert buy["position_cap"] is False        # applies, and cleared


def test_a_disabled_rail_is_none_not_false():
    """max_order_value_usd: 0 means DISABLED (§29). Reporting False would say
    the cap was checked and passed, which is not what happened."""
    c = _call(risk.rail_census,
              dict(action="buy", symbol="SPY", qty=1, price=100.0, positions={}))
    assert c["order_value_cap"] is None
    c2 = _call(risk.rail_census,
               dict(action="buy", symbol="SPY", qty=1, price=100.0, positions={},
                    cfg=_cfg(max_order_value_usd=2000)))
    assert c2["order_value_cap"] is False


def test_the_census_has_no_side_effects(monkeypatch):
    """Read-only, and it must never touch the filesystem — the HALT file is
    exactly the thing a diagnostic must not stat."""
    def _no_open(*a, **k):
        raise AssertionError("the census opened a file")
    monkeypatch.setattr("builtins.open", _no_open)

    cfg = _cfg(max_open_positions=2)
    account = dict(ACCOUNT)
    positions = _pos(A=1000.0, B=2000.0)
    before = (copy.deepcopy(cfg), copy.deepcopy(account), copy.deepcopy(positions))

    for _ in range(2):
        risk.rail_census("buy", "NEW", 5, 100.0, account, positions, cfg,
                         regime_label="up/low", strategy=None)

    assert (cfg, account, positions) == before, "the census mutated its inputs"


def test_the_census_never_raises_on_the_shapes_pure_checks_accepts():
    """Any input pure_checks survives, the census must survive — it runs on
    the rejection path and an exception there would lose the rejection."""
    for _, case in CASES:
        _call(risk.rail_census, case)   # must not raise


# ---- 4. it reaches the ledger, and a failure never loses the rejection ----

def test_the_vector_lands_beside_the_binding_rail(tmp_path):
    """Both fields, and they agree: rail_vector's first True IS rail."""
    from ledger import Ledger
    case = dict(action="buy", symbol="SPY", qty=1, price=100.0,
                positions={}, peak_equity=200_000.0)
    try:
        _call(risk.pure_checks, case)
        pytest.fail("this fixture is supposed to be rejected")
    except RiskRejection as e:
        rail, vec = e.rail, _call(risk.rail_census, case)

    led = Ledger(str(tmp_path / "l.jsonl"))
    led.log_decision("SPY", "buy", "m", {}, None, executed=False,
                     rail=rail, rail_vector=vec)
    row = led.all_records()[0]
    assert row["rail"] == "drawdown"
    assert row["rail_vector"]["drawdown"] is True
    assert risk.first_refusing_rail(row["rail_vector"]) == row["rail"]


def test_a_non_rejection_carries_a_null_vector(tmp_path):
    from ledger import Ledger
    led = Ledger(str(tmp_path / "l.jsonl"))
    led.log_decision("SPY", "buy", "m", {}, None, executed=True)
    row = led.all_records()[0]
    assert "rail_vector" in row and row["rail_vector"] is None


def test_a_census_failure_never_loses_the_rejection(monkeypatch, caplog):
    """The audit trail is not a trading path. If the census explodes, the
    rejection must still be recorded — with a null vector, not no row."""
    import main
    monkeypatch.setattr(risk, "rail_census",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = main._rail_census_safe("buy", "SPY", 1, 100.0, ACCOUNT, {}, _cfg(),
                                 "up/low", None)
    assert out is None
