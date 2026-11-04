"""Scoring 55 frozen predictions against what actually happened.

`prior` is mandatory on every spec and `research/README.md` says why: it is
"the only thing that makes a surprise legible afterwards." Fifty-five of them
were written, hashed before the run, and never once read back.

The readings are a SIDECAR and must stay one. `gatespec.canonical()` hashes the
whole parsed spec and `register_gate.py` refuses re-registration once a verdict
exists, so no field can be added to any of the 55 frozen specs — not now, not
ever. That constraint is what these tests protect.
"""
import json

import pytest

import recall


@pytest.fixture(scope="module")
def regs():
    return recall.gatespec.registrations(
        recall._path(recall.gatespec.REGISTRATIONS))


def test_every_registration_has_been_read(regs):
    read = recall.readings()
    assert set(read) == set(regs), (
        f"unread: {sorted(set(regs) - set(read))}; "
        f"readings with no registration: {sorted(set(read) - set(regs))}")


def test_a_reading_must_quote_its_prior_byte_exact(regs):
    """A reading that does not quote the prior it claims to read is an
    assertion about the record rather than a reading of it."""
    for rec in recall.readings().values():
        recall.validate_reading(rec, regs)
        assert rec["quote"] in regs[rec["id"]]["spec"]["prior"]


def test_a_paraphrased_quote_is_refused(regs):
    rec = dict(recall.readings()["s57a"])
    rec["quote"] = "the honest expectation is that it fails"  # case changed
    with pytest.raises(recall.RecallError, match="byte-exact"):
        recall.validate_reading(rec, regs)


def test_a_reading_bound_to_a_stale_sha_is_refused(regs):
    """A re-registration moves `spec_sha256`, so a reading of the superseded
    text surfaces as a mismatch instead of silently describing a prior that is
    no longer the goalpost."""
    rec = dict(recall.readings()["s57a"])
    rec["spec_sha256"] = "0" * 64
    with pytest.raises(recall.RecallError, match="re-registered"):
        recall.validate_reading(rec, regs)


def test_an_unknown_direction_is_refused(regs):
    rec = dict(recall.readings()["s57a"])
    rec["direction"] = "probably"
    with pytest.raises(recall.RecallError, match="direction"):
        recall.validate_reading(rec, regs)


def test_readings_are_attributed_and_dated(regs):
    """Never anonymous. A reading carries who made it and when, and
    `approved_by` stays null until the owner has actually reviewed it — the
    agent may not sign for the owner."""
    for rec in recall.readings().values():
        assert rec["read_by"] and rec["read_at"]
        assert "approved_by" in rec, "the approval field must exist, even null"


def test_unread_ids_are_reported_and_never_dropped(monkeypatch, regs):
    """A calibration that quietly excluded what nobody had read would report
    the accuracy of the subset somebody chose to read."""
    partial = {k: v for k, v in recall.readings().items() if k != "s57a"}
    monkeypatch.setattr(recall, "readings",
                        lambda rel=recall.PRIOR_READINGS: partial)
    out = recall.calibrate()
    assert out["counts"]["unread"] == 1
    assert len(out["rows"]) == len(regs), "an unread id vanished from the table"
    assert any(r["id"] == "s57a" and r["direction"] == "unread"
               for r in out["rows"])


def test_no_expectation_is_excluded_from_both_sides():
    """s38 states outright that no expectation is recorded, because a prior is
    what makes a test falsifiable and s38 is not a test. Scoring it either way
    would invent a prediction nobody made."""
    out = recall.calibrate()
    # s38 (P13) plus s73d (2026-08-12): "No strong directional prior either
    # way" is stated outright, not a hedge dressed as a prediction.
    assert out["counts"]["no_expectation_stated"] == 2
    row = next(r for r in out["rows"] if r["id"] == "s38")
    assert row["direction"] == "no_expectation_stated"
    assert out["scored"] + out["counts"]["mixed"] \
        + out["counts"]["no_expectation_stated"] + out["counts"]["unread"] \
        == out["registrations"]


def test_the_base_rate_uses_the_SAME_denominator_as_the_hit_rate():
    """A hit rate over 51 specs printed beside a base rate over 55 is two
    different questions rendered as one, and the gap between them would read
    as skill. This was wrong in the first cut of `calibrate()`."""
    out = recall.calibrate()
    scored_failed = (out["cells"]["expected_fail|failed"]
                     + out["cells"]["expected_pass|failed"])
    assert out["base_rate_always_fail"] == round(scored_failed / out["scored"], 4)


def test_the_finding_every_scored_prior_predicted_failure():
    """§60. Not one of 55 priors predicted its own hypothesis would pass, so
    the author's hit rate and a constant "it fails" are the SAME strategy and
    score identically. The priors are honest; they are not informative."""
    out = recall.calibrate()
    # AMENDED 2026-08-12 (§72): the first-ever predicted_pass readings are the
    # three s72 confirmations — a DETERMINISTIC RE-SCORING of s68's measured
    # result, where predicting pass was arithmetic, not forecasting (s72c,
    # whose frozen rule predicted its own period FAILS, reads expected_fail).
    # AMENDED 2026-08-22 (§75): the FIRST genuine first-run forecast to
    # predict its own pass. s75a's prior named, before running, that clause
    # (b) would survive in 2000-2006 and fail the other three periods — a
    # specific pattern, not arithmetic on a measured result — and it did.
    # hit_rate moved to 0.83 against an always-fail base of 0.79: the first
    # time the author's forecasts have beaten the rubber stamp. On ONE
    # genuine first-run prediction, which is worth exactly that much; the
    # §60 finding is amended, not overturned, and it is written up in §75.
    assert out["predicted_pass"] == 4
    assert out["cells"]["expected_pass|passed"] == 4
    assert out["cells"]["expected_pass|failed"] == 0
    assert out["hit_rate"] >= out["base_rate_always_fail"], (
        "if these ever separate on FIRST-RUN priors, they have started carrying information "
        "and §60's finding has changed — which is a result worth writing up, "
        "not a test to relax")


def test_the_confounder_is_stated_in_the_output_not_a_footnote():
    out = recall.calibrate()
    assert "also sets the pass mark" in out["confounder"]
    assert "never a fact IN it" in out["status"]


def test_the_sidecar_is_append_only_json_lines():
    text = recall._read(recall.PRIOR_READINGS)
    rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    # 55 at P13; 63 after s62a-h (Phase 15, 2026-08-11).
    assert len(rows) == 108  # 55 at P13; 63 after s62; 79 after s64-s67; 102 after s73; 106 after s75; 107 after s81a; 108 after s84a;
                              # 98 after s72; 102 after s73a-d (2026-08-12)
    for r in rows:
        assert set(r) == {"id", "spec_sha256", "read_at", "read_by",
                          "approved_by", "direction", "quote"}


def test_no_reading_was_written_into_a_frozen_spec(regs):
    """The constraint that forced a sidecar in the first place. If a
    `direction` key ever appears inside a registered spec, someone has tried
    to edit a hashed pre-registration."""
    for spec_id, reg in regs.items():
        assert "direction" not in reg["spec"], spec_id
        assert "prior_reading" not in reg["spec"], spec_id
        assert recall.gatespec.canonical_sha256(reg["spec"]) \
            == reg["spec_sha256"], f"{spec_id}: the frozen spec no longer hashes"
