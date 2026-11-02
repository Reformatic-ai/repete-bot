"""The per-symbol research dossier: degrades, never decides, never crashes.

The judge's context has always been GLOBAL — the last 20 resolved calls across
every symbol, aggregate calibration, one 600-char news block. This module
answers the questions about the name actually in front of it.

Three properties matter more than the content, and this file exists for them:

1. A failing tool DEGRADES the brief. It does not raise. A cycle that does not
   trade because a news file was unreadable is a worse outcome than a trade
   judged on three findings instead of four.
2. "UNAVAILABLE" and "nothing on record" are DIFFERENT and stay different.
   Conflating them is how a broken retriever reads as a quiet market — the
   same error as `data_age_hours` returning 0.0 for an unreadable ledger, and
   as `absence is not an exit`.
3. The brief renders inside its budget by dropping WHOLE findings and saying
   so. §61 found the judge had been reading context cut mid-word at
   `"Iran deal h"`, with nothing marking the loss.
"""
from datetime import datetime, timezone

import pytest

import research

NOW = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
CFG = {"risk": {}, "sectors": {}, "news": {"memory": {}}}


# ---- failure policy -------------------------------------------------------

def test_a_failing_tool_degrades_the_brief_instead_of_raising():
    """THE LOAD-BEARING ONE. Every tool blows up; build() still returns."""
    def boom():
        raise RuntimeError("disk on fire")

    f = research._safe("news", boom)
    assert f.ok is False
    assert "disk on fire" in f.error
    assert "RuntimeError" in f.error


def test_a_failure_carries_its_cause():
    """An UNAVAILABLE with no reason is a silent failure wearing a label."""
    f = research._safe("news", lambda: (_ for _ in ()).throw(ValueError("bad json")))
    assert f.error and "bad json" in f.error
    assert f.detail.get("traceback")


def test_unavailable_is_not_the_same_as_nothing_found():
    """The distinction this repo keeps having to relearn."""
    failed = research._safe("news", lambda: (_ for _ in ()).throw(OSError("gone")))
    empty = research._safe("news", lambda: ("", {"n": 0}))

    assert failed.ok is False and empty.ok is True
    assert "UNAVAILABLE" in failed.line()
    assert "nothing on record" in empty.line()
    assert failed.line() != empty.line()


def test_build_never_raises_even_with_garbage_inputs():
    b = research.build("AAPL", "buy", cfg={}, positions=None,
                       judgments=None, now=NOW)
    assert isinstance(b, research.ResearchBrief)
    assert b.findings, "a brief with no findings at all is not a brief"


# ---- the degraded flag ----------------------------------------------------

def test_degraded_is_true_when_any_tool_failed():
    b = research.ResearchBrief("AAPL", "buy", findings=[
        research.Finding("news", ok=True, summary="x"),
        research.Finding("book", ok=False, error="boom"),
    ])
    assert b.degraded is True
    assert b.failed_tools == ["book"]


def test_a_fully_successful_brief_is_not_degraded():
    b = research.ResearchBrief("AAPL", "buy", findings=[
        research.Finding("news", ok=True, summary=""),
    ])
    assert b.degraded is False


def test_the_header_names_what_was_unavailable():
    """The judge must be able to discount a partial dossier. A brief that
    hides its own gaps invites reading three findings as four."""
    b = research.ResearchBrief("AAPL", "buy", findings=[
        research.Finding("earnings", ok=False, error="no cache"),
    ])
    block = b.to_block(500)
    assert "PARTIAL" in block and "earnings" in block


# ---- budget ---------------------------------------------------------------

def test_the_block_respects_its_budget():
    b = research.ResearchBrief("AAPL", "buy", findings=[
        research.Finding("t%d" % i, ok=True, summary="y" * 80) for i in range(10)
    ])
    for budget in (120, 300, 1000):
        assert len(b.to_block(budget)) <= budget, f"overran budget {budget}"


def test_truncation_drops_whole_findings_and_says_so():
    """Not a mid-word cut. §61's judge was reading `"Iran deal h"`."""
    b = research.ResearchBrief("AAPL", "buy", findings=[
        research.Finding("t%d" % i, ok=True, summary="y" * 60) for i in range(8)
    ])
    block = b.to_block(200)
    assert "omitted for space" in block
    for line in block.split("\n")[1:]:
        if line.startswith("- (") or not line.startswith("- "):
            continue
        assert line.endswith("y") or ":" in line, "a finding was cut mid-line"


# ---- the tools, on real shapes -------------------------------------------

def test_prior_calls_reads_only_this_symbol():
    js = {"a": {"symbol": "AAPL", "verdict": "downsize", "ts": "2026-08-01",
                "pnl_pct": 2.0},
          "b": {"symbol": "MSFT", "verdict": "veto", "ts": "2026-08-02",
                "pnl_pct": -1.0}}
    summary, detail = research._prior_calls_on_symbol("AAPL", js)
    assert detail["n"] == 1 and "MSFT" not in summary
    assert "1W/0L" in summary


def test_prior_calls_on_an_unseen_symbol_is_empty_not_an_error():
    summary, detail = research._prior_calls_on_symbol("NVDA", {})
    assert summary == "" and detail["n"] == 0


def test_prior_calls_counts_wins_and_losses_separately():
    js = {str(i): {"symbol": "AAPL", "verdict": "approve", "ts": f"2026-08-0{i}",
                   "pnl_pct": p}
          for i, p in enumerate([3.0, -2.0, 1.0], start=1)}
    summary, detail = research._prior_calls_on_symbol("AAPL", js)
    assert detail["w"] == 2 and detail["l"] == 1


@pytest.mark.parametrize("pnl,expect", [(None, "open"), (0.0, "+0.0%")])
def test_an_unresolved_prior_call_is_not_scored_as_flat(pnl, expect):
    """A trade still open is not a zero-return trade. Scoring it as one would
    quietly dilute the win/loss counts toward nothing."""
    js = {"a": {"symbol": "AAPL", "verdict": "approve", "ts": "2026-08-01",
                "pnl_pct": pnl, "executed": True}}
    summary, detail = research._prior_calls_on_symbol("AAPL", js)
    assert expect in summary
    if pnl is None:
        assert detail["w"] == 0 and detail["l"] == 0


# ---- the invariant --------------------------------------------------------

def test_the_brief_carries_no_key_that_could_redirect_a_trade():
    """This module returns EVIDENCE. If a brief ever grew a `symbol` or `qty`
    field that main.py might read, it would become a second channel around
    the guard in tests/test_judge_verdict_surface.py."""
    b = research.build("AAPL", "buy", CFG, positions={}, judgments={}, now=NOW)
    forbidden = {"qty", "quantity", "size", "price", "stop", "target",
                 "action_override", "verdict", "scale"}
    for f in b.findings:
        assert not (set(f.detail) & forbidden), (
            f"{f.tool} returned {sorted(set(f.detail) & forbidden)} — the "
            f"research layer must not carry fields that name a trade")


# ---- against the SHIPPED config, not a convenient fiction -----------------

def test_book_exposure_resolves_a_sector_from_the_real_config():
    """The first version of this test invented a flat {"AAPL": "tech"} config.
    That shape does not exist — the real block is `sectors: {Technology:
    [AAPL, ...]}`, inverted by `strategies.base.sector_map`. A tool tested
    only against a made-up shape is a tool that has never been tested.
    """
    import os

    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(root, "config.yaml")))

    summary, detail = research._book_exposure("AAPL", {"AAPL": {"qty": 10}}, cfg)
    assert detail["sector"], "AAPL resolved to no sector against the real config"
    assert detail["held"] is True
    assert "already held" in summary


def test_an_unmapped_symbol_is_absent_not_a_sector_named_none():
    """`sector_map`'s own docstring: a symbol in no sector is ABSENT, not in a
    sector called None — so unmapped names are never lumped into one
    pseudo-sector. Since 2026-11-02 every core name is mapped (SPY is "Broad
    market"), so this uses DELL, a news nomination outside the core list."""
    import os

    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(root, "config.yaml")))

    _, detail = research._book_exposure("DELL", {}, cfg)
    assert detail["sector"] is None
    _, spy = research._book_exposure("SPY", {}, cfg)
    assert spy["sector"] == "Broad market"


def test_the_news_finding_reads_the_field_news_memory_ACTUALLY_writes(monkeypatch):
    """The first cut guessed `note`/`headline`. Neither exists — news_memory
    writes `text` (news_memory.py:121). Against a real ledger the block
    rendered "11 flagged; 2026-07-31 | 2026-07-31 | ..." — five dates and no
    information, while every unit test passed, because they all fed the shape
    the reader hoped for rather than the one the WRITER produces.

    This asserts against the writer's shape.
    """
    import news_memory
    rows = [{"ts": "2026-07-31T12:00:00+00:00", "date": "2026-07-31",
             "symbol": "AAPL", "text": "Q4 guidance miss triggered a sell-off",
             "nominated": True}]
    monkeypatch.setattr(news_memory, "history", lambda *a, **k: rows)

    summary, detail = research._news_on_symbol("AAPL", CFG, NOW)
    assert "Q4 guidance miss" in summary, (
        "the news finding dropped the only informative field it had")
    assert "2026-07-31" in summary
    assert detail["n"] == 1


def test_a_news_row_with_no_text_does_not_render_as_a_bare_date(monkeypatch):
    """The failure mode above, asserted directly: a row carrying nothing
    useful must not masquerade as an entry with content."""
    import news_memory
    monkeypatch.setattr(news_memory, "history",
                        lambda *a, **k: [{"date": "2026-07-31", "text": ""}])
    summary, _ = research._news_on_symbol("AAPL", CFG, NOW)
    assert summary.strip().endswith("2026-07-31"), summary
