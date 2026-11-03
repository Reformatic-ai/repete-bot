"""Entry cost filter: skip an entry whose expected edge does not clear its cost.

Owner decision 2026-11-03 (§82), Connor relaying Peter: "Trade less often where
fees eat the edge: a filter that skips trades whose expected edge doesn't clear
the round-trip cost. Off by default."

OFF BY DEFAULT. The switch is the console's, not config.yaml's: the operator
settings file carries `costs.entry_filter.repete = {"on": bool, "multiple": 1..10}`
(cost_models.entry_filter). With it off this module is never consulted and the
cycle writes exactly the records it wrote before.

THE EDGE IS A TRACK RECORD, NOT A FORECAST. trade_plan.py refuses to write a
forecast, and §77 retired the edge claim, so the "expected edge" here is only a
description of what this strategy has done live so far:

    edge = mean(realised P&L / entry value) over this bot's CLOSED trades of the
           SAME strategy whose close was recorded strictly before this entry
           x this entry's value (qty x signal price)

Realised P&L is the outcome record's `pnl`: before operator costs, as the ledger
books it. A trade whose entry never filled (`entry_unfilled`, P&L 0 by
construction) is not a trade and is left out. Fewer than MIN_TRADES such trades
means no estimate (None), and the entry is allowed: the filter only stops a trade
it can judge (cost_models.edge_clears_cost). The repo has no registered per-strategy
backtest figure to fall back on (research verdicts are per arm), so there is no
second source.

The round trip is cost_models.round_trip_cost under the costs the bot is using this
cycle, on this entry's shares and value: the entry order and a close of the same size
and value. Alpaca paper charges no commission, so nothing is subtracted for a venue
fee.

Pure: no I/O. main.py ledgers the result; scripts/replay_cost_filter.py replays it
over history with the same functions, so the live rule and the replay cannot drift
apart.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import cost_models

BOT = "repete"
MARKET = "equities"
MIN_TRADES = 10
EDGE_RULE = (f"mean realised P&L (before operator costs) per dollar of entry "
             f"value over this strategy's closed trades that closed before the "
             f"entry, x the entry's value; at least {MIN_TRADES} trades, else "
             f"no estimate")


def _when(ts) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def track_record(closed_trades: list[dict], strategy: str | None,
                 before) -> tuple[float | None, int]:
    """(mean P&L per dollar of entry value, n) over `strategy`'s closed trades
    whose close was recorded strictly before `before` (a datetime or ISO string).
    Trades without a usable close time, entry value or P&L are left out, never
    guessed. (None, 0) when there are none."""
    cut = _when(before) if not isinstance(before, datetime) else before
    if cut is None or not strategy:
        return None, 0
    if cut.tzinfo is None:
        cut = cut.replace(tzinfo=timezone.utc)
    ratios = []
    for t in closed_trades:
        if t.get("strategy") != strategy:
            continue
        if t.get("exit_reason") == "entry_unfilled":
            continue
        closed_at = _when(t.get("exit_ts"))
        if closed_at is None or not closed_at < cut:
            continue
        try:
            value = abs(float(t["qty"]) * float(t["entry_price"]))
            pnl = float(t["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        if value <= 0 or not math.isfinite(pnl):
            continue
        ratios.append(pnl / value)
    if not ratios:
        return None, 0
    return sum(ratios) / len(ratios), len(ratios)


def expected_edge(closed_trades: list[dict], strategy: str | None,
                  entry_value: float, before) -> tuple[float | None, dict]:
    """(edge in dollars or None, basis). None below MIN_TRADES."""
    per_dollar, n = track_record(closed_trades, strategy, before)
    basis: dict = {"source": "live_track_record", "strategy": strategy, "n": n,
                   "min_trades": MIN_TRADES}
    if per_dollar is None or n < MIN_TRADES:
        basis["why_none"] = (f"{n} closed {strategy} trades before this entry, "
                             f"fewer than {MIN_TRADES}")
        return None, basis
    basis["per_dollar"] = round(per_dollar, 6)
    return round(per_dollar * abs(float(entry_value)), 4), basis


def evaluate(costs: dict, closed_trades: list[dict], strategy: str | None,
             qty: float, price: float, before) -> dict:
    """The filter's verdict on one entry, as the record main.py ledgers:
    {"on", "multiple", "edge", "round_trip", "clears", "edge_basis", "cost_basis"}.
    Raises on bad inputs; the caller fails OPEN on any exception."""
    flt = cost_models.entry_filter(costs, BOT)
    shares = abs(float(qty))
    value = shares * float(price)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"entry value {value!r} is not a positive number")
    edge, edge_basis = expected_edge(closed_trades, strategy, value, before)
    rt, cost_basis = cost_models.round_trip_cost(
        costs, MARKET, shares=shares, notional=value)
    multiple = float(flt["multiple"])
    return {"on": bool(flt["on"]), "multiple": multiple, "edge": edge,
            "round_trip": rt, "entry_value": round(value, 4),
            "clears": cost_models.edge_clears_cost(edge, rt, multiple),
            "edge_basis": edge_basis, "cost_basis": cost_basis}


def is_on(costs: dict | None) -> bool:
    """Is this bot's filter switched on in these costs? False on anything odd."""
    try:
        return bool(cost_models.entry_filter(costs or {}, BOT)["on"])
    except (AttributeError, KeyError, TypeError):
        return False


def describe(verdict: dict) -> str:
    """One line for the ledger's `detail` and the log."""
    edge = verdict.get("edge")
    edge_txt = "no estimate" if edge is None else f"${edge:,.2f}"
    return (f"cost filter: expected edge {edge_txt} does not clear "
            f"{verdict['multiple']:g} x round-trip cost "
            f"${verdict['round_trip']:,.2f} on ${verdict['entry_value']:,.2f} "
            f"({verdict['edge_basis'].get('n', 0)} closed "
            f"{verdict['edge_basis'].get('strategy')} trades)")
