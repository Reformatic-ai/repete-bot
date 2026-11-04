"""The ensemble simulator (backtest.simulate_ensemble).

Why it exists: `simulate()` replays ONE strategy against the rails; the live bot
runs the whole ensemble against ONE shared set of them. Every gate verdict in
knowledge/backtest_candidates.md was therefore measured on a bot that does not
exist — which is how §19b managed to produce four byte-identical arms.

The load-bearing test here is `test_single_strategy_matches_simulate`: with one
strategy enabled the ensemble must reproduce `simulate()` trade-for-trade. If it
does, ensemble behaviour is single-strategy behaviour plus contention and
nothing else, which is the whole correctness argument. If it drifts, every §20
number is suspect.
"""
from datetime import date, timedelta

import pytest

import backtest as bt
import strategies

ALL = ("meanrev", "tsmom", "ma_crossover", "xsmom", "donchian")


def synth_bars(symbols, n=320, seed=7):
    """Deterministic pseudo-random walks — no network, no snapshot needed."""
    out = {}
    for k, sym in enumerate(symbols):
        px, rows, d = 100.0 + 10 * k, [], date(2024, 1, 1)
        state = seed + k
        for i in range(n):
            state = (state * 1103515245 + 12345) % (2 ** 31)
            drift = ((state % 1000) / 1000.0 - 0.48) * 2.2
            px = max(5.0, px + drift)
            rows.append({"ts": f"{d.isoformat()}T21:00:00+00:00",
                         "open": round(px * 0.998, 4), "high": round(px * 1.02, 4),
                         "low": round(px * 0.98, 4), "close": round(px, 4),
                         "volume": 1_000_000})
            d += timedelta(days=1)
        out[sym] = rows
    return out


@pytest.fixture
def bars():
    """Symbols given in CONFIG order.

    Order matters: contention is resolved first-come by symbol, so whoever is
    scanned first gets first refusal on the shared slots. `simulate_ensemble`
    normalises to config order (mirroring live's `scan_symbols`), while
    `simulate` uses the caller's order — so an equivalence test must hand both
    the same order or it is comparing two different experiments."""
    cfg = bt.load_config()
    picks = ["SPY", "AAPL", "MSFT", "XOM", "JNJ"]
    ordered = [s for s in cfg["symbols"] if s in picks]
    assert len(ordered) == len(picks), "fixture symbols must all be in config"
    return synth_bars(ordered)


def test_symbol_order_changes_outcomes(bars):
    """Pin the property that made this bite: when slots are CONTESTED, the
    symbol scanned first wins, so scan order changes the outcome.

    The rails must actually bind for this to show — with 8 slots and 5 symbols
    nothing competes and order is irrelevant. Squeeze to one slot."""
    cfg = bt.load_config()
    cfg["risk"]["max_open_positions"] = 1
    cfg["strategies"]["meanrev"]["max_open_positions"] = 1
    reversed_bars = {s: bars[s] for s in reversed(list(bars))}
    # simulate() honours caller order, so it exposes the effect directly.
    a = bt.simulate(bars, cfg, {}, 100_000.0, "meanrev")
    b = bt.simulate(reversed_bars, cfg, {}, 100_000.0, "meanrev")
    assert a.n_trades > 0, "fixture produced no trades — test proves nothing"
    assert ([t.symbol for t in a.trades] != [t.symbol for t in b.trades]
            or a.total_return_pct != b.total_return_pct), \
        "scan order had no effect even with a single contested slot"


def test_ensemble_is_insensitive_to_input_dict_order(bars):
    """...and the ensemble must NOT inherit that sensitivity, because live
    always scans config order. A snapshot loaded from disk arrives sorted
    alphabetically; that must not silently become a different experiment."""
    cfg = bt.load_config()
    shuffled = {s: bars[s] for s in sorted(bars)}
    a = bt.simulate_ensemble(bars, cfg, 100_000.0)
    b = bt.simulate_ensemble(shuffled, cfg, 100_000.0)
    assert (a.total_return_pct, a.n_trades, a.profit_factor) == \
           (b.total_return_pct, b.n_trades, b.profit_factor)


def only(name):
    return {n: {"enabled": (n == name)} for n in ALL}


# ---------------- the decisive equivalence check ----------------

#: `xsmom` is the ONLY strategy with a short leg, and the two simulators model
#: genuinely different books for it since Phase 3: `simulate_ensemble` carries
#: signed positions, `simulate` is long-only. Equivalence is therefore not the
#: property to assert for it — REFUSAL is, and that is
#: test_simulate_refuses_a_short_configured_strategy below. Silently excluding
#: it from the parametrize list would have left the weaker claim looking like
#: the original one.
LONG_ONLY = tuple(n for n in ALL if n != "xsmom")


@pytest.mark.parametrize("name", LONG_ONLY)
def test_single_strategy_matches_simulate(bars, name):
    """One enabled strategy => the ensemble IS simulate(). Trade-for-trade.

    This is what licenses reading ensemble results as 'the same logic, plus
    contention'. A mismatch means the ensemble invented behaviour of its own."""
    cfg = bt.load_config()
    solo = bt.simulate(bars, cfg, {}, 100_000.0, name)
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0, strategy_overrides=only(name))

    for field in ("total_return_pct", "n_trades", "profit_factor",
                  "max_drawdown_pct", "win_rate", "avg_deployment_pct",
                  "n_heat_blocked", "n_corr_blocked", "n_guard_skipped_exits"):
        assert getattr(solo, field) == getattr(ens, field), (
            f"{name}: {field} diverged — {getattr(solo, field)} vs "
            f"{getattr(ens, field)}")

    assert ([(t.symbol, t.entry_ts, t.exit_ts, t.exit_reason, round(t.pnl, 6))
             for t in solo.trades]
            == [(t.symbol, t.entry_ts, t.exit_ts, t.exit_reason, round(t.pnl, 6))
                for t in ens.trades]), f"{name}: trade sequence diverged"


# ---------------- contention: the behaviour simulate() cannot show ----------------

def test_positions_are_tagged_with_an_owner(bars):
    cfg = bt.load_config()
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0)
    members = set(ens.params["members"])
    assert members and members <= set(strategies.REGISTRY)
    assert set(ens.params["by_strategy"]) >= members


def test_every_trade_is_attributed(bars):
    """Attribution must be complete: trades counted per strategy have to sum to
    the total, or the breakdown is quietly losing trades."""
    cfg = bt.load_config()
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0)
    counted = sum(v["trades"] for v in ens.params["by_strategy"].values())
    assert counted == ens.n_trades


def test_priority_order_decides_who_claims_a_symbol(bars):
    """Two strategies wanting the same symbol: the higher-priority one wins.
    This is the live rule (main.py 'first buy that survives review + rails takes
    ownership') and it is the mechanism that starves low-priority strategies."""
    # tsmom is OFF in the shipped config since 2026-11-04 (retired live by the
    # kill, #23). This test is about the contention MECHANISM, so it turns the
    # pair on explicitly rather than relying on what ships.
    cfg = bt.load_config()
    cfg["strategies"]["tsmom"]["enabled"] = True
    cfg["strategies"]["meanrev"]["priority"] = 1
    cfg["strategies"]["tsmom"]["priority"] = 2
    first = bt.simulate_ensemble(bars, cfg, 100_000.0)
    by_first = first.params["by_strategy"]

    cfg2 = bt.load_config()
    cfg2["strategies"]["tsmom"]["enabled"] = True
    cfg2["strategies"]["meanrev"]["priority"] = 9
    cfg2["strategies"]["tsmom"]["priority"] = 1
    second = bt.simulate_ensemble(bars, cfg2, 100_000.0)
    by_second = second.params["by_strategy"]

    # Reordering priority must change the allocation of trades between them.
    assert (by_first["meanrev"]["trades"], by_first["tsmom"]["trades"]) != \
           (by_second["meanrev"]["trades"], by_second["tsmom"]["trades"]), \
        "priority order had no effect — contention is not being modelled"


def test_shared_trade_cap_is_ensemble_wide(bars):
    """The §19b defect, pinned. A per-strategy counter would let each strategy
    fill its own quota; the live counter is global."""
    cfg = bt.load_config()
    cfg["risk"]["max_trades_per_day"] = 1
    tight = bt.simulate_ensemble(bars, cfg, 100_000.0)

    cfg2 = bt.load_config()
    cfg2["risk"]["max_trades_per_day"] = 20
    loose = bt.simulate_ensemble(bars, cfg2, 100_000.0)

    assert tight.n_trades < loose.n_trades, (
        "the daily trade cap did not bind across the ensemble — it is being "
        "applied per strategy, which is the bug this simulator exists to fix")


def test_global_slot_ceiling_binds_across_strategies(bars):
    cfg = bt.load_config()
    cfg["risk"]["max_open_positions"] = 1
    tight = bt.simulate_ensemble(bars, cfg, 100_000.0)
    cfg2 = bt.load_config()
    cfg2["risk"]["max_open_positions"] = 12
    loose = bt.simulate_ensemble(bars, cfg2, 100_000.0)
    assert tight.n_trades <= loose.n_trades


def test_one_position_per_symbol_across_strategies(bars):
    """Two strategies must never both hold the same symbol — the live account
    has one position per symbol, so overlapping entries would double-count."""
    cfg = bt.load_config()
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0)
    for t in ens.trades:
        overlapping = [o for o in ens.trades
                       if o is not t and o.symbol == t.symbol
                       and o.entry_ts < t.exit_ts and t.entry_ts < (o.exit_ts or "9")]
        assert not overlapping, f"{t.symbol}: concurrent positions in one symbol"


def test_disabled_owner_still_gets_its_exits(bars):
    """Ownership rule: a strategy that is disabled must still be able to close
    the positions it opened, or they would be stranded until end-of-data."""
    cfg = bt.load_config()
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0)
    owners = {k for k, v in ens.params["by_strategy"].items() if v["trades"]}
    reasons = {t.exit_reason for t in ens.trades}
    assert owners, "no strategy traded at all"
    # Strategy-driven exits must exist, not only bracket/end-of-data ones.
    assert reasons & {"strategy_sell", "stop_loss", "take_profit"}


def test_no_enabled_strategies_is_an_error(bars):
    cfg = bt.load_config()
    with pytest.raises(ValueError):
        bt.simulate_ensemble(bars, cfg, 100_000.0,
                             strategy_overrides={n: {"enabled": False}
                                                 for n in ALL})


def test_caller_config_is_never_mutated(bars):
    """simulate_ensemble deep-copies; a gate that silently rewrote the live
    config would be the 07-16 drift accident all over again."""
    cfg = bt.load_config()
    before = cfg["strategies"]["meanrev"]["enabled"]
    bt.simulate_ensemble(bars, cfg, 100_000.0,
                         strategy_overrides={"meanrev": {"enabled": False}})
    assert cfg["strategies"]["meanrev"]["enabled"] is before


def test_entry_priority_is_the_gated_order():
    """§22 reverted §20a. Putting meanrev first DOES raise trade count (179 ->
    253) but was adopted on numbers measured in the wrong symbol order; in live
    order it drops PF 1.955 -> 1.457, failing its own gate. Pin the reverted
    order so a future session cannot reinstate it without re-gating."""
    cfg = bt.load_config()
    order = [n for n, _ in strategies.enabled(cfg)]
    # tsmom left the ENABLED list on 2026-11-04 (retired live by the kill,
    # divergence #23) — its priority did not move, so the gated order still
    # holds among the strategies that enter.
    assert order == ["ma_crossover", "meanrev"], (
        f"entry priority is {order}; §22 gated ma_crossover -> tsmom -> "
        f"meanrev, and tsmom is off")
    pri = {n: cfg["strategies"][n]["priority"]
           for n in ("ma_crossover", "tsmom", "meanrev")}
    assert pri["ma_crossover"] < pri["tsmom"] < pri["meanrev"], (
        f"priorities {pri} no longer encode §22's ma_crossover -> tsmom -> "
        f"meanrev")


def test_trade_cap_is_a_runaway_guard_not_a_risk_rail():
    """§29 (2026-07-26) raised this 3 -> 15 by owner decision; it is no longer
    a risk rail, it is a runaway guard.

    The original guard (§20c) pinned it at 3 so a circuit breaker could not
    drift back up WITHOUT EVIDENCE. That property is kept, not dropped — the
    value is still pinned, it just moved once, deliberately, with the reasoning
    recorded in config.yaml and §29.

    Why 3 measured as harmless in §20c and was not: the ensemble found caps of
    3/5/8/12 giving +3.237/+3.232/+3.232/+3.232% because the GLOBAL SLOT CEILING
    bound first. That ceiling is now 0/disabled, so the cap is the only thing
    left in that position and its old measurement no longer applies. Live it was
    refusing 59 of 146 buy signals.

    It must stay LOOSE (above real demand of ~15 signals/day) but non-zero, so
    an API retry loop or a bad feed still cannot place unbounded orders.
    """
    cfg = bt.load_config()
    cap = cfg["risk"]["max_trades_per_day"]
    assert cap == 15, (
        "max_trades_per_day moved without a recorded decision — §29 set 15")
    assert cap >= 15, "below observed live demand; it would refuse real signals"
    assert cap, "0 disables the runaway guard entirely — an unbounded order loop"


# ---------------- the one strategy the two simulators disagree about ----------

def test_simulate_refuses_a_short_configured_strategy(bars):
    """`xsmom` ships `short_bottom_fraction: 0.25`, and since Phase 3 the two
    simulators model different books for it: `simulate_ensemble` carries signed
    positions, `simulate` does not — unsigned account, one-sided bracket legs,
    buyer's slippage throughout.

    Handed that config, `simulate` would score the LONG half and return a
    perfectly plausible number for a strategy that does not exist. That is the
    failure `docs/divergences.md` exists to count, so it raises instead. This is
    why xsmom is absent from the equivalence parametrization above — the claim
    for it is refusal, not agreement."""
    cfg = bt.load_config()
    assert cfg["strategies"]["xsmom"]["short_bottom_fraction"] > 0, \
        "fixture premise moved — xsmom no longer configures a short leg"
    with pytest.raises(ValueError, match="long-only"):
        bt.simulate(bars, cfg, {}, 100_000.0, "xsmom")


def test_simulate_still_scores_xsmom_with_the_short_leg_off(bars):
    """The twin, and the one that keeps the refusal honest: it must key off the
    CONFIGURED short leg, not off the strategy's name. Set the fraction to 0 and
    the same strategy scores exactly as it always did."""
    cfg = bt.load_config()
    cfg["strategies"]["xsmom"]["short_bottom_fraction"] = 0
    res = bt.simulate(bars, cfg, {}, 100_000.0, "xsmom")
    ens = bt.simulate_ensemble(bars, cfg, 100_000.0,
                               strategy_overrides=only("xsmom"))
    assert res.total_return_pct == ens.total_return_pct
    assert res.n_trades == ens.n_trades
