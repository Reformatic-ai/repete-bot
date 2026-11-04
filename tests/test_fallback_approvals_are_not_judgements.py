"""A fallback approval is not a judgement, and must never be counted as one.

THE MEASUREMENT THAT PROMPTED THIS (audit, 2026-08-21). The live judgment
ledger held 47 kind="llm" rows: 30 downsize, 11 veto, and 6 approve. All six
approvals were the fallback the code emits when the judge is unreachable —
`{"verdict": "approve", "scale": 1.0}` with the reasoning "LLM review
disabled/unavailable". So the judge, on every occasion it was actually reached,
never once approved a trade at full size. Every genuine verdict was a downsize
or a veto.

That mattered in two places, both of which fed themselves:

  1. judgments.calibration_metrics counted all six in `approves`, so
     `approve_accuracy` was computed entirely from trades no model approved —
     and calibration_line puts that number back into the review prompt.
  2. scripts/calibrate_judge.py had no exclusion either, so fallback rows
     (always scale 1.0) pulled the modelled haircut toward "no haircut":
     measured 15 degraded of 301 rows, inflating mean_scale 0.6482 -> 0.6664
     in the PERMISSIVE direction. That number lands in
     knowledge/judge_calibration.json and sizes entries in every backtest run
     with the judge on.

These tests are the mutation guard for both. Revert either filter and one goes
red.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import judgments as jm
import llm
from judgments import JudgmentStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return JudgmentStore(str(tmp_path / "judgments.jsonl"))


def _resolve(store, jid, verdict, executed, pnl_pct):
    store.log_resolution(jid, "realized" if executed else "counterfactual",
                         pnl_pct, "stop_loss",
                         jm.assess(verdict, executed, pnl_pct))


def test_a_genuine_approval_still_counts(store):
    """The control. If this ever goes red the filter is eating real data."""
    jid = store.log_judgment("t1", "SPY", "buy", "approve", 1.0, 100.0,
                             "up/low", "llm", True,
                             reasoning="Trend intact and the analogs held.")
    _resolve(store, jid, "approve", True, 4.0)
    m = jm.calibration_metrics(store.replay(), min_n=1)
    assert m["n_approves_resolved"] == 1
    assert m["approve_accuracy"] == 1.0


def test_a_degraded_approval_is_excluded_by_kind(store):
    """Going forward, main.py and swing_scan.py write kind='degraded'."""
    jid = store.log_judgment("t2", "SPY", "buy", "approve", 1.0, 100.0,
                             "up/low", "degraded", True,
                             reasoning=llm.FALLBACK_REASONING)
    _resolve(store, jid, "approve", True, 4.0)
    m = jm.calibration_metrics(store.replay(), min_n=1)
    assert m["n_approves_resolved"] == 0
    assert m["approve_accuracy"] is None
    assert m["n_resolved"] == 0


def test_a_historical_unmarked_fallback_is_excluded_by_its_reasoning(store):
    """THE ONE THAT PROTECTS THE SIX REAL RECORDS.

    They are on disk with kind='llm' and no degraded marker, written before the
    markers existed, and the ledger is append-only so they cannot be amended.
    The reasoning string is the only thing that distinguishes them from a real
    approval — which is why llm.FALLBACK_REASONING is a shared constant.
    """
    jid = store.log_judgment("t3", "SPY", "buy", "approve", 1.0, 100.0,
                             "up/low", "llm", True,
                             reasoning=llm.FALLBACK_REASONING)
    _resolve(store, jid, "approve", True, 4.0)
    m = jm.calibration_metrics(store.replay(), min_n=1)
    assert m["n_approves_resolved"] == 0, (
        "a fallback written before the degraded markers existed is still a "
        "fallback; counting it is how 100% of recorded approvals became fake")


def test_the_mix_reproduces_the_live_shape(store):
    """One real downsize plus one fallback approve — the live ratio in
    miniature. Only the downsize may survive into the metrics."""
    real = store.log_judgment("t4", "SPY", "buy", "downsize", 0.5, 100.0,
                              "up/low", "llm", True,
                              reasoning="Extended; halving.")
    _resolve(store, real, "downsize", True, -2.0)
    fake = store.log_judgment("t5", "QQQ", "buy", "approve", 1.0, 100.0,
                              "up/low", "llm", True,
                              reasoning=llm.FALLBACK_REASONING)
    _resolve(store, fake, "approve", True, 6.0)

    m = jm.calibration_metrics(store.replay(), min_n=1)
    assert m["n_resolved"] == 1
    assert m["n_downsizes_resolved"] == 1
    assert m["n_approves_resolved"] == 1  # the downsize executed; the fake did not count


def test_calibrate_judge_excludes_fallbacks_from_the_haircut(tmp_path):
    """The second contamination site, end to end through the real script.

    A fallback is always scale 1.0, so including it can only ever pull
    mean_scale UP — toward 'the judge cuts nothing'. With one genuine 0.5 and
    one fallback 1.0, an unfiltered run reports 0.75 and a filtered run
    reports 0.5.
    """
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"type": "decision", "action": "buy", "ts": "2026-08-01T00:00:00+00:00",
         "llm_review": {"verdict": "downsize", "scale": 0.5,
                        "reasoning": "Extended; halving."}},
        {"type": "decision", "action": "buy", "ts": "2026-08-02T00:00:00+00:00",
         "llm_review": {"verdict": "approve", "scale": 1.0,
                        "reasoning": llm.FALLBACK_REASONING}},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))

    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "calibrate_judge.py"),
         # --since: the rows are dated before the default window (the local
         # judge's first day, 2026-08-28), and the window is not this test.
         "--ledger", str(ledger), "--since", "2026-08-01"],
        capture_output=True, text=True, cwd=str(ROOT))

    # It exits 1 below MIN_SAMPLE, which is correct and not what we're testing.
    text = out.stdout
    assert "mean_scale: 0.5" in text, (
        f"fallback rows must not enter the haircut; got:\n{text}")
    assert "n_judged_buys: 1" in text
    assert "n_degraded_excluded: 1" in text, (
        "the exclusion must be VISIBLE — a filter nobody can see is "
        "indistinguishable from one that never fired")
