"""The entry cost filter (§82, owner decision 2026-11-03), its replay, and the
"After trading costs" card.

Connor, 2026-11-03: "Trade less often where fees eat the edge: a filter that
skips trades whose expected edge doesn't clear the round-trip cost. Off by
default." and "Show P&L after trading costs."

Pinned in both directions (bot-prove-it): the filter skips what it should AND
lets through what it should; off changes nothing; a failure fails open.
"""
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import cost_filter
import cost_models
import dashboard
import main
import operator_settings as ops
from conftest import make_bars
from ledger import Ledger
from test_main_cycle import BUY_CLOSES, FakeCycleBroker

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import replay_cost_filter as rcf                          # noqa: E402

T0 = datetime(2026, 9, 1, 15, 45, tzinfo=timezone.utc)
RESEARCH = cost_models.validate(cost_models.RECOMMENDED)


def _on(costs=None, multiple=1.0):
    c = copy.deepcopy(costs or RESEARCH)
    c["entry_filter"] = cost_models.validate_filter(
        {"repete": {"on": True, "multiple": multiple}})
    return c


def _closed(n, pnl, strategy="meanrev", qty=10, entry=100.0, start=T0,
            exit_reason="strategy_sell"):
    """n closed trades, one a day, each worth qty x entry at entry."""
    return [{"trade_id": f"{strategy[:2]}{i}", "strategy": strategy,
             "qty": qty, "entry_price": entry, "pnl": pnl,
             "exit_reason": exit_reason,
             "exit_ts": (start + timedelta(days=i)).isoformat()}
            for i in range(n)]


# ------------------------------------------------------------ the edge rule

def test_the_edge_is_the_mean_per_dollar_times_this_entry_value():
    # per dollar: +5 on $1,000 = 0.005; this entry is worth $2,000 -> $10.
    trades = _closed(10, 5.0)
    edge, basis = cost_filter.expected_edge(trades, "meanrev", 2000.0,
                                            T0 + timedelta(days=30))
    assert edge == pytest.approx(10.0)
    assert basis["n"] == 10 and basis["per_dollar"] == pytest.approx(0.005)


def test_nine_trades_is_no_estimate_and_ten_is_one():
    later = T0 + timedelta(days=30)
    assert cost_filter.expected_edge(_closed(9, -5.0), "meanrev", 1000.0,
                                     later)[0] is None
    assert cost_filter.expected_edge(_closed(10, -5.0), "meanrev", 1000.0,
                                     later)[0] == pytest.approx(-5.0)


def test_only_trades_closed_strictly_before_the_entry_count():
    """No lookahead: a trade that closes at or after the entry is invisible."""
    trades = _closed(12, -5.0)                    # closes T0 .. T0+11d
    at = T0 + timedelta(days=10)                  # 10 closed strictly before
    _, n = cost_filter.track_record(trades, "meanrev", at)
    assert n == 10
    _, n = cost_filter.track_record(trades, "meanrev", at.isoformat())
    assert n == 10


def test_other_strategies_and_unfilled_entries_do_not_count():
    trades = (_closed(10, 50.0, strategy="tsmom")
              + _closed(10, 0.0, exit_reason="entry_unfilled")
              + _closed(3, -5.0))
    per, n = cost_filter.track_record(trades, "meanrev", T0 + timedelta(days=30))
    assert n == 3 and per == pytest.approx(-0.005)


def test_a_record_it_cannot_read_is_skipped_not_guessed():
    trades = _closed(10, 5.0) + [
        {"strategy": "meanrev", "qty": None, "entry_price": 1, "pnl": 1,
         "exit_ts": T0.isoformat()},
        {"strategy": "meanrev", "qty": 1, "entry_price": 1, "pnl": 1,
         "exit_ts": "not a time"}]
    _, n = cost_filter.track_record(trades, "meanrev", T0 + timedelta(days=30))
    assert n == 10


# --------------------------------------------------------- cost and verdict

def test_round_trip_is_two_orders_under_the_model_in_use():
    """100 shares at $50 under the research per-share model: US$1 an order."""
    v = cost_filter.evaluate(_on(), _closed(10, 5.0), "meanrev", 100, 50.0,
                             T0 + timedelta(days=30))
    assert v["round_trip"] == pytest.approx(2.0)
    assert v["entry_value"] == pytest.approx(5000.0)
    assert v["cost_basis"]["model"] == "per_share"


def test_the_conversion_is_in_the_round_trip():
    c = copy.deepcopy(RESEARCH)
    c["equities"]["fx_conversion_pct"] = 1.5
    v = cost_filter.evaluate(_on(c), _closed(10, 5.0), "meanrev", 100, 50.0,
                             T0 + timedelta(days=30))
    # each order: US$1 commission + 1.5% of US$5,000 = US$76.
    assert v["round_trip"] == pytest.approx(152.0)


@pytest.mark.parametrize("pnl,multiple,clears", [
    (0.4, 1.0, True),      # edge $0.40/$1,000 x $5,000 = $2.00 ... not > $2.00
    (0.5, 1.0, True),      # $2.50 > $2.00
    (0.5, 2.0, False),     # $2.50 is not > 2 x $2.00
    (-5.0, 1.0, False),
])
def test_the_edge_must_beat_multiple_times_the_round_trip(pnl, multiple, clears):
    v = cost_filter.evaluate(_on(multiple=multiple), _closed(10, pnl), "meanrev",
                             100, 50.0, T0 + timedelta(days=30))
    if pnl == 0.4:
        assert v["edge"] == pytest.approx(2.0)
        assert v["clears"] is False                 # equal is not "more than"
    else:
        assert v["clears"] is clears


def test_no_estimate_clears():
    v = cost_filter.evaluate(_on(), _closed(3, -500.0), "meanrev", 100, 50.0,
                             T0 + timedelta(days=30))
    assert v["edge"] is None and v["clears"] is True
    assert "fewer than 10" in v["edge_basis"]["why_none"]


def test_is_on_reads_only_this_bots_switch():
    assert cost_filter.is_on(_on()) is True
    assert cost_filter.is_on(RESEARCH) is False
    other = copy.deepcopy(RESEARCH)
    other["entry_filter"] = cost_models.validate_filter(
        {"repete3": {"on": True, "multiple": 1.0}})
    assert cost_filter.is_on(other) is False
    assert cost_filter.is_on(None) is False
    assert cost_filter.is_on({"entry_filter": "junk"}) is False
    assert cost_filter.BOT == ops.BOT == "repete"


def test_the_settings_line_names_the_filter_only_when_it_is_on():
    off = ops.OperatorSettings(version=5, schema=2, costs=RESEARCH)
    on = ops.OperatorSettings(version=5, schema=2, costs=_on(multiple=2.0))
    assert "entry_filter" not in off.summary()
    assert on.summary() == off.summary() + " entry_filter=on x2"


# ------------------------------------------------------- through the cycle

V2 = {"equities": {"model": "per_share", "flat_per_order": 1.0,
                   "per_share": 0.005, "min_per_order": 1.0,
                   "max_pct_of_value": 1.0},
      "options": {"model": "per_contract", "flat_per_order": 1.4,
                  "per_contract": 0.65, "fees_per_contract": 0.025,
                  "min_per_order": 1.0},
      "fx": {"model": "per_10k_units", "flat_per_order": 1.0,
             "per_10k_units": 1.0, "spread_pips": 0.2},
      "crypto": {"model": "percent", "flat_per_order": 1.0, "pct_of_value": 0.8}}


def _settings(entry_filter=None):
    costs = copy.deepcopy(V2)
    if entry_filter is not None:
        costs["entry_filter"] = entry_filter
    return {"schema": 2, "version": 9, "frequency": "medium",
            "updated_at": "2026-11-03T14:00:00Z", "costs": costs,
            "fee_per_trade": 1.0, "fee_on_crypto": False, "fee_on_fx": False}


def _retarget(obj, old, new):
    if isinstance(obj, dict):
        return {k: _retarget(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_retarget(v, old, new) for v in obj]
    if isinstance(obj, str) and obj.startswith(old):
        return new + obj[len(old):]
    return obj


def _run(base, cfg, monkeypatch, name, settings, history=None, broker=None):
    """One real cycle in its own directory, with its own ledger and operator dir."""
    d = base / name
    (d / "op").mkdir(parents=True)
    c = _retarget(copy.deepcopy(cfg), str(base), str(d))
    c["risk"]["brackets"]["atr_period"] = 3
    monkeypatch.chdir(d)
    with open("config.yaml", "w") as f:
        yaml.safe_dump(c, f)
    monkeypatch.setenv("REPETE_OPERATOR_DIR", str(d / "op"))
    if settings is not None:
        (d / "op" / ops.SETTINGS_FILE).write_text(json.dumps(settings))
    led = Ledger(c["memory"]["ledger_path"])
    for i in range(history[0] if history else 0):
        tid = led.log_decision("ZZZ", "buy", "seed", {}, None, executed=True,
                               qty=10, entry_price=100.0,
                               strategy=history[2])
        led.close_trade(tid, 100.0 + history[1] / 10, history[1], history[1] / 10)
    b = broker or FakeCycleBroker(make_bars(BUY_CLOSES))
    monkeypatch.setattr(main, "Broker", lambda cfg_: b)
    main.run_cycle()
    return c, b, Ledger(c["memory"]["ledger_path"]).all_records()


def _normalised(records):
    """The ledger with what legitimately differs run to run taken out: write
    times, random trade ids, the cycle's wall-clock duration, and the
    model_version stamp (it hashes config.yaml, whose paths differ between the
    two directories here and nowhere else)."""
    tids = {r["trade_id"] for r in records if r.get("trade_id")}
    out = []
    for r in records:
        r = {k: v for k, v in r.items()
             if k not in ("ts", "trade_id", "model_version")}
        if r.get("event") == "cycle_timing":
            r.pop("detail", None)
        text = json.dumps(r, sort_keys=True)
        for t in tids:
            text = text.replace(t, "<tid>")
        out.append(text)
    return out


def test_off_writes_exactly_what_a_settings_file_without_a_filter_writes(
        tmp_path, cfg, monkeypatch):
    """OFF = UNCHANGED. The console wrote no `entry_filter` before 2026-11-03;
    a file that carries one switched off (and another bot's switched on) must
    produce the same ledger, line for line, and the filter must never run."""
    def _never(*a, **k):
        raise AssertionError("the cost filter ran while it was off")
    monkeypatch.setattr(cost_filter, "evaluate", _never)
    _, b0, before = _run(tmp_path, cfg, monkeypatch, "before", _settings())
    _, b1, after = _run(tmp_path, cfg, monkeypatch, "off", _settings(
        {"repete": {"on": False, "multiple": 4.0},
         "repete3": {"on": True, "multiple": 1.0}}))
    assert _normalised(after) == _normalised(before)
    assert len(b0.submitted) == len(b1.submitted) == 1
    assert not any("cost_filter" in json.dumps(r) for r in after)


def test_on_and_a_losing_record_skips_the_entry_and_ledgers_why(
        tmp_path, cfg, monkeypatch):
    c, b, recs = _run(tmp_path, cfg, monkeypatch, "skip",
                      _settings({"repete": {"on": True, "multiple": 1.0}}),
                      history=(10, -5.0, "ma_crossover"))
    assert b.submitted == []                      # nothing reached the broker
    [d] = [r for r in recs if r["type"] == "decision" and r["symbol"] == "SPY"
           and r["action"] == "buy"]
    assert d["executed"] is False and d["rail"] == "cost_filter"
    cf = d["cost_filter"]
    # 50 shares at $20 = $1,000; per dollar -5/1000 -> edge -$5; US$1 a side.
    assert cf["edge"] == pytest.approx(-5.0)
    assert cf["round_trip"] == pytest.approx(2.0)
    assert cf["multiple"] == 1.0 and cf["clears"] is False
    assert cf["edge_basis"]["n"] == 10
    assert cf["cost_basis"]["model"] == "per_share"
    assert d["detail"].startswith("cost filter: expected edge $-5.00")
    js = [json.loads(line) for line in open(c["learning"]["judgments_path"])]
    assert any(j.get("verdict") == "rails_reject" and j.get("kind") == "rails"
               for j in js)


def test_on_and_a_winning_record_trades_and_records_the_verdict(
        tmp_path, cfg, monkeypatch):
    _, b, recs = _run(tmp_path, cfg, monkeypatch, "clear",
                      _settings({"repete": {"on": True, "multiple": 1.0}}),
                      history=(10, 50.0, "ma_crossover"))
    assert len(b.submitted) == 1
    [d] = [r for r in recs if r["type"] == "decision" and r["symbol"] == "SPY"
           and r["action"] == "buy"]
    assert d["executed"] is True
    assert d["cost_filter"]["clears"] is True
    assert d["cost_filter"]["edge"] == pytest.approx(50.0)


def test_on_with_too_little_history_trades(tmp_path, cfg, monkeypatch):
    _, b, recs = _run(tmp_path, cfg, monkeypatch, "thin",
                      _settings({"repete": {"on": True, "multiple": 1.0}}),
                      history=(9, -5.0, "ma_crossover"))
    assert len(b.submitted) == 1
    [d] = [r for r in recs if r["type"] == "decision" and r["symbol"] == "SPY"
           and r["action"] == "buy"]
    assert d["executed"] is True and d["cost_filter"]["edge"] is None


def test_a_failure_while_estimating_fails_open_and_says_so(
        tmp_path, cfg, monkeypatch):
    def _boom(*a, **k):
        raise ValueError("estimate broke")
    monkeypatch.setattr(cost_filter, "evaluate", _boom)
    _, b, recs = _run(tmp_path, cfg, monkeypatch, "boom",
                      _settings({"repete": {"on": True, "multiple": 1.0}}),
                      history=(10, -5.0, "ma_crossover"))
    assert len(b.submitted) == 1                  # traded as before
    assert any(r.get("event") == "degradation"
               and r["detail"].startswith("cost_filter: could not estimate")
               for r in recs)


def _scan(monkeypatch, verdict):
    """One swing-scan pass (the second entry path, inert until §62) with the
    filter's verdict scripted: None = off."""
    import swing_scan
    from test_swing_scan import FakeBroker, etf_bars, in_zone_price, scan_cfg, wire
    cfg, bars = scan_cfg(), etf_bars()
    broker = FakeBroker({"XLE": in_zone_price(bars, cfg)})
    ledger, memory, _ = wire(monkeypatch, cfg, bars, broker)
    seen = []

    def _verdict(cfg_, ledger_, strategy, qty, price, symbol):
        seen.append((strategy, qty, price, symbol))
        return verdict
    monkeypatch.setattr(swing_scan.cycle, "_cost_filter_verdict", _verdict)
    swing_scan.run_scan()
    return broker, ledger, memory, seen


SKIP = {"on": True, "multiple": 1.0, "edge": -5.0, "round_trip": 2.0,
        "entry_value": 1000.0, "clears": False,
        "edge_basis": {"n": 12, "strategy": "swing_sectors"},
        "cost_basis": {"model": "per_share"}}


def test_the_swing_scan_skips_what_the_filter_refuses(monkeypatch):
    broker, ledger, memory, seen = _scan(monkeypatch, SKIP)
    assert broker.orders == []
    [d] = ledger.decisions
    assert d["executed"] is False and d["rail"] == "cost_filter"
    assert d["cost_filter"] == SKIP
    assert seen and seen[0][0] == "swing_sectors"
    assert memory.judgments.rows[-1][0][3] == "rails_reject"


def test_the_swing_scan_trades_when_the_filter_clears_or_is_off(monkeypatch):
    broker, ledger, _, _ = _scan(monkeypatch, dict(SKIP, clears=True, edge=50.0))
    assert len(broker.orders) == 1
    assert [d for d in ledger.decisions if d["executed"]][0]["cost_filter"][
        "clears"] is True
    broker, ledger, _, _ = _scan(monkeypatch, None)
    assert len(broker.orders) == 1
    assert all("cost_filter" not in d for d in ledger.decisions)


# ------------------------------------------------------------------ replay

def _replay_ledger(tmp_path):
    led = Ledger(str(tmp_path / "l.jsonl"))
    # 10 losing meanrev trades that closed before the window.
    for _ in range(10):
        t = led.log_decision("AAA", "buy", "seed", {}, None, executed=True,
                             qty=10, entry_price=100.0, strategy="meanrev")
        led.close_trade(t, 99.5, -5.0, -0.5)
    recs = led.all_records()
    for r in recs:
        r["ts"] = "2026-08-01T15:45:00+00:00"
    # Inside the window: a meanrev entry that lost $40, an open one, and an
    # ma_crossover entry with no history.
    extra = [
        {"type": "decision", "trade_id": "w1", "symbol": "BBB", "action": "buy",
         "executed": True, "qty": 50, "entry_price": 100.0, "strategy": "meanrev",
         "ts": "2026-08-21T15:45:00+00:00"},
        {"type": "outcome", "trade_id": "w1", "exit_price": 99.2, "pnl": -40.0,
         "pnl_pct": -0.8, "result": "loss", "exit_reason": "stop_loss",
         "ts": "2026-08-25T15:45:00+00:00"},
        {"type": "decision", "trade_id": "w2", "symbol": "CCC", "action": "buy",
         "executed": True, "qty": 50, "entry_price": 100.0, "strategy": "meanrev",
         "ts": "2026-08-26T15:45:00+00:00"},
        {"type": "decision", "trade_id": "w3", "symbol": "DDD", "action": "buy",
         "executed": True, "qty": 50, "entry_price": 100.0,
         "strategy": "ma_crossover", "ts": "2026-08-27T15:45:00+00:00"},
    ]
    path = tmp_path / "copy.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs + extra))
    return path


def test_the_replay_skips_counts_and_prices_without_lookahead(tmp_path):
    path = _replay_ledger(tmp_path)
    led = rcf.ReadOnlyLedger(str(path))
    rep = rcf.replay(led.all_records(), led.closed_trades(),
                     rcf.WINDOW_START, 100_000.0)
    assert rep["entries"] == 3 and rep["entries_open"] == 2
    s = rep["scenarios"]["research_paper"]
    assert (s["considered"], s["skipped"], s["no_estimate"],
            s["open_excluded"]) == (3, 2, 1, 1)
    # w1: lost $40; its two orders under per-share: US$1 each.
    assert s["skipped_pnl_before_cost"] == pytest.approx(-40.0)
    assert s["skipped_round_trip_cost"] == pytest.approx(2.0)
    assert s["net_effect"] == pytest.approx(42.0)
    # The 1k scenario scales P&L by 1000/100000.
    s1 = rep["scenarios"]["research_1k"]
    assert s1["skipped_pnl_before_cost"] == pytest.approx(-0.40)
    assert set(rep["scenarios"]) == set(rcf.SCENARIOS)
    assert set(s) == {"considered", "skipped", "no_estimate", "open_excluded",
                      "skipped_pnl_before_cost", "skipped_round_trip_cost",
                      "net_effect"}


def test_the_replay_never_sees_a_close_after_the_entry(tmp_path):
    """w1 closed on 08-25. If the replay leaked it into w2's record (08-26 is
    after), w2's estimate would rest on 11 trades, not 10. The 10 seeds are
    -0.5% each; w1 is -0.8%, so the per-dollar mean moves if it leaks."""
    path = _replay_ledger(tmp_path)
    led = rcf.ReadOnlyLedger(str(path))
    closed = led.closed_trades()
    w1_entry = datetime(2026, 8, 21, 15, 45, tzinfo=timezone.utc)
    w2_entry = datetime(2026, 8, 26, 15, 45, tzinfo=timezone.utc)
    assert cost_filter.track_record(closed, "meanrev", w1_entry)[1] == 10
    assert cost_filter.track_record(closed, "meanrev", w2_entry)[1] == 11


def test_the_replay_cli_writes_the_report_and_reads_only(tmp_path):
    path = _replay_ledger(tmp_path)
    before = path.read_bytes()
    out = tmp_path / "r.json"
    assert rcf.main(["--ledger", str(path), "--out", str(out),
                     "--starting-equity", "100000"]) == 0
    rep = json.loads(out.read_text())
    assert rep["bot"] == "repete" and rep["window_start"] == rcf.WINDOW_START
    assert rep["edge_rule"] == cost_filter.EDGE_RULE
    assert path.read_bytes() == before


def test_the_flat_c9_scenario_is_converted_into_us_dollars():
    costs = dict(rcf.scenario_costs())["flat_c9_paper"][0]
    assert costs["equities"]["model"] == "flat"
    assert costs["equities"]["flat_per_order"] == pytest.approx(
        round(9.0 / cost_models.CAD_PER_USD, 4))
    assert costs["equities"]["fx_conversion_pct"] == 1.5


# ---------------------------------------------- gross and net on the page

def _render(cfg, tmp_path, records=(), fees=()):
    led = Ledger(cfg["memory"]["ledger_path"])
    led.log_decision("SPY", "hold", "x", {}, None, executed=False)
    led.log_event("cycle_complete", json.dumps({"equity": 101_234.5}))
    opdir = os.environ["REPETE_OPERATOR_DIR"]
    with open(os.path.join(opdir, ops.FEES_FILE), "w") as f:
        for fee in fees:
            f.write(json.dumps(fee) + "\n")
    out = dashboard.render(cfg, out_path=str(tmp_path / "d.html"), spy_bars=[])
    page = open(out).read()
    feed = json.load(open(tmp_path / dashboard.DATA_PATH))
    return page, feed, led


def test_gross_costs_and_net_side_by_side(tmp_path, cfg):
    cfg.setdefault("reporting", {})["starting_equity"] = 100_000
    fees = [{"ts": "2026-10-27T13:36:07+00:00", "ref": "close:a", "fee": 9.0,
             "currency": "USD"},
            {"ts": "2026-10-28T19:46:26+00:00", "ref": "buy:b", "fee": 1.0,
             "currency": "USD"}]
    page, feed, led = _render(cfg, tmp_path, fees=fees)
    region = feed["regions"]["net_pnl"]
    first = led.all_records()[0]["ts"]
    since = datetime.fromisoformat(first).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    root = re.match(r'<div class="net-pnl"[^>]*>', region).group(0)
    assert root == (f'<div class="net-pnl" data-gross="1234.50" '
                    f'data-costs="10.00" data-net="1224.50" '
                    f'data-currency="USD" data-since="{since}">')
    assert "After trading costs" in region
    assert "gross P&amp;L (before trading costs)" in region
    assert "net P&amp;L (after trading costs)" in region
    assert "-$10.00" in region and "+$1,224.50" in region
    assert "charged since 2026-10-27" in region
    assert dashboard.NET_PNL_NOTE in region.replace("&#x27;", "'")
    assert "id=rgn-net_pnl" in page
    # Beside the Performance cards, not inside them.
    assert page.index("id=rgn-cards") < page.index("id=rgn-net_pnl") \
        < page.index("id=rgn-handoff")
    assert "net_pnl" not in feed["regions"]["cards"]


def test_no_costs_charged_reads_zero_not_blank(tmp_path, cfg):
    cfg.setdefault("reporting", {})["starting_equity"] = 100_000
    _, feed, _ = _render(cfg, tmp_path)
    region = feed["regions"]["net_pnl"]
    assert 'data-costs="0.00"' in region and 'data-net="1234.50"' in region
    assert "charged since" not in region


def test_a_loss_after_costs_is_signed_plainly():
    r = dashboard.net_pnl_region(-12.345, 1234.5, "2026-07-14T23:04:55Z")
    assert ('data-gross="-12.35" data-costs="1234.50" data-net="-1246.85"'
            in r)


def test_the_page_says_swing_and_keeps_the_old_name_once(tmp_path, cfg):
    page, _, _ = _render(cfg, tmp_path)
    assert "<title>Swing · repete</title>" in page
    assert re.search(r"<h1>Swing <span class=small>repete · \[PAPER\]", page)
    assert "<title>Repete</title>" not in page
    # The robot keeps its own name; identifiers and assets are unchanged.
    assert "Repete the trading robot" in page
    assert "__repete_after_swap" in page
