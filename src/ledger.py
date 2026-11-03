"""Trade ledger — the audit trail. Every decision (including skips and
rejections) is appended as one JSON line with full reasoning.

This is the 'enterprise' documentation layer: append-only JSONL, one
record per decision, machine-parseable and human-readable. Outcomes are
written only AFTER a position closes (the 'outcome embargo' from the
research — memory must never see a result before it was observable).
"""
import json
from datetime import datetime, timezone

import uuid

import store as store_mod
from strategies.base import ENTRY_ACTIONS


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self.model_version: str | None = None  # set once per cycle (modelver)
        # Backend chosen once at startup (store.configure); JSONL by default.
        self._store = store_mod.open_store(path)
        # The prompt sidecar is opened LAZILY, on the first write, and by
        # nothing that reads. That is the property the whole design rests on:
        # all_records()/open_buys()/closed_trades() parse the entire ledger
        # stream, and closed_trades() runs once per signal inside the cycle
        # (main.py, via risk.live_kill_blocked). Prompt bodies at ~11 KB each
        # would make that ~132 MB/year of JSON on the hot path. Kept in a
        # separate stream, they cost the cycle nothing.
        self._prompts = None

    def set_model_version(self, version: str | None):
        """Stamp every subsequent record with the decision-surface
        fingerprint, so the track record segments by rulebook version."""
        self.model_version = version

    def _append(self, record: dict):
        record["ts"] = datetime.now(timezone.utc).isoformat()
        if self.model_version:
            record.setdefault("model_version", self.model_version)
        self._store.append(record)

    # ---- decision records ----

    def log_decision(self, symbol: str, action: str, reason: str,
                     indicators: dict, llm_review: dict | None,
                     executed: bool, detail: str = "", order: dict | None = None,
                     entry_price: float | None = None, qty: int | None = None,
                     regime: str | None = None, strategy: str | None = None,
                     entry_ts: str | None = None,
                     trade_plan: dict | None = None,
                     rail: str | None = None,
                     rail_vector: dict | None = None,
                     cost_filter: dict | None = None) -> str:
        trade_id = str(uuid.uuid4())[:8]
        # Pull the prompt record OUT of llm_review before it is written.
        # llm_review is read by review.py, dashboard.py, learn.py,
        # counterfactual.py and measured by calibrate_judge.py; a blob in it
        # is a key some reader will iterate. Hashes go top-level; bodies go
        # to the sidecar keyed by hash; neither goes into llm_review.
        #
        # Three states, deliberately distinguishable on disk:
        #   judged        -> prompt_sha256 is a hex string
        #   not judged    -> prompt_sha256 is None  (fallback's `_prompt: None`)
        #   pre-2026-08-22-> key absent entirely
        # A reader must never treat the third as the second.
        prompt_fields = {"prompt_sha256": None, "context_sha256": None,
                         "system_sha256": None, "prompt_chars": None,
                         # Which model ACTUALLY served the call, and its cost.
                         # `vendor_model` sits beside `model_version` (stamped
                         # in _append) and they are NOT the same thing:
                         # model_version is the rulebook fingerprint from
                         # modelver.py — config + risk + strategy source —
                         # and vendor_model is the LLM the vendor ran. Two
                         # similar names side by side is a misreading waiting
                         # to happen, hence this comment.
                         "vendor_model": None, "vendor_fell_back": None,
                         "input_tokens": None, "output_tokens": None,
                         "cost_usd": None}
        if llm_review is not None and "_prompt" in llm_review:
            llm_review = dict(llm_review)
            pr = llm_review.pop("_prompt")
            if pr:
                v = pr.get("vendor") or {}
                prompt_fields = {
                    "prompt_sha256": pr["prompt_sha256"],
                    "context_sha256": pr["context_sha256"],
                    "system_sha256": pr["system_sha256"],
                    "prompt_chars": pr["prompt_chars"],
                    "vendor_model": v.get("model"),
                    "vendor_fell_back": v.get("fell_back"),
                    "input_tokens": v.get("input_tokens"),
                    "output_tokens": v.get("output_tokens"),
                    "cost_usd": v.get("cost_usd"),
                }
                self._store_prompt(trade_id, pr)
        self._append({
            "type": "decision",
            "trade_id": trade_id,
            "regime": regime,              # market regime tag (learning loop)
            "strategy": strategy,          # which strategy produced the signal
            "symbol": symbol,
            "action": action,
            "strategy_reason": reason,
            "indicators": indicators,
            "llm_review": llm_review,      # verdict + reasoning from the judgment layer
            # What the judge was SENT, as hashes (2026-08-22, audit Gate 1d).
            # The bodies are in the prompt sidecar keyed by these; see
            # _store_prompt. None = a fallback verdict no model produced.
            **prompt_fields,
            "executed": executed,
            "detail": detail,              # e.g. risk-rejection reason
            # WHICH rail refused this, as a queryable key rather than prose
            # (2026-08-02). §40 made the same fix in the backtester's census on
            # 2026-07-29 — "the reason existed at the moment of the block and
            # was dropped" — but only there, so until now the simulator could
            # break blocks down by rail and the LIVE bot could not. Answering
            # "what stopped us trading this week" meant string-matching `detail`
            # against message text that is free to be reworded.
            # None on every non-rejection record, which is most of them.
            "rail": rail,
            # WHICH RAILS WOULD ALSO HAVE REFUSED — added 2026-08-22, audit
            # Phase 4 ("what did each guardrail return?"). `rail` above is the
            # BINDING rail, singular, because pure_checks raises on the first
            # failure and never evaluates the rest. That answers "what stopped
            # this trade" and can never answer "what else would have".
            #
            # ALONGSIDE, never replacing: 1,344 records carry only `rail`, and
            # dashboard.py and docs/runbooks.md read it. Same discipline as
            # alpha_pct and the prompt hashes.
            #
            # {rail: True|False|None} — None means the rail does not apply to
            # this action, never "checked and cleared". None for the whole
            # field on a non-rejection, or if the census itself failed.
            "rail_vector": rail_vector,
            "order": order,
            "entry_price": entry_price,
            "qty": qty,
            # The position's REAL fill time when it differs from this record's
            # write time (adopted positions). `ts` keeps its audit meaning —
            # when the record was appended — so age checks read entry_ts first.
            "entry_ts": entry_ts,
            # The bot's game plan at entry: thesis, expected hold, exit levels,
            # regime. Every field is read from a real signal/order/config value
            # (src/trade_plan.py) — never generated prose, never a forecast.
            "trade_plan": trade_plan,
            "outcome": None,               # embargoed until close — see close_trade()
            # The entry cost filter's verdict (§82, 2026-11-03): edge, round
            # trip, multiple and how each was worked out. Written ONLY when the
            # filter ran (it is off by default), so a record made with it off
            # is byte-identical to one made before the filter existed.
            **({"cost_filter": cost_filter} if cost_filter is not None else {}),
        })
        return trade_id

    def close_trade(self, trade_id: str, exit_price: float, pnl: float, pnl_pct: float,
                    exit_reason: str = "", benchmark_pnl_pct: float | None = None):
        """Record the outcome of a closed trade as a separate event (append-only).

        exit_reason: strategy_sell | stop_loss | take_profit | closed_order |
        last_price_estimate | entry_unfilled ("" in pre-bracket records).

        benchmark_pnl_pct: the benchmark's % move over the SAME holding window,
        or None when it could not be computed. `alpha_pct` is derived from it.

        `result` stays keyed to absolute P&L — it is what the broker did, and
        rewriting its meaning would silently reinterpret 564 existing records.
        Alpha is added ALONGSIDE it (2026-07-27) because a +3% trade in a +5%
        week is a win by that field and a miss by this one, and until now only
        the flattering half reached lessons.py.

        Absent benchmark => both new fields absent, never 0.0: "unknown" and
        "matched the market" must not collapse into the same number.
        """
        rec = {
            "type": "outcome",
            "trade_id": trade_id,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": round(pnl_pct, 3),
            "result": "win" if pnl > 0 else "loss",
            "exit_reason": exit_reason,
        }
        if benchmark_pnl_pct is not None:
            rec["benchmark_pnl_pct"] = round(benchmark_pnl_pct, 3)
            rec["alpha_pct"] = round(pnl_pct - benchmark_pnl_pct, 3)
        self._append(rec)

    def log_event(self, event: str, detail: str = ""):
        self._append({"type": "event", "event": event, "detail": detail})

    # Where the sidecar lives: next to the ledger, same backend. With the
    # sqlite backend store.stream_name() makes this its own stream for free.
    PROMPT_STREAM_SUFFIX = "decision_prompts.jsonl"

    def prompt_store_path(self) -> str:
        import os
        return os.path.join(os.path.dirname(self.path) or ".",
                            self.PROMPT_STREAM_SUFFIX)

    def _store_prompt(self, trade_id: str, pr: dict):
        """Write the prompt bodies the ledger row only hashes.

        Content-addressed: one `context` body per context_sha256, one `user`
        body per prompt_sha256, both joined to the decision by trade_id. Within
        a cycle the context is identical across symbols, so the first decision
        writes the body and the rest write only the (cheap) join row.

        DISPLAY AND DIAGNOSIS ONLY — same rule as log_context_eviction.
        Nothing reads this back to make a trading decision, and nothing on the
        cycle path reads it at all. Best-effort: a sidecar write must never
        take down a decision record.
        """
        try:
            if self._prompts is None:
                self._prompts = store_mod.open_store(self.prompt_store_path())
                self._seen_contexts: set = set()
            if pr["context_sha256"] not in self._seen_contexts:
                self._prompts.append({
                    "type": "context", "context_sha256": pr["context_sha256"],
                    "chars": pr["context_chars"], "body": pr["_context"],
                    "ts": datetime.now(timezone.utc).isoformat()})
                self._seen_contexts.add(pr["context_sha256"])
            self._prompts.append({
                "type": "prompt", "trade_id": trade_id,
                "prompt_sha256": pr["prompt_sha256"],
                "system_sha256": pr["system_sha256"],
                "context_sha256": pr["context_sha256"],
                "user": pr["_user"],
                "ts": datetime.now(timezone.utc).isoformat()})
        except Exception as e:  # noqa: BLE001 — the audit trail is not a trading path
            import logging
            logging.getLogger("ledger").warning("prompt sidecar write failed: %s", e)

    def log_context_eviction(self, cap: int, assembled_chars: int,
                             blocks_lost: list, budget_overage: int = 0,
                             unbounded_blocks: list | None = None):
        """The judge did not see everything `context_for_llm` built for it.

        Structured rather than a `log_event` prose string, for §40's stated
        reason — "a queryable key rather than prose" — because the questions
        this answers are *which block* and *how often*, and neither survives a
        sentence.

        The record exists because its absence cost months. `context_for_llm`
        ended in a bare `ctx[:cap]`: it returned happily, nothing marked the
        prompt, and the only symptom was worse judgments. Measured 2026-08-11,
        5,613 chars assembled against a 4,000 cap, with NEWS MEMORY, YOUR LAST
        RESOLVED CALLS, YOUR RECENT CALIBRATION and CURRENT REGIME dropped
        entirely on every call. §61.

        DISPLAY AND DIAGNOSIS ONLY. Nothing reads this back to make a trading
        decision and nothing should — it is a photograph of a prompt, the same
        standing `log_positions_mark` has.
        """
        self._append({
            "type": "event",
            "event": "context_evicted",
            "detail": (f"{assembled_chars - cap} chars dropped from the judge "
                       f"prompt; lost: {', '.join(blocks_lost) or 'none named'}"),
            "cap": cap,
            "assembled_chars": assembled_chars,
            "dropped_chars": assembled_chars - cap,
            "blocks_lost": blocks_lost,
            "budget_overage": budget_overage,
            "unbounded_blocks": unbounded_blocks or [],
        })

    def log_fill_quality(self, trade_id: str, symbol: str, side: str,
                         signal_price: float, filled_avg_price: float,
                         slippage_bps: float):
        """Measured execution cost for one fill: signal price (the close the
        decision was made on) vs the broker's actual average fill. Positive
        slippage_bps = the fill was worse than the signal price."""
        self._append({
            "type": "fill_quality",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "signal_price": signal_price,
            "filled_avg_price": filled_avg_price,
            "slippage_bps": round(slippage_bps, 2),
        })

    def log_positions_mark(self, positions: dict):
        """Snapshot of what the open book is worth RIGHT NOW, from the broker.

        DISPLAY ONLY. Invariant #4 says positions and equity for a trading
        decision are read fresh from the broker every cycle — this record is a
        photograph for the dashboard, never an input to sizing, signals or the
        judge. Reading it back to make a decision would reintroduce exactly the
        stale-state bug the invariant exists to prevent.

        Stored RAW (the broker's own qty / avg_entry / market_value /
        unrealized_pl) rather than pre-computed percentages, so the record stays
        lossless and the presentation layer can change its mind later.

        Written at every touchpoint that already holds a broker — 09:35, 15:45
        and 16:20 ET — so `dashboard.html`, a static file published to GitHub
        Pages, can show current value without needing keys or a network call at
        render time.
        """
        if not positions:
            return
        self._append({
            "type": "event",
            "event": "positions_mark",
            "detail": json.dumps({
                sym: {"qty": p.get("qty"),
                      "avg_entry": p.get("avg_entry"),
                      "market_value": p.get("market_value"),
                      "unrealized_pl": p.get("unrealized_pl")}
                for sym, p in sorted(positions.items())
            }, separators=(",", ":")),
        })

    # ---- reads ----

    def all_records(self) -> list[dict]:
        return self._store.read_all()

    def open_buys(self) -> dict:
        """trade_id -> decision record for executed ENTRIES with no outcome yet.

        ENTRY_ACTIONS, not `== "buy"`. This is the ONLY path by which the live
        bot reloads its open book from persisted state (main.py reads it at
        cycle start, and reconcile/adopt both key off it), so on the old filter
        a restart would have silently dropped every open SHORT: the position
        would still exist at the broker, but the bot would hold no record of
        it — no owner to route its exit, no stop-risk in the heat cap, and
        `adopt_untracked_positions` would then re-adopt it as a fresh trade
        with a fabricated entry price.

        The NAME is deliberately unchanged. It is now imprecise (it returns
        shorts too), but renaming it would rewrite ~25 call sites across six
        test files and three read-only mirrors inside the largest single
        commit this project has made — churn, in the one commit where diff
        size is itself a risk. Flagged for a follow-up rename to
        `open_entries()`; do not read the name as evidence that shorts are
        excluded."""
        records = self.all_records()
        closed = {r["trade_id"] for r in records if r["type"] == "outcome"}
        return {r["trade_id"]: r for r in records
                if r["type"] == "decision" and r["action"] in ENTRY_ACTIONS
                and r["executed"] and r["trade_id"] not in closed}

    def closed_trades(self) -> list[dict]:
        """Decisions joined with their outcomes, oldest first."""
        records = self.all_records()
        decisions = {r["trade_id"]: r for r in records if r["type"] == "decision"}
        out = []
        for r in records:
            if r["type"] == "outcome" and r["trade_id"] in decisions:
                merged = dict(decisions[r["trade_id"]])
                merged.update({k: r[k] for k in ("exit_price", "pnl", "pnl_pct", "result")})
                # Absent, never 0.0 — see close_trade()'s docstring. Copied only
                # when present so a dashboard reading merged.get("alpha_pct")
                # can tell "no benchmark move was computable" from "matched it".
                if "benchmark_pnl_pct" in r:
                    merged["benchmark_pnl_pct"] = r["benchmark_pnl_pct"]
                if "alpha_pct" in r:
                    merged["alpha_pct"] = r["alpha_pct"]
                merged["exit_reason"] = r.get("exit_reason", "")
                merged["exit_ts"] = r.get("ts")   # when the outcome was recorded
                out.append(merged)
        return out
