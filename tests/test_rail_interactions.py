"""What happens when rails fire TOGETHER (2026-08-02).

Why this file exists
--------------------
`test_rails_are_live.py` proves each rail exists and sits at its threshold, one
rail at a time, by pairing an allowed call with a neighbouring blocked one. That
was the answer to the 2026-07-27 audit, where replacing the whole engine with
`return None` left 13 safety tests green.

It leaves a second gap. The rails are a strict first-match-wins `raise` sequence
across `pure_checks` and `pre_trade_checks`, so two things are only decidable
when more than one rail is breached at once:

  1. WHICH rail gets reported. `RiskRejection.rail` is the name an operator reads
     during an incident. Before this file, NO test anywhere asserted on `.rail`,
     so re-ordering two checks would silently change the diagnosis while every
     test stayed green.

  2. Whether an EXIT can be trapped. Entry rails are individually exempt for
     sells, but "each rail lets a sell through on its own" is not the same claim
     as "a sell gets out when the whole book is on fire". The second is the one
     that matters, and it is the one nothing tested.

Two live defects were found by asking those questions; the tests that drive them
are marked DEFECT below.

All tests here are offline and side-effect free, or run under `monkeypatch.chdir`
— `pre_trade_checks` ratchets `memory/.equity_highwater.json` and reads
`memory/.trade_counts.json`, both RELATIVE paths, so an unisolated test would
mutate the live deployment's state.
"""
import ast
import json
import os

import pytest

import risk


def _acct(equity=100_000):
    return {"equity": equity, "buying_power": equity, "last_equity": equity}


def _cfg(**over):
    """The shipped rail values, so these tests describe the real system.

    Deliberately mirrors `_cfg` in test_rails_are_live.py rather than importing
    it: these two files must be able to disagree if the shipped config moves,
    and a shared fixture would hide that.
    """
    r = {"risk_per_trade_pct": 8.0, "max_position_pct": 10.0,
         "max_order_value_usd": 0, "max_open_positions": 0,
         "max_trades_per_day": 15, "max_drawdown_pct": 10.0,
         "max_portfolio_heat_pct": 4.0, "min_holding_days": 2}
    r.update(over)
    return {"risk": r}


def _rail_of(**kw):
    """The rail key reported for this call, or None if it was allowed."""
    base = dict(action="buy", symbol="ZZZ", qty=10, price=100.0,
                account=_acct(), positions={}, cfg=_cfg())
    base.update(kw)
    try:
        risk.pure_checks(**base)
        return None
    except risk.RiskRejection as e:
        return e.rail


# Breach kits. Each is the MINIMUM change that trips exactly one rail, so a test
# can compose them and know precisely which rails are in play.
DEEP_DRAWDOWN = dict(peak_equity=200_000)            # 50% down vs a 10% cap
OVERSIZED = dict(qty=10_000)                         # $1M vs a 10%-of-equity cap
FULL_BOOK = dict(cfg=_cfg(max_open_positions=1),
                 positions={"OTHER": {"market_value": 1.0}})
DOWN_REGIME = dict(regime_label="down")


# ---------------------------------------------------------------- precedence

def test_every_rail_reports_a_named_key_never_unattributed():
    """`rail` defaults to 'unattributed'. A rejection that reaches an operator
    without a name is a rejection they cannot triage."""
    for kw in (dict(qty=0), DEEP_DRAWDOWN, OVERSIZED, FULL_BOOK,
               dict(cfg=_cfg(max_order_value_usd=100))):
        rail = _rail_of(**kw)
        assert rail is not None, f"expected a rejection for {kw}"
        assert rail != "unattributed", f"{kw} produced an unnamed rail"


@pytest.mark.parametrize("label,kw,expected", [
    # Each row breaches EVERY rail named to its right, and asserts which wins.
    ("zero qty beats all",
     {**DEEP_DRAWDOWN, **OVERSIZED, "qty": 0}, "zero_qty"),
    ("order value beats drawdown",
     {**DEEP_DRAWDOWN, "cfg": _cfg(max_order_value_usd=100)},
     "order_value_cap"),
    ("drawdown beats concentration",
     {**DEEP_DRAWDOWN, **OVERSIZED}, "drawdown"),
    ("drawdown beats a full book",
     {**DEEP_DRAWDOWN, **FULL_BOOK}, "drawdown"),
    ("drawdown beats down-regime",
     {**DEEP_DRAWDOWN, **OVERSIZED, **DOWN_REGIME}, "drawdown"),
    ("full book beats concentration",
     {**FULL_BOOK, "qty": 10_000}, "max_open_positions"),
])
def test_precedence_is_pinned_when_several_rails_breach_at_once(
        label, kw, expected):
    """Order of evaluation IS the diagnosis.

    These assertions are not claims that this order is the only defensible one.
    They are a tripwire: change the order deliberately and update this table;
    change it accidentally and this table says so. Without it, a refactor can
    swap which cause an operator is shown at 3am and nothing goes red.
    """
    assert _rail_of(**kw) == expected, label


# ------------------------------------------------- exits under an N-way breach

def test_a_sell_escapes_with_every_entry_rail_breached_simultaneously():
    """THE one that matters, stated as a conjunction.

    Individually-exempt rails do not compose by themselves: a single early
    `raise` that forgot its `action == "buy"` guard would trap the position, and
    every per-rail exit test would still pass because each tests its own rail in
    isolation.
    """
    cfg = _cfg(max_open_positions=1, regime_exposure={"enabled": True,
                                                      "down_max_gross_pct": 1})
    positions = {"ZZZ": {"market_value": 90_000.0},
                 "OTHER": {"market_value": 90_000.0}}
    # Breached at once: drawdown, max_open_positions, strategy_slots,
    # position_cap, regime_exposure.
    risk.pure_checks(
        "sell", "ZZZ", 10_000, 100.0, _acct(50_000), positions, cfg,
        regime_label="down", strategy="tsmom", strategy_open=99,
        peak_equity=500_000)


def test_the_same_call_as_a_buy_is_refused():
    """The contrast that makes the test above mean something. Without it, a
    neutered engine would pass the sell case trivially."""
    cfg = _cfg(max_open_positions=1, regime_exposure={"enabled": True,
                                                      "down_max_gross_pct": 1})
    positions = {"ZZZ": {"market_value": 90_000.0},
                 "OTHER": {"market_value": 90_000.0}}
    with pytest.raises(risk.RiskRejection):
        risk.pure_checks(
            "buy", "ZZZ", 10_000, 100.0, _acct(50_000), positions, cfg,
            regime_label="down", strategy="tsmom", strategy_open=99,
            peak_equity=500_000)


# ------------------------------------------------------ DEFECT: the daily cap
#
# `max_trades_per_day: 15` is live, `record_trade()` is called for buys AND
# sells (main.py, unconditional after the order), and the check carried no
# action guard. So a day that reaches 15 total trades refused the next EXIT and
# left the position open past its exit signal.
#
# The runaway-guard rationale in risk.py is entry-side by construction: it
# bounds "an API loop, a bad feed, or a signal bug that would otherwise place
# orders until buying power ran out". None of those is a reason to refuse a sell
# — refusing the sell is how a control enlarges risk through its own limit.

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate the relative-path state files from the live deployment."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("memory", exist_ok=True)
    return tmp_path


def _sell(cfg, **kw):
    return risk.pre_trade_checks(
        "sell", "ZZZ", 10, 100.0, _acct(), {"ZZZ": {"market_value": 1000.0}},
        cfg, entry_ts="2000-01-01T00:00:00+00:00", **kw)


def test_an_exit_is_not_refused_once_the_daily_cap_is_reached(sandbox):
    """DEFECT (fixed): the cap counts exits too, so a busy day could trap a
    position it had already decided to close."""
    with open(risk._TRADECOUNT_FILE, "w") as f:
        json.dump({str(risk.date.today()): 99}, f)
    _sell(_cfg(max_trades_per_day=15))          # must not raise


def test_an_exit_is_not_refused_when_the_trade_counter_is_corrupt(sandbox):
    """DEFECT (fixed), and the worse half.

    `_trades_today()` fails CLOSED on an unreadable file — correct for entries,
    and catastrophic for exits: a single corrupt JSON file would have blocked
    EVERY exit until a human noticed and repaired it.
    """
    with open(risk._TRADECOUNT_FILE, "w") as f:
        f.write("{not valid json")
    assert risk._trades_today() == risk._TRADECOUNT_UNKNOWN   # still fails closed
    _sell(_cfg(max_trades_per_day=15))          # but not for a sell


def test_the_daily_cap_still_refuses_an_ENTRY_at_the_limit(sandbox):
    """The guard on the guard. Exempting sells must not disable the runaway
    guard it was built to be — that would trade one defect for a worse one."""
    with open(risk._TRADECOUNT_FILE, "w") as f:
        json.dump({str(risk.date.today()): 99}, f)
    with pytest.raises(risk.RiskRejection) as ei:
        risk.pre_trade_checks("buy", "ZZZ", 10, 100.0, _acct(), {},
                              _cfg(max_trades_per_day=15))
    assert ei.value.rail == "daily_cap"


def test_a_corrupt_counter_still_refuses_an_ENTRY(sandbox):
    """Fail-closed is preserved on the side where it belongs."""
    with open(risk._TRADECOUNT_FILE, "w") as f:
        f.write("{not valid json")
    with pytest.raises(risk.RiskRejection) as ei:
        risk.pre_trade_checks("buy", "ZZZ", 10, 100.0, _acct(), {},
                              _cfg(max_trades_per_day=15))
    assert ei.value.rail == "daily_cap"


# ------------------------------------------- heat contamination (§41, live-adjacent)

def test_ONE_stopless_open_position_exhausts_the_whole_heat_budget(sandbox):
    """Shipped `risk_per_trade_pct: 8.0` exceeds `max_portfolio_heat_pct: 4.0`,
    and `portfolio_heat` charges a position with NO RECORDED STOP the full
    per-trade fraction. So one such position spends 200% of the budget and every
    later entry is refused no matter how tightly IT is stopped.

    Pinned, not fixed: both numbers are recorded owner decisions (§29) and no
    risk number moves on the strength of a test. The point is that the symptom
    is the §40 shape — the bot stops entering and never says why — so it should
    be a named rail in a test, not a mystery in production.
    """
    cfg = _cfg()
    stopless = {"AAA": {"qty": 1, "entry_price": 100.0, "order": {}}}
    heat = risk.portfolio_heat(stopless, cfg, 100_000)
    cap = 100_000 * cfg["risk"]["max_portfolio_heat_pct"] / 100
    assert heat > cap, "the inversion is gone — update this test and §41"

    with pytest.raises(risk.RiskRejection) as ei:
        risk.pre_trade_checks("buy", "ZZZ", 10, 100.0, _acct(), {}, cfg,
                              open_trades=stopless,
                              candidate_stop=99.0)     # a $10 risk, still refused
    assert ei.value.rail == "heat"


def test_a_stopless_position_cannot_normally_be_created(sandbox):
    """What keeps the trap above LATENT is a conjunction of two OTHER guards.

    If either stops holding, the heat rail arms silently. Asserting the guard
    here means its removal fails loudly next to the explanation, instead of
    showing up as a bot that quietly stopped trading.
    """
    assert risk.unprotectable_entry(100.0, None, _cfg()) is not None


def test_a_position_WITH_a_recorded_stop_leaves_room_to_trade(sandbox):
    """The contrast: heat is not simply always over cap. A properly bracketed
    book admits new entries, which is what makes the stopless case a defect
    rather than the design."""
    cfg = _cfg()
    bracketed = {"AAA": {"qty": 10, "entry_price": 100.0,
                         "order": {"stop_price": 99.0}}}
    assert risk.portfolio_heat(bracketed, cfg, 100_000) == 10.0
    risk.pre_trade_checks("buy", "ZZZ", 10, 100.0, _acct(), {}, cfg,
                          open_trades=bracketed, candidate_stop=99.0)


# ------------------------------------------- which rails the backtester inherits

def _rails_in(func_name: str) -> set:
    """Rail keys raised lexically inside one function of risk.py."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "risk.py")).read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {kw.value.value
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Call)
                    for kw in sub.keywords
                    if kw.arg == "rail" and isinstance(kw.value, ast.Constant)}
    raise AssertionError(f"{func_name} not found in risk.py")


def test_the_backtester_runs_under_FEWER_rails_than_live_and_the_gap_is_pinned():
    """A backtest inherits only `pure_checks` (backtest.py calls it directly);
    heat, correlation, the daily cap and HALT live in `pre_trade_checks` and
    never run in simulation.

    That gap makes every backtest OPTIMISTIC — simulated fills pass caps that
    live orders must also clear. Pinning both sets means adding a live-only rail
    forces a deliberate decision and a divergence entry, rather than widening
    the gap silently and flattering the next set of results.
    """
    # `desync_cover` and `net_exposure` were both added 2026-08-04 by Phase 1
    # of the long/short work — `desync_cover` the cover-side mirror of
    # `desync_sell`, `net_exposure` the directional 130/30 band. Both live in
    # `pure_checks`, not `pre_trade_checks`, so BOTH the live bot and the
    # backtester inherit them together — the pinned GAP between the two sets
    # below does not widen, and neither needs a divergence entry in
    # docs/divergences.md. That is exactly the deliberate decision this test
    # exists to force: a rail added instead to `pre_trade_checks` WOULD widen
    # the live-only gap and would require one. (Don't let this comment grow
    # into a list of every rail ever added — the reasoning is what matters,
    # not the roll call.)
    # `direction_conflict` and `sector_concentration` (2026-08-05, with the
    # `reclaim` leg) land in `pure_checks` for the same reason and with the same
    # consequence: the simulator inherits them, so the gap below is unchanged
    # and no divergence entry is owed. So does `sector_unmapped` (2026-11-04).
    assert _rails_in("pure_checks") == {
        "zero_qty", "order_value_cap", "drawdown", "max_open_positions",
        "strategy_slots", "position_cap", "regime_exposure", "desync_sell",
        "desync_cover", "net_exposure", "direction_conflict",
        "sector_unmapped", "sector_concentration"}
    assert _rails_in("pre_trade_checks") == {"halt", "daily_cap", "heat",
                                             "correlation"}


# ------------------------------------------------------- live rail telemetry

def test_the_live_path_records_WHICH_rail_refused_a_trade():
    """§40 taught the backtester to keep `e.rail` ('the reason existed at the
    moment of the block and was dropped'), but only the backtester. The live
    ledger stored the message prose and threw the key away, so the simulator
    could break blocks down by rail and the real bot could not — you had to
    string-match `detail` against text that is free to be reworded.

    Asserted against main.py's source: the point is that the call site passes it
    at all, and a mock that accepted anything would prove nothing.

    EVERY such call site must carry it, not just the one that raises.

    Written first as "find a rejection site and assert it passes `rail`", which
    passed on the first match and hid the fact that main.py has FOUR. Three of
    them (vendor divergence, downsize-to-zero, entry drift) are inline guards
    that never construct a RiskRejection, so a `rail` field covering only the
    raising path would have looked complete in the ledger while omitting most
    of it — the worst kind of partial telemetry, because it reads as total.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "src", "main.py")).read())
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log_decision"):
            continue
        detail = next((kw for kw in node.keywords if kw.arg == "detail"), None)
        # A rejection site is one whose detail is stamped "risk rejection".
        if detail and "risk rejection" in ast.dump(detail.value):
            sites.append((node.lineno,
                          {kw.arg for kw in node.keywords}))
    assert len(sites) >= 4, (
        f"expected every known rejection site; found {len(sites)}. If one was "
        f"removed, delete it here too — do not lower the number to go green.")
    missing = [ln for ln, kws in sites if "rail" not in kws]
    assert not missing, f"log_decision drops the rail key at main.py:{missing}"


def test_the_ledger_stores_the_rail_as_its_own_field(tmp_path):
    """End to end, not just the signature: a queryable key in the record."""
    import ledger as ledger_mod
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.jsonl"))
    lg.log_decision("ZZZ", "buy", "r", {}, None, executed=False,
                    detail="risk rejection: x", rail="daily_cap")
    rows = [json.loads(ln) for ln in
            open(tmp_path / "ledger.jsonl") if ln.strip()]
    assert rows[-1]["rail"] == "daily_cap"


# ------------------------------------------- the kill switch cannot block itself

def test_flatten_all_does_not_pass_through_the_rails():
    """`_kill_switch_fired` engages HALT and THEN calls `flatten_all()`
    (deliberately — a crash mid-flatten must not leave the breach un-halted).
    That ordering is only safe because the flatten bypasses the rails entirely:
    routing it through `pre_trade_checks` would make the kill switch refuse its
    own liquidation on the `halt` rail it had just set, one line earlier.

    Proven against the source rather than by mocking, so it holds for whatever
    a future refactor does to the call path.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "src", "broker.py")).read()
    assert "import risk" not in src, "broker.py must not depend on the rails"
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "flatten_all":
            called = {n.func.attr for n in ast.walk(node)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)}
            assert not called & {"pre_trade_checks", "pure_checks"}
            return
    raise AssertionError("flatten_all not found in broker.py")
