"""The live kill is a LIVE rail. The simulator has never modelled it — #23.

Until 2026-11-04 this file only made divergence #23 falsifiable: it pinned
that the simulator does not call `live_kill_blocked` and the live path does.
#23 itself stayed open, because the gap was BINDING — tsmom was retired live by
the kill on 2026-08-20 and still `enabled: true` in config.yaml, so every
backtest traded it to the end of the window.

The owner-approved round-two plan (2026-11-04, B1) closed it the other way
round from the one the register ruled out: not by wiring the kill into
`simulate_ensemble` (which would silently move the baseline of every frozen
registration), but by making config.yaml say what live already does —
`strategies.tsmom.enabled: false`. `test_every_strategy_the_kill_retired_is_
disabled` is the test that fails if #23 reopens. "A divergence is CLOSED only
when a test would fail if it reopened."

The two AST tests stay: the gap is still a real asymmetry in the code, and the
day a SECOND strategy is retired live it reopens for that strategy. They stop
the register drifting from the code in BOTH directions, because a one-sided
assertion would pass just as happily if `live_kill_blocked` were deleted.
"""
import ast
import re
from pathlib import Path

import risk

SRC = Path(risk.__file__).parent
REGISTER = SRC.parent / "docs" / "divergences.md"


def _calls(path: Path) -> set[str]:
    """Every function name called anywhere in a module."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name:
                out.add(name)
    return out


def test_the_simulator_does_not_apply_the_live_kill():
    """One half of #23. An AST scan, not a grep, so a call inside a comment or
    a string cannot satisfy it and a real call cannot hide from it."""
    assert "live_kill_blocked" not in _calls(SRC / "backtest.py"), (
        "src/backtest.py now calls live_kill_blocked. Divergence #23 says it "
        "does not (it was closed in config, 2026-11-04) — if that is "
        "deliberate, rewrite #23 in docs/divergences.md rather than deleting "
        "this test, and re-run the frozen gates: an in-run kill can retire any "
        "strategy mid-window, so the baseline of every registration can move.")


def test_the_live_path_does_apply_it():
    """The other half. Without this, deleting `live_kill_blocked` entirely
    would make the test above pass — a green suite reporting that a divergence
    is intact when the rail on the live side of it no longer exists."""
    for module in ("main.py", "swing_scan.py"):
        assert "live_kill_blocked" in _calls(SRC / module), (
            f"src/{module} no longer calls live_kill_blocked. The rail that "
            f"retired tsmom's entries on 2026-08-20 is gone from the live "
            f"path, and divergence #23 describes a gap that no longer has two "
            f"sides.")


def test_the_kill_is_named_on_both_live_raise_sites():
    """It logged fifteen production rejections as `unattributed` before
    2026-08-23 (the BIZON's ledger — this repo's copy froze at the 2026-08-20
    cutover and greps zero). `tests/test_block_census.py` forbids bare raises
    generally; this pins THIS rail's name, which the register entry cites."""
    for module in ("main.py", "swing_scan.py"):
        src = (SRC / module).read_text()
        assert 'rail="live_kill"' in src, (
            f"src/{module} raises the live kill without naming the rail")
    assert "live_kill" in risk.NON_PURE_RAILS


#: Strategies the live kill has retired, with the date it first fired and the
#: record it fired on. Read off the PRODUCTION ledger (the Bizon's), never this
#: repo's copy — see #23's "Read the production ledger" note. A strategy the
#: kill retires later goes HERE, with its config flipped in the same change;
#: otherwise #23 reopens for it and the backtests trade a strategy live refuses.
RETIRED_LIVE = {
    # 15 trades PF 0.145 when it first fired; 34 trades PF 0.45 after cost on
    # 2026-11-04, measured with risk.live_kill_blocked on the live ledger.
    "tsmom": "2026-08-20",
}


def _shipped_cfg() -> dict:
    import yaml
    return yaml.safe_load((SRC.parent / "config.yaml").read_text())


def test_every_strategy_the_kill_retired_is_disabled():
    """THE TEST THAT CLOSES #23. The simulator enters only `enabled` strategies
    (strategies.enabled, backtest.simulate_ensemble), so a retired strategy left
    enabled is a strategy every gate trades and live refuses — the divergence,
    exactly. Flipping it back on without a fresh gate pass reopens #23."""
    cfg = _shipped_cfg()
    still_on = sorted(name for name in RETIRED_LIVE
                      if ((cfg.get("strategies") or {}).get(name) or {})
                      .get("enabled"))
    assert still_on == [], (
        f"{still_on} {'is' if len(still_on) == 1 else 'are'} enabled in "
        f"config.yaml but retired live by risk.live_kill (RETIRED_LIVE). Every "
        f"backtest now trades what the live bot refuses — divergence #23 is "
        f"open again. Re-enabling needs a fresh gate pass; if that happened, "
        f"remove the name from RETIRED_LIVE in the same change and say why.")


def test_the_closing_test_can_fail():
    """The twin. A closing test that passes on any config closes nothing."""
    import strategies
    cfg = _shipped_cfg()
    cfg["strategies"]["tsmom"]["enabled"] = True
    assert "tsmom" in [n for n, _ in strategies.enabled(cfg)]
    assert cfg["strategies"]["tsmom"].get("enabled")


def test_the_simulator_enters_no_retired_strategy():
    """The consumer, not just the flag: the simulator's entry list is
    `strategies.enabled(cfg)` (backtest.py), and on the shipped config it must
    not contain a strategy the kill retired."""
    import strategies
    entering = {n for n, _ in strategies.enabled(_shipped_cfg())}
    assert not entering & set(RETIRED_LIVE), sorted(entering & set(RETIRED_LIVE))


def test_the_register_lists_23_as_closed_by_this_file():
    """Ties the code fact to the document. #21 was registered with a full
    section and no table row, and every count in the repo agreed with every
    other count for days — the failure this half exists to prevent."""
    text = REGISTER.read_text()
    assert re.search(r"^## #23", text, re.M), "#23 has no section in the register"
    row = re.search(r"^\| 23 \| .*$", text, re.M)
    assert row, "#23 has no summary-table row"
    assert "closed" in row.group(0).lower(), row.group(0)
    assert "test_live_kill_is_live_only.py" in row.group(0), (
        "#23's row no longer names the test that closes it")
    m = re.search(r"\*\*Open as of [^:]+: ([^*]+)\*\*", text)
    assert m, "the `Open as of` line is gone from docs/divergences.md"
    assert "#23" not in m.group(1), (
        "#23 is listed open again. If it reopened, this file should have gone "
        "red first — rewrite it to say why rather than leaving it asserting a "
        "status the register no longer claims.")
