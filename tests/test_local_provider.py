"""A self-hosted, keyless judge provider — and the four ways it could lie.

WHY THIS EXISTS. The bot has refused every entry since 2026-08-21 because the
Anthropic account ran out of credits and `on_unavailable: block` did what it
was configured to do. The Bizon meanwhile runs qwen3.8-27b on vLLM across two
A100s, on its own docker network, idle.

`llm_client`'s docstring said a second provider "needs a second dependency and
a second API key that this deployment does not have". Both premises expired
when the deployment became the box: an OpenAI-compatible endpoint on the same
host needs no vendor key.

WHAT THIS FILE GUARDS. Not that the local model is good — nothing here claims
that, and `src/llm_shadow.py` exists to measure it. These are the places a
keyless provider could report something false:

  1. cost — pricing a free call against a vendor table, or calling a known
     zero "unknown"
  2. preflight — demanding a key that cannot exist, which fails UNSAFE while
     looking like caution
  3. the served model id — the ledger must name which judge decided
  4. the endpoint — a keyless provider with no base_url has nowhere to send
     the prompt, and a guessed default sends it somewhere nobody chose
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import llm_client  # noqa: E402


def _cfg(**over):
    c = {"llm": {"enabled": True, "provider": "local",
                 "base_url": "http://vllm-qwen38:8000/v1",
                 "model": "qwen3.8-27b", "timeout_seconds": 30}}
    c["llm"].update(over)
    return c


def _anthropic_cfg(**over):
    c = {"llm": {"enabled": True, "provider": "anthropic",
                 "model": "claude-opus-5"}}
    c["llm"].update(over)
    return c


# ---- 1. cost --------------------------------------------------------------

def test_a_local_call_costs_zero_not_unknown():
    """The ONE place 0.0 is a measurement rather than the absent-vs-zero
    collapse this repo forbids. A self-hosted model incurs no vendor charge —
    that is known exactly. Returning None would report "we do not know what
    this cost" about the only call whose cost we do know."""
    got = llm_client.estimate_cost_usd(
        _cfg(), {"model": "qwen3.8-27b", "input_tokens": 5000,
                 "output_tokens": 900})
    assert got == 0.0
    assert got is not None


def test_a_local_call_is_free_even_with_no_token_counts():
    """A vendor call with no usage returns None, because the price depends on
    tokens. A local call does not — zero times anything is still zero."""
    assert llm_client.estimate_cost_usd(
        _cfg(), {"model": "qwen3.8-27b",
                 "input_tokens": None, "output_tokens": None}) == 0.0


def test_an_unpriced_VENDOR_model_still_returns_None():
    """The keyless shortcut must not have swallowed the unknown-price case."""
    assert llm_client.estimate_cost_usd(
        _anthropic_cfg(), {"model": "some-unlisted-model",
                           "input_tokens": 10, "output_tokens": 10}) is None


# ---- 2. does this provider need a key at all --------------------------------

def test_a_keyless_provider_is_configured_without_a_key(monkeypatch):
    """THE ONE THAT UNBLOCKS THE BOT. If `configured()` demanded a key here,
    the judge would report unavailable while a working model sat on the same
    host — and `on_unavailable: block` would refuse every entry for a reason
    that is not true."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_client.needs_key(_cfg()) is False
    assert llm_client.configured(_cfg()) is True


def test_a_keyed_provider_still_requires_its_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_client.needs_key(_anthropic_cfg()) is True
    assert llm_client.configured(_anthropic_cfg()) is False


def test_disabled_is_still_disabled_for_a_keyless_provider():
    """Keyless must not mean always-on."""
    assert llm_client.configured(_cfg(enabled=False)) is False


# ---- 3. the endpoint ------------------------------------------------------

def test_a_keyless_provider_without_a_base_url_refuses():
    """No endpoint is not a default to guess. Guessing sends the judge's
    prompt — the book, the lessons, the news — somewhere nobody chose."""
    with pytest.raises(RuntimeError, match="base_url"):
        llm_client.base_url(_cfg(base_url=None))


# ---- 4. the adapter tells the truth about what answered ---------------------

class _FakeResponse:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _serve(monkeypatch, payload, capture=None):
    import urllib.request
    def fake_urlopen(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["body"] = json.loads(req.data.decode())
            capture["timeout"] = timeout
        return _FakeResponse(payload)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


PAYLOAD = {
    "id": "cmpl-abc", "model": "qwen3.8-27b",
    "choices": [{"message": {"content": '{"verdict": "downsize"}'},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 535, "completion_tokens": 240},
}


def test_the_served_model_id_reaches_the_meta(monkeypatch):
    """The ledger records which judge decided a trade. If this reported the
    CONFIGURED name rather than what answered, a server quietly loaded with a
    different model would be invisible — the same failure `fell_back` was
    added to make visible for the vendor path."""
    _serve(monkeypatch, {**PAYLOAD, "model": "qwen3.8-27b-AWQ"})
    text, meta = llm_client.complete_detailed(
        _cfg(), "sys", "user", max_tokens=100)
    assert meta["model"] == "qwen3.8-27b-AWQ"
    assert meta["requested_model"] == "qwen3.8-27b"
    assert text == '{"verdict": "downsize"}'


def test_token_counts_are_mapped_from_the_openai_names(monkeypatch):
    _serve(monkeypatch, PAYLOAD)
    _, meta = llm_client.complete_detailed(_cfg(), "s", "u", max_tokens=100)
    assert meta["input_tokens"] == 535
    assert meta["output_tokens"] == 240


def test_absent_cache_fields_are_None_not_zero(monkeypatch):
    """An OpenAI-compatible server reports no cache accounting. That is not a
    report of zero cache reads."""
    _serve(monkeypatch, PAYLOAD)
    _, meta = llm_client.complete_detailed(_cfg(), "s", "u", max_tokens=100)
    assert meta["cache_read_input_tokens"] is None
    assert meta["cache_creation_input_tokens"] is None


def test_the_system_prompt_is_sent_as_a_system_ROLE(monkeypatch):
    """The whole adapter: `system` is a top-level argument for the vendor and a
    message role here. Drop it and the judge loses its entire instruction set
    while still returning plausible JSON."""
    cap = {}
    _serve(monkeypatch, PAYLOAD, cap)
    llm_client.complete_detailed(_cfg(), "YOU ARE THE RISK LAYER", "signal",
                                 max_tokens=100)
    msgs = cap["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "YOU ARE THE RISK LAYER"}
    assert msgs[1]["role"] == "user"
    assert cap["url"].endswith("/v1/chat/completions")
    assert cap["timeout"] == 30


def test_the_configured_timeout_reaches_the_request(monkeypatch):
    """An unbounded judge call is the failure timeout_seconds exists to
    prevent; a new transport must not quietly drop it."""
    cap = {}
    _serve(monkeypatch, PAYLOAD, cap)
    llm_client.complete_detailed(_cfg(timeout_seconds=7), "s", "u",
                                 max_tokens=10)
    assert cap["timeout"] == 7


# ---- the shipped config is unchanged --------------------------------------

def test_switching_provider_invalidates_the_judge_calibration():
    """Was `assert provider == "anthropic"` until 2026-08-28 — a pin on NOT
    having switched yet. Its docstring named what a legitimate switch owes:
    "a context_version bump and a refit, not a config edit nobody reviewed."
    The switch happened, so the pin became a coupling check: whatever the
    provider is, the calibration must not silently claim to describe it.

    From 2026-08-28 to 2026-11-04 the shipped calibration was the v1 fit
    (Sonnet, mean_scale 0.595 over 250 buys) and this asserted the MISMATCH
    was there and declared. The refit on 2026-11-04 (round-two plan, B2) is
    the other legitimate state, and it owes something specific: the fit must
    be stamped with the CURRENT context_version AND counted only rows judged
    since the local model took over — `calibrate_judge.py --since`, default
    2026-08-28. A matching version over a window that reaches back into the
    Sonnet era would pool two judges and look exactly right.
    """
    import json as _json

    import yaml
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load(open(root / "config.yaml"))
    jm = cfg["backtest"]["judge_model"]
    with open(root / "knowledge" / "judge_calibration.json") as f:
        cal = _json.load(f)
    fitted = cal.get("fitted_context_version")

    if llm_client.needs_key(cfg):
        return  # a keyed vendor judge is what the fit describes; nothing owed

    if jm["context_version"] != fitted:
        # The mismatch state: legitimate only while DECLARED.
        assert jm["calibration_refit_pending"] is True
        return

    switched = "2026-08-28"   # provider: local, context_version 2 -> 3
    assert cal.get("since") and cal["since"] >= switched, (
        f"the calibration claims version {fitted!r} (the local judge) but "
        f"was counted from {cal.get('since')!r} — rows judged by the vendor "
        f"model before {switched} may be pooled into it")
    assert cal["observed_from"] >= switched, cal["observed_from"]
    assert jm["calibration_refit_pending"] is False


# ---- 5. preflight, driven for real ----------------------------------------
#
# ADDED AFTER A MUTATION SURVIVED. Everything above tests `llm_client`. When
# the guard in preflight was replaced with `if False:` — making it demand an
# API key from a provider that authenticates with none — all thirteen tests
# above still passed, because not one of them called preflight.
#
# That is the same failure this session has now found four times: asserting a
# helper WORKS without asserting its consumer USES it. `needs_key()` being
# correct is worth nothing if the caller that gates the trading cycle ignores
# it. Preflight fails CLOSED, so the bug it would have caused is the expensive
# direction: the cycle refusing to run at all, with a message naming a key
# that cannot exist.

import preflight  # noqa: E402


def _full_cfg(monkeypatch, tmp_path, **llm_over):
    """The shipped config, pointed at a temp ledger, with llm overridden."""
    import yaml
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load(open(root / "config.yaml"))
    cfg["memory"]["ledger_path"] = str(tmp_path / "memory" / "ledger.jsonl")
    cfg["llm"].update(llm_over)
    monkeypatch.setenv("ALPACA_API_KEY", "k" * 24)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s" * 24)
    return cfg


def test_preflight_does_not_demand_a_key_from_a_keyless_provider(
        monkeypatch, tmp_path):
    """THE ONE THE MUTATION FOUND MISSING.

    With no ANTHROPIC_API_KEY set and a local provider configured, preflight
    must not fail the cycle. It fails CLOSED, so a spurious failure here does
    not merely warn — it stops the bot trading, citing a key that by
    definition cannot exist."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _full_cfg(monkeypatch, tmp_path, provider="local",
                    base_url="http://vllm-qwen38:8000/v1")
    fails = preflight.run(cfg)
    assert not any("ANTHROPIC_API_KEY" in f for f in fails), fails
    assert not any("is not set" in f for f in fails), fails


def test_preflight_still_demands_a_key_from_a_KEYED_provider(
        monkeypatch, tmp_path):
    """The guard must not have disabled the check for everyone."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _full_cfg(monkeypatch, tmp_path, provider="anthropic")
    fails = preflight.run(cfg)
    assert any("ANTHROPIC_API_KEY" in f and "is not set" in f for f in fails), fails


def test_preflight_refuses_a_keyless_provider_with_no_endpoint(
        monkeypatch, tmp_path):
    """Swapping one failure mode for another would be no gain. A keyless
    provider still has to say WHERE, and preflight is where that is caught —
    before the cycle, not on the first judge call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _full_cfg(monkeypatch, tmp_path, provider="local", base_url=None)
    fails = preflight.run(cfg)
    assert any("base_url" in f for f in fails), fails


def test_a_mangled_key_is_still_caught_for_a_keyed_provider(
        monkeypatch, tmp_path):
    """Present is not usable — the shape check must survive the refactor."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-short")
    cfg = _full_cfg(monkeypatch, tmp_path, provider="anthropic")
    assert any("ANTHROPIC_API_KEY" in f for f in preflight.run(cfg))
