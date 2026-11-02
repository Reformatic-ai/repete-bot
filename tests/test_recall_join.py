"""The §↔spec join, derived twice and refusing when the derivations disagree.

Nothing in this repo mapped §57 to `s57a-d` before this. The mapping existed
only in prose and in spec header comments, which is a naming habit wearing the
clothes of a fact. So it is derived two ways — from the identifier and from the
spec file's own header — and a disagreement raises instead of picking a winner.
"""
import pytest

import recall


def test_every_registration_joins_to_a_section():
    joined = recall.join()
    # 55 at P13; 63 after s62a-h (Phase 15, 2026-08-11); 98 after s72
    # (2026-08-12); 102 after s73a-d (2026-08-12); 107 after s81a
    # (2026-11-02).
    assert len(joined) == 107, (
        "the join must cover every registration; if this number moved, a spec "
        "was registered and this test is the record of what the count was")
    keys = {s["key"] for s in recall.sections()}
    orphans = {i: v["section"] for i, v in joined.items()
               if v["section"] not in keys}
    assert not orphans, f"specs joining a section that does not exist: {orphans}"


def test_both_derivations_are_available_for_every_spec():
    """`id-only` is permitted by the code but should not silently become the
    norm — a spec whose file lost its `# §N` header still joins, and this test
    is what makes that visible rather than invisible."""
    weak = [i for i, v in recall.join().items() if v["source"] == "id-only"]
    assert not weak, (
        f"these specs join by identifier alone, with no `# §N` header comment "
        f"to corroborate it: {weak}")


def test_the_two_derivations_must_agree(tmp_path, monkeypatch):
    """A spec file whose header names a different section is a contradiction,
    and this module does not average contradictions."""
    regs = {"s58a": {"spec_sha256": "x", "spec": {}}}
    monkeypatch.setattr(recall, "section_of_spec_file", lambda i: "§99")
    with pytest.raises(recall.RecallError, match=r"s58a.*§58.*§99"):
        recall.join(regs)


def test_a_spec_id_that_is_not_a_spec_id_is_refused():
    with pytest.raises(recall.RecallError):
        recall.section_of_spec_id("coil25")


def test_section_numbers_are_read_only_from_paragraph_keys():
    """`## 1.` is NOT §1. Rewriting it would be an opinion about equivalence
    between two eras of this file, and METHOD NOTE 3 says the §1-§13 snapshot
    is gone and those numbers are unreproducible."""
    assert recall._leading_number("§58") == 58
    assert recall._leading_number("§14–§17") == 14
    assert recall._leading_number("1.") is None
    assert recall._leading_number("METHOD NOTE 2") is None
    assert recall._leading_number("PHASE 0") is None


def test_the_h3_only_exception_set_is_exactly_what_the_record_holds():
    """§30 is the only §-numbered section never given an H2. If a future
    section is written as an H3, this fails and somebody decides deliberately
    whether it is a section — rather than the parser widening on its own."""
    found = {s["key"] for s in recall.sections() if s["level"] == 3}
    assert found == set(recall.H3_ONLY_SECTIONS) == {"§30"}


def test_sub_lettered_h3_results_are_not_their_own_sections():
    """`### §19b RESULT`, `### §20a ADOPTED`, `### §33b RESULT` and
    `### §7–§9 RESULTS` all sit under an H2 of the same base number. Treating
    them as sections would double-count four results as four new claims."""
    keys = [s["key"] for s in recall.sections()]
    for absent in ("§19b", "§20a", "§33b", "§7–§9"):
        assert absent not in keys, f"{absent} is a sub-heading, not a section"


def test_first_registered_section_is_computed_not_asserted():
    """Rendered beside sections that carry no spec, so a reader joins two facts
    instead of being told a claim about any particular section."""
    assert recall.first_registered_section() == 35


def test_a_verdict_with_no_registration_is_caught_by_audit(monkeypatch):
    rows = recall.verdicts() + [{"id": "s99z", "passed": True,
                                 "ran_at": "2026-08-11T00:00:00+00:00",
                                 "arms": {}, "clauses": []}]
    monkeypatch.setattr(recall, "verdicts", lambda rel=recall.VERDICTS: rows)
    assert any("s99z" in p for p in recall.audit())


def test_the_shipped_record_audits_clean():
    assert recall.audit() == []


def test_gate_result_is_a_count_and_never_a_word():
    import re
    for key in ("§57", "§58", "§59", "§44", "§48", "§12", "METHOD NOTE"):
        assert re.fullmatch(r"\d+/\d+|—", recall.gate_result(key)), (
            "a ratio is arithmetic over rows and stays inside the one rule; a "
            "word like 'rejected' would be this module's opinion of them")


def test_the_three_most_recent_sections_are_all_zero_for_n():
    """§57, §58 and §59 read `pre-registered` in the index because that phrase
    is in their headings. This is what actually happened."""
    assert recall.gate_result("§57") == "0/4"
    assert recall.gate_result("§58") == "0/8"
    assert recall.gate_result("§59") == "0/8"


def test_a_family_specs_clauses_are_not_invisible():
    """Family specs put their per-clause results in `per_arm` and leave
    `clauses` empty — all twelve §37/§38/§58 rows do. A reader of `clauses`
    alone sees nothing for a third of §58."""
    row = recall.latest_verdicts()["s58a"]
    assert row["clauses"] == [], "s58a is a family spec; this pins the shape"
    lines = recall.clause_lines(row)
    assert lines, "clause_lines must unwrap per_arm for family specs"
    assert all(c["arm"] for c in lines)
