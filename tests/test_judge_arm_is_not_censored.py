"""The judge's approve arm must survive a rails rejection.

WHY THIS EXISTS. Measured on the live ledger 2026-09-08: of 250 judged buys
the judge returned **34 full-size approves**, and every single one was killed
by a deterministic risk rail — max trades per day, max open positions, the
down-regime cap, zero computed quantity, the heat cap. Not one reached an
outcome.

Worse, the rails path recorded them as `verdict="rails_reject", kind="rails"`,
overwriting what the judge had said, and `calibration_metrics` filters on
`kind == "llm"`. So a signal the judge APPROVED was scored as though the judge
had never spoken. Two of the judge's three arms were censored: approves never
executed, and the record then forgot they were approves.

The consequence is the reason this file exists: **"is this judge any good" was
structurally unanswerable, for any model.** Swapping in a stronger one would
have changed nothing measurable, because the measurement could not see the
arm where the models differ most.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import judgments  # noqa: E402


def _store(tmp_path):
    import store
    store.configure({"storage": {"backend": "jsonl"}})
    return judgments.JudgmentStore(str(tmp_path / "j.jsonl"))


# --- the field survives the rail --------------------------------------------

def test_a_rails_block_records_what_the_judge_had_said(tmp_path):
    js = _store(tmp_path)
    js.log_judgment("t1", "AAPL", "buy", "rails_reject", 1.0, 100.0, "up/low",
                    kind="rails", executed=False, reasoning="heat cap",
                    judge_verdict="approve", judge_scale=1.0,
                    judge_confidence=0.62)
    j = list(js.replay().values())[0]
    assert j["verdict"] == "rails_reject", "the rail really did decide"
    assert j["kind"] == "rails"
    assert j["judge_verdict"] == "approve", (
        "the judge's approve was erased by the rail — the censoring this "
        "file exists to prevent")
    assert j["judge_scale"] == 1.0
    assert j["judge_confidence"] == 0.62


def test_absent_judge_fields_are_none_not_a_verdict(tmp_path):
    """A pre-judge rails block (datacheck, halt, universe) happens BEFORE the
    judge runs. None must mean "never spoke", never a silent "approve"."""
    js = _store(tmp_path)
    js.log_judgment("t2", "SPY", "buy", "rails_reject", 1.0, 100.0, "up/low",
                    kind="rails", executed=False, reasoning="datacheck")
    j = list(js.replay().values())[0]
    assert j["judge_verdict"] is None
    assert j["judge_scale"] is None
    assert j["judge_confidence"] is None


# --- the metric can finally see it ------------------------------------------

def _resolved(js, jid, assessment):
    js.log_resolution(jid, "counterfactual", 1.0, "horizon", assessment)


def test_blocked_approves_are_scored_once_they_resolve(tmp_path):
    """The deliverable: a non-zero count where today there are none."""
    js = _store(tmp_path)
    for i, outcome in enumerate(["good_veto", "bad_veto", "bad_veto",
                                 "bad_veto", "bad_veto"]):
        jid = js.log_judgment(f"t{i}", "AAPL", "buy", "rails_reject", 1.0,
                              100.0, "up/low", kind="rails", executed=False,
                              judge_verdict="approve", judge_scale=1.0)
        _resolved(js, jid, outcome)
    m = judgments.calibration_metrics(js.replay(), min_n=5)
    assert m["n_blocked_approves_resolved"] == 5
    # 4 of 5 were bad blocks -> the judge's approve would have been right 4/5.
    # _rate counts "good"-prefixed as good, i.e. good blocks; so 1/5.
    assert m["blocked_approve_accuracy"] == 1 / 5
    assert m["rails_overrode_judge"] == {"approve": 5}


def test_too_few_resolved_is_none_not_zero(tmp_path):
    """Absent-vs-zero: 'not enough data' and 'the judge was never right' must
    not share a value."""
    js = _store(tmp_path)
    jid = js.log_judgment("t1", "AAPL", "buy", "rails_reject", 1.0, 100.0,
                          "up/low", kind="rails", executed=False,
                          judge_verdict="approve", judge_scale=1.0)
    _resolved(js, jid, "bad_veto")
    m = judgments.calibration_metrics(js.replay(), min_n=5)
    assert m["n_blocked_approves_resolved"] == 1
    assert m["blocked_approve_accuracy"] is None


def test_the_rails_scoreboard_still_counts_rails_blocks(tmp_path):
    """Carrying the judge's verdict must not stop a rails block being a rails
    block — the rail really did make the decision and its own precision
    metric has to keep working."""
    js = _store(tmp_path)
    for i in range(5):
        jid = js.log_judgment(f"t{i}", "AAPL", "buy", "rails_reject", 1.0,
                              100.0, "up/low", kind="rails", executed=False,
                              judge_verdict="approve", judge_scale=1.0)
        _resolved(js, jid, "good_veto")
    m = judgments.calibration_metrics(js.replay(), min_n=5)
    assert m["rails_block_precision"] == 1.0
    assert m["n_rails_resolved"] == 5


# --- the call sites actually pass it ----------------------------------------

def test_every_post_judge_rails_site_carries_the_verdict():
    """A correct store field that no caller populates is the failure mode this
    whole change exists to fix, one level up. Asserted at the source: each
    post-judge `rails_reject` log_judgment call must pass judge_verdict."""
    src = (ROOT / "src" / "main.py").read_text()
    calls = src.count('"rails_reject"')
    carried = src.count("judge_verdict=review.get")
    # 4 since 2026-11-03: the entry cost filter (§82) is a post-judge rails
    # site too and carries the verdict like the other three.
    assert carried == 4, (
        f"expected the 4 post-judge rails sites to carry the verdict, "
        f"found {carried}")
    assert calls >= 4, "the pre-judge rails site should still exist uncarried"
