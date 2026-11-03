#!/usr/bin/env python3
"""Replay the entry cost filter over the live record (§82, DIAGNOSTIC).

Owner decision 2026-11-03, Connor: "Replay it on each bot's history since Aug 20
and report how many trades it would have skipped and the net effect."

READ-ONLY. It reads a ledger file (a COPY of memory/ledger.jsonl, never the live
one while the bot runs) through the store's read-only reader, places nothing,
writes no ledger, fee or settings line, and prints or writes one JSON report.

For every executed entry whose decision was recorded at or after --since, it asks
the live filter's own functions (src/cost_filter.py, so the live rule and this
replay cannot drift apart) whether the entry would have cleared, using only the
trades that had CLOSED before that entry's decision was recorded: no lookahead.
Four cost scenarios, filter multiple 1.0:

  research_paper  cost_models.validate(RECOMMENDED), the costs the bot charges now,
                  at the sizes the bot actually traded
  flat_c9_paper   apply_preset(R, "flat_c9"): C$9 an order converted to US$ at the
                  preset's rate, plus 1.5% a side to convert Canadian dollars
  research_1k     as research_paper with every order scaled by k = 1000 / starting
  flat_c9_1k      equity (qty, value and P&L x k; a flat cost stays flat; the
                  per-share minimum and its 1% cap still apply)

Per scenario: entries considered, how many the filter would have skipped, how many
it could not estimate (fewer than cost_filter.MIN_TRADES earlier closed trades of
the strategy: allowed), how many skipped trades are still open (left out of the
net effect), the skipped closed trades' P&L before cost, their round-trip cost
(the entry order at its value plus the close at its exit value, under that
scenario), and

  net_effect = -(sum of the skipped closed trades' P&L after that cost)

positive = skipping them would have left the account better off. An entry that
never filled (`entry_unfilled`) is considered (the filter runs before the order
goes out) but moves nothing: it paid nothing and made nothing.

What this holds FIXED, and so cannot see: every other decision. A skipped entry
would have freed a slot, cash and heat for an entry the rails refused, and the
judge's and the rails' later choices would have differed. This prices the trades
that happened; it does not re-run the bot.

    python scripts/replay_cost_filter.py --ledger <copy of ledger.jsonl> \\
        --out ~/scratch/net-pnl/replay-repete.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cost_filter                                        # noqa: E402
import cost_models                                        # noqa: E402
import store                                              # noqa: E402
from ledger import Ledger                                 # noqa: E402
from strategies.base import ENTRY_ACTIONS                 # noqa: E402

WINDOW_START = "2026-08-20T00:00:00Z"
MULTIPLE = 1.0
SCENARIOS = ("research_paper", "flat_c9_paper", "research_1k", "flat_c9_1k")


class ReadOnlyLedger(Ledger):
    """The live Ledger's own readers (all_records, closed_trades) over a store
    that cannot write: the same merge the cycle uses, and no way to append."""

    def __init__(self, path: str):                       # noqa: D107
        self.path = path
        self.model_version = None
        self._prompts = None
        self._store = store.read_only_reader(None, path)


def _when(ts: str) -> datetime:
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def scenario_costs() -> dict:
    """{name: (cost-models block with this bot's filter on at MULTIPLE, scaled?)}"""
    research = cost_models.validate(cost_models.RECOMMENDED)
    flat_c9, _kept = cost_models.apply_preset(research, "flat_c9")
    out = {}
    for name, block, scaled in (("research_paper", research, False),
                                ("flat_c9_paper", flat_c9, False),
                                ("research_1k", research, True),
                                ("flat_c9_1k", flat_c9, True)):
        b = {m: dict(block[m]) for m in cost_models.MARKETS}
        b["entry_filter"] = {cost_filter.BOT: {"on": True, "multiple": MULTIPLE}}
        out[name] = (cost_models.validate(b), scaled)
    return out


def _order_cost(costs: dict, shares: float, price: float) -> float:
    cost, _ = cost_models.order_cost(costs, cost_filter.MARKET, shares=shares,
                                     notional=abs(shares) * price)
    return cost


def replay(records: list[dict], closed: list[dict], since: str,
           starting_equity: float) -> dict:
    """The report, from a ledger's records and its closed trades. Pure."""
    cut = _when(since)
    outcomes = {r["trade_id"]: r for r in records if r.get("type") == "outcome"}
    entries = [r for r in records
               if r.get("type") == "decision" and r.get("executed")
               and r.get("action") in ENTRY_ACTIONS and _when(r["ts"]) >= cut]
    k_1k = 1000.0 / float(starting_equity)
    scen = {}
    for name, (costs, scaled) in scenario_costs().items():
        k = k_1k if scaled else 1.0
        s = {"considered": 0, "skipped": 0, "no_estimate": 0, "open_excluded": 0,
             "skipped_pnl_before_cost": 0.0, "skipped_round_trip_cost": 0.0,
             "net_effect": 0.0}
        for e in entries:
            qty = abs(float(e["qty"])) * k
            price = float(e["entry_price"])
            v = cost_filter.evaluate(costs, closed, e.get("strategy"), qty,
                                     price, _when(e["ts"]))
            s["considered"] += 1
            if v["edge"] is None:
                s["no_estimate"] += 1
            if v["clears"]:
                continue
            s["skipped"] += 1
            out = outcomes.get(e["trade_id"])
            if out is None:
                s["open_excluded"] += 1
                continue
            if out.get("exit_reason") == "entry_unfilled":
                continue                    # never filled: no P&L, no cost
            pnl = float(out["pnl"]) * k
            rt = (_order_cost(costs, qty, price)
                  + _order_cost(costs, qty, float(out["exit_price"])))
            s["skipped_pnl_before_cost"] += pnl
            s["skipped_round_trip_cost"] += rt
            s["net_effect"] -= pnl - rt
        for key in ("skipped_pnl_before_cost", "skipped_round_trip_cost",
                    "net_effect"):
            s[key] = round(s[key], 2)
        scen[name] = s
    n_open = sum(1 for e in entries if e["trade_id"] not in outcomes)
    n_unfilled = sum(1 for e in entries
                     if (outcomes.get(e["trade_id"]) or {}).get("exit_reason")
                     == "entry_unfilled")
    return {"bot": cost_filter.BOT, "window_start": since,
            "entries": len(entries), "edge_rule": cost_filter.EDGE_RULE,
            "scenarios": scen,
            "multiple": MULTIPLE, "min_trades": cost_filter.MIN_TRADES,
            "starting_equity": float(starting_equity), "k_1k": k_1k,
            "entries_open": n_open, "entries_unfilled": n_unfilled,
            "cad_per_usd": cost_models.CAD_PER_USD}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ledger", required=True,
                    help="a COPY of memory/ledger.jsonl (read only)")
    ap.add_argument("--since", default=WINDOW_START)
    ap.add_argument("--starting-equity", type=float, default=None,
                    help="default: config.yaml reporting.starting_equity")
    ap.add_argument("--out", default=None, help="write the JSON here")
    a = ap.parse_args(argv)
    start = a.starting_equity
    if start is None:
        import yaml
        with open(os.path.join(ROOT, "config.yaml")) as f:
            start = float(yaml.safe_load(f)["reporting"]["starting_equity"])
    led = ReadOnlyLedger(a.ledger)
    report = replay(led.all_records(), led.closed_trades(), a.since, start)
    report["ledger_sha256"] = _sha256(a.ledger)
    text = json.dumps(report, indent=2, sort_keys=True)
    if a.out:
        with open(os.path.expanduser(a.out), "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
