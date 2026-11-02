"""Pre-flight validation (2026-07-21, enterprise-hardening pass).

Runs before anything else in the cycle. Polarity is deliberately OPPOSITE the
data-outage convention: data failures degrade gracefully (a quiet morning),
but a misconfigured system must FAIL SAFE — a bot with a mangled risk block
or a corrupted ledger tail must not trade at all. Pure checks, no network.
"""
import json
import os

import strategies   # SECTORS_UNIVERSE — the universe names kept in one place

# Rails where zero is meaningless or dangerous, so the value must be positive.
# min_holding_days at 0 switches off the swing guard (invariant #3);
# daily_loss_limit_pct at 0 switches off the kill switch; either sizing
# percentage at 0 sizes every order to nothing.
REQUIRED_POSITIVE_RISK = (
    "risk_per_trade_pct", "max_position_pct",
    "daily_loss_limit_pct", "min_holding_days",
)

# Rails where 0 legitimately means "disabled" — matching how risk.py itself
# reads them:
#     risk.py:248   if r.get("max_order_value_usd"):        # 0 disables
#     risk.py:693   cfg["risk"].get("max_trades_per_day") or 0
# Still required to be PRESENT and non-negative: a missing key or a negative
# number is a config error; 0 is a deliberate choice.
#
# §29 (2026-07-26) set max_order_value_usd to 0 and taught risk.py and
# backtest.py to read it that way. Nobody told preflight, which still demanded
# a positive number — so from that commit onward every cycle aborted at
# main.py:500 and refused to trade. 861 tests stayed green because not one of
# them ran the SHIPPED config through preflight. That test exists now.
DISABLEABLE_RISK = ("max_order_value_usd", "max_trades_per_day")

# Rails expressed as a PERCENTAGE OF EQUITY. Every check above this line asks
# whether a number is present and non-negative; none of them asks whether it is
# survivable, so until 2026-08-02 `risk_per_trade_pct: 150` and
# `max_position_pct: 500` both passed preflight and shipped straight into the
# sizing arithmetic.
#
# THE BOUND IS DELIBERATELY LOOSE, and that is the design, not a compromise.
# This repo's owner runs values other people would call reckless on purpose and
# with evidence (§29 raised per-trade risk 1.0 -> 8.0 and moved the drawdown
# tolerance to 10pp, both recorded as owner decisions). Preflight has no
# business relitigating that. What it CAN say without an opinion is that a
# percentage of equity above 100 is not aggressive, it is incoherent: you
# cannot risk more than the account on one trade, hold more than the whole
# account in one name, or fall further than 100% from a peak. Those are
# arithmetic facts, not risk preferences.
#
# So the ceiling catches the failure that actually happens — a missing decimal
# point or a doubled keystroke (8.0 -> 80 is NOT caught; 8.0 -> 800 is) — while
# leaving every coherent value, however bold, to the owner.
PERCENT_OF_EQUITY_RAILS = (
    "risk_per_trade_pct", "max_position_pct", "daily_loss_limit_pct",
    "max_drawdown_pct", "max_portfolio_heat_pct",
)
PERCENT_CEILING = 100.0

REQUIRED_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")

def anthropic_key_shape_fail(value: str) -> str | None:
    """Why this string cannot be an Anthropic API key, or None if it plausibly is.

    The rule itself lives in `llm_client.key_shape_fail`, so preflight and the
    call path cannot disagree about what a key looks like. That is the §29
    lesson: preflight and risk.py read `max_order_value_usd: 0` differently and
    every cycle silently aborted for a day. One definition, two readers.

    Kept as a named function because it is the documented entry point and the
    tests written for it name it. `run()` does the provider-aware check.

    SHAPE ONLY — never a validity check. A revoked, rotated or simply wrong key
    passes; only the network can tell those apart, and preflight is pure by
    design (see the module docstring). Read a pass as "nothing was obviously
    mangled in transit", not "authenticated".

    2026-07-27: setting the key by hand produced FIVE `ANTHROPIC_API_KEY` lines
    in .env — empty, 16 chars, 148, 324 holding three prefixes, and a final 216
    holding two. python-dotenv takes the last, so the agent held two keys
    concatenated. Preflight said CLEAR TO TRADE, because it asked only whether
    the variable was set.
    """
    import llm_client
    return llm_client.key_shape_fail(value, llm_client.PROVIDERS["anthropic"])


def run(cfg: dict) -> list[str]:
    """All failures the config alone can convict (empty list = clear to trade).

    Pure by design (see the module docstring): no network, so nothing here
    can depend on the broker's own state. `run_account_checks` is the
    second, account-aware pass — see its docstring for why that could not
    live here and still be true to "pure checks, no network".
    """
    fails: list[str] = []

    r = cfg.get("risk")
    if not isinstance(r, dict):
        fails.append("risk block missing from config")
        r = {}
    for key in REQUIRED_POSITIVE_RISK:
        v = r.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            fails.append(f"risk.{key} missing or not a positive number ({v!r})")
    for key in DISABLEABLE_RISK:
        v = r.get(key)
        if not isinstance(v, (int, float)) or v < 0:
            fails.append(f"risk.{key} missing or negative ({v!r}) — "
                         f"use 0 to disable it, not a negative number")

    # Upper bounds. Only values that are PRESENT and numeric are checked: a
    # missing key is either already reported above (required rails) or a
    # legitimate omission (disableable ones), and reporting it twice would
    # bury the real fault in noise.
    for key in PERCENT_OF_EQUITY_RAILS:
        v = r.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) \
                and v > PERCENT_CEILING:
            fails.append(
                f"risk.{key} is {v!r}% of equity, which is not possible — "
                f"a percentage of equity cannot exceed {PERCENT_CEILING:g}. "
                f"Check for a missing decimal point or a doubled keystroke; "
                f"this rail would otherwise size orders off a number the "
                f"account cannot back")

    # The universe floor is a FRACTION, not a percentage, and the two are one
    # keystroke apart. `0.8` blocks entries below 80% of the requested
    # cross-section; `80` blocks every cycle ever, and `-1` can never fire.
    # Both mistakes read as a configured rail while being the opposite of one,
    # which is the failure mode this file exists for.
    v = r.get("min_universe_fraction")
    if v is not None and (not isinstance(v, (int, float))
                          or isinstance(v, bool) or v < 0 or v > 1):
        fails.append(
            f"risk.min_universe_fraction must be a fraction between 0 and 1 "
            f"({v!r}) — it is the share of REQUESTED symbols that must have "
            f"usable bars before entries are allowed, not a percentage. Above "
            f"1 blocks every cycle; below 0 can never fire. Use 0 to disable.")

    # Broker resilience. Not in `risk` because these bound how long the bot
    # waits and retries, not what it is allowed to trade.
    o = cfg.get("ops") or {}
    if isinstance(o, dict):
        for key, why in (
            ("broker_timeout_sec",
             "it is the socket timeout; 0 restores the 2026-08-05 hang"),
            ("broker_retry_attempts",
             "it is how many EXTRA attempts a failed read gets"),
            ("broker_retry_budget_sec",
             "it bounds total retry time per cycle against the 16:00 close"),
        ):
            v = o.get(key)
            if v is not None and (not isinstance(v, (int, float))
                                  or isinstance(v, bool) or v < 0):
                fails.append(
                    f"ops.{key} must be a non-negative number ({v!r}) — {why}")

    # The kill switch's recovery budget. A typo here does not stop the bot, but
    # it decides how long an AUTOMATIC LIQUIDATION keeps retrying after the
    # daily-loss rail fired. Worth failing loudly on rather than falling back
    # silently, because the fallback is invisible in a config file the operator
    # believes they have just tuned.
    ks = r.get("kill_switch")
    if ks is not None:
        if not isinstance(ks, dict):
            fails.append(f"risk.kill_switch must be a block, got {ks!r}")
        else:
            v = ks.get("max_attempts")
            if v is not None and (not isinstance(v, int)
                                  or isinstance(v, bool) or v <= 0):
                fails.append(
                    f"risk.kill_switch.max_attempts must be a positive int "
                    f"({v!r}) — it bounds how many times the bot will retry "
                    f"liquidating the book after a kill switch")
            v = ks.get("auto_retry_flatten")
            if v is not None and not isinstance(v, bool):
                fails.append(
                    f"risk.kill_switch.auto_retry_flatten must be true or "
                    f"false ({v!r}) — anything else reads as true, which is "
                    f"the side that liquidates")

    if cfg.get("mode", "paper") not in ("paper", "live"):
        fails.append(f"mode must be paper|live, got {cfg.get('mode')!r}")
    if (cfg.get("mode") == "live"
            and os.environ.get("LIVE_TRADING_CONFIRMED", "NO") != "YES"):
        # Broker enforces this too; surfacing it here makes the cycle log say
        # WHY it is still paper instead of silently downgrading.
        fails.append("mode: live without LIVE_TRADING_CONFIRMED=YES in env "
                     "(double interlock) — refusing to run half-configured")

    if cfg.get("strategy", {}).get("timeframe") != "1Day":
        fails.append("strategy.timeframe must stay 1Day (swing-only invariant)")

    # THE HEAT INVERSION TRAP (§41, 2026-07-28). portfolio_heat charges a
    # position with no recorded stop `equity x risk_per_trade_pct`, and the
    # heat check charges an entry with no candidate stop the same
    # (src/risk.py:626,633-634 and :845-847). Shipped, that fallback is 8.0%
    # of equity against a cap of 4.0% — double the ENTIRE budget, so any
    # stopless entry would be blocked unconditionally and every other entry
    # blocked behind it.
    #
    # Measured 2026-07-28 on 2022-2026: 0 of 244,920 entries had no stop and
    # 0 of 465 heat evaluations saw a stopless open position, because
    # brackets are on and unprotectable_entry (src/risk.py:355-392) refuses
    # the rest. So the inversion is LATENT, not live, and NO risk number was
    # changed on the strength of a diagnostic.
    #
    # What makes it latent is `brackets.enabled`. Turning brackets off would
    # arm it silently — the bot would simply stop entering, exactly the §40
    # failure shape. So the trap is closed here rather than left documented:
    # a documented check is not a check.
    #
    # CORRECTION 2026-09-28: "latent" was true of stopless ENTRIES only. A
    # stopless open POSITION has a second way in that this config check cannot
    # see: an entry written off as unfilled while still queued, which then
    # fills and comes back through adopt_untracked_positions with no stop.
    # That happened (META, Labor Day 2026-09-07 → adopted 09-08) and refused
    # every entry for three weeks. Closed at run time, not here: reconcile no
    # longer writes off a live order, adoption reads the resting broker leg,
    # main.attach_broker_stops repairs older records each cycle, and whatever
    # is still stopless is logged `heat_trap_armed` and fails health.py.
    heat_cap = r.get("max_portfolio_heat_pct") or 0
    per_trade = r.get("risk_per_trade_pct") or 0
    brackets_on = (r.get("brackets") or {}).get("enabled")
    if heat_cap and per_trade > heat_cap and not brackets_on:
        fails.append(
            f"risk.risk_per_trade_pct ({per_trade}) exceeds "
            f"risk.max_portfolio_heat_pct ({heat_cap}) while "
            f"risk.brackets.enabled is off — every stopless entry would be "
            f"blocked by the heat cap on its own fallback charge, and the bot "
            f"would stop trading without saying why. Raise the heat cap, lower "
            f"the per-trade risk, or leave brackets on.")

    # THE NET EXPOSURE INVERSION TRAP (Phase 1, 2026-08-04). risk.py:1150-1169
    # reads `net_exposure_pct.max` as a ceiling on buys and `.min` as a floor
    # on shorts — deliberately DIRECTIONAL, per the comment there, so that a
    # floor can never block every buy the way a two-sided band would. That
    # protection assumes `min < max`; an inverted band (e.g. `min: 120, max:
    # 80`) is a two-sided outage instead — DEMONSTRATED: with those values,
    # `projected > hi` refuses every buy that would move net above 80% and
    # `projected < lo` refuses every short that would move net below 120%,
    # which between them cover the entire number line either side of the
    # band's (nonsensical) interior. The key ships commented out in
    # config.yaml, so nothing today can hit this, but nothing would stop
    # someone from uncommenting a doubled or swapped value later either —
    # same shape as the heat inversion above, closed here rather than left
    # documented.
    band = r.get("net_exposure_pct")
    if band is not None:
        if not isinstance(band, dict):
            fails.append(f"risk.net_exposure_pct must be a block with min/max, "
                         f"got {band!r}")
        else:
            lo, hi = band.get("min"), band.get("max")
            for name, v in (("min", lo), ("max", hi)):
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    fails.append(
                        f"risk.net_exposure_pct.{name} missing or not a "
                        f"number ({v!r}) — both min and max are required "
                        f"once the block is present")
            if (isinstance(lo, (int, float)) and not isinstance(lo, bool)
                    and isinstance(hi, (int, float)) and not isinstance(hi, bool)
                    and lo >= hi):
                fails.append(
                    f"risk.net_exposure_pct.min ({lo}) is not below "
                    f".max ({hi}) — an inverted or equal band refuses both a "
                    f"buy toward the ceiling and a short toward the floor, "
                    f"which between them cover every order on either side of "
                    f"the band: a two-sided outage instead of a 130/30 band")

    # The judge is optional, but claiming to have one and not having one is a
    # misconfiguration — and a quiet one. llm.review_signal() returns a clean
    # `approve` at full size when the key is absent, so trades run UNJUDGED at
    # the size the rules asked for, and evidence.py reports every entry as
    # judged because an llm_review block is present either way. Set
    # llm.enabled: false to trade on rules alone; that is a decision, not a
    # silent hole.
    if cfg.get("llm", {}).get("enabled"):
        import llm_client
        provider = llm_client.provider_name(cfg)
        if provider not in llm_client.PROVIDERS:
            fails.append(f"llm.provider is {provider!r}, which llm_client does "
                         f"not implement — supported: "
                         f"{', '.join(sorted(llm_client.PROVIDERS))}")
        spec = llm_client.provider_spec(cfg)
        env_name = spec["key_env"]
        if not llm_client.needs_key(cfg):
            # A self-hosted provider authenticates with nothing, so there is no
            # key to demand. Refusing the cycle over an absent key would be the
            # inversion this file exists to prevent: failing SAFE means
            # refusing to trade when something is WRONG, and a keyless provider
            # with a reachable endpoint is not wrong.
            #
            # What replaces the key check is the endpoint check — a keyless
            # provider with no base_url has nowhere to send the prompt, and
            # guessing a default would send it somewhere nobody chose.
            if not (cfg.get("llm") or {}).get("base_url"):
                fails.append(
                    f"llm.provider is {llm_client.provider_name(cfg)!r}, which "
                    f"uses no API key, but llm.base_url is unset — the judge "
                    f"has no endpoint. Set llm.base_url or llm.enabled: false")
        elif not os.environ.get(env_name):
            fails.append(f"llm.enabled: true but {env_name} is not set — "
                         f"every trade would be approved unjudged at full size; "
                         f"set the key or set llm.enabled: false")
        else:
            # Present is not the same as usable. A mangled key fails at the
            # first API call, by which point the cycle has already traded.
            key = os.environ.get(env_name)
            shape = llm_client.key_shape_fail(key, spec)
            if shape:
                fails.append(f"{env_name} {shape}. The judge would fail on "
                             f"every call and each entry would be approved "
                             f"unjudged at full size. Fix the value in .env "
                             f"(one line, one key) or set llm.enabled: false")

        # A misspelled policy silently reverts to `approve` at runtime (the
        # cycle must not crash on a typo), which means an owner who set
        # `blok` believing they had stopped unsupervised trading would get
        # exactly the behaviour they were trying to switch off, with no
        # symptom. Reported here rather than left to be noticed in a ledger.
        raw_policy = cfg["llm"].get("on_unavailable")
        if raw_policy is not None and str(raw_policy).strip().lower() not in (
                "approve", "block"):
            fails.append(
                f"llm.on_unavailable is {raw_policy!r}, which is neither "
                f"'approve' nor 'block' — it would silently fall back to "
                f"'approve' and trade unjudged entries at full size")

        # An unbounded judge call inside a cycle is the 600s-SDK-default
        # failure this setting exists to prevent; a typo must not reinstate it
        # silently either.
        raw_timeout = cfg["llm"].get("timeout_seconds")
        if raw_timeout is not None:
            try:
                bad = float(raw_timeout) <= 0
            except (TypeError, ValueError):
                bad = True
            if bad:
                fails.append(
                    f"llm.timeout_seconds is {raw_timeout!r} — must be a "
                    f"positive number of seconds; it would fall back to the "
                    f"{llm_client.DEFAULT_TIMEOUT_SECONDS}s default")

    for key in REQUIRED_ENV:
        if not os.environ.get(key):
            fails.append(f"env {key} missing")

    # The regime block is indexed directly inside the cycle (main.run_cycle ->
    # regime.compute_regime), so a missing key or a nonsensical period is a
    # config error that belongs here rather than as a mid-cycle exception.
    rg = (cfg.get("learning") or {}).get("regime")
    if not isinstance(rg, dict):
        fails.append("learning.regime block missing from config")
    else:
        for key, floor in (("sma_period", 1), ("vol_period", 2)):
            v = rg.get(key)
            if not isinstance(v, int) or v < floor:
                fails.append(f"learning.regime.{key} must be an int >= {floor} "
                             f"({v!r}) — vol_period < 2 divides by zero")

    # ---- the sector map and the strategy universes that depend on it ----
    #
    # Every failure below is SILENT at runtime, which is why they are convicted
    # here: a malformed map does not raise, it quietly changes which names a
    # strategy may buy and how many of them the concentration cap allows.
    # Preflight's polarity is fail-safe, so each is a refusal to start.
    # `core_sectors:` (2026-11-02) feeds the same `sector_map`, so `seen` spans
    # both blocks: a name in each is as ambiguous as a name twice in one.
    seen: dict = {}
    for block in ("sectors", "core_sectors"):
        sectors = cfg.get(block)
        if sectors is None:
            continue
        if not isinstance(sectors, dict):
            fails.append(f"{block} must be a mapping of sector name -> symbol list")
            continue
        for sector, syms in sectors.items():
            if not isinstance(syms, list) or not syms:
                fails.append(f"{block}.{sector} must be a non-empty list")
                continue
            for s in syms:
                if s in seen:
                    # Ambiguous membership makes risk.sector_open_count
                    # answer differently depending on which lookup wins, so
                    # the concentration cap becomes evadable by construction.
                    fails.append(f"symbol {s} appears in two sectors "
                                 f"({seen[s]} and {sector})")
                seen[s] = sector

    # ---- the ETF list, and the strategies that subtract it ----
    #
    # Same polarity and the same failure mode as the sector map above: nothing
    # here raises at runtime. `excluded_etfs` subtracts a SET, so a name that is
    # misspelled, or that is not in `symbols:` at all, subtracts nothing and the
    # exclusion silently does not happen. The strategy keeps ranking the basket
    # it was supposed to stop ranking, with no log line and no failing test —
    # which is exactly how `short_bottom_fraction` shipped naming nothing.
    etfs = cfg.get("etfs")
    if etfs is not None:
        if not isinstance(etfs, list) or not etfs:
            fails.append("etfs must be a non-empty list of symbols")
        else:
            core = set(cfg.get("symbols") or ())
            for s in etfs:
                if s not in core:
                    fails.append(f"etfs lists {s}, which is not in symbols: — "
                                 f"the exclusion would silently match nothing")
            if len(set(etfs)) != len(etfs):
                fails.append("etfs contains a duplicate symbol")

    for sname, sparams in (cfg.get("strategies") or {}).items():
        if (sparams or {}).get("exclude_etfs") and not cfg.get("etfs"):
            # The mirror: opting in with no list to subtract is a strategy that
            # believes it excludes ETFs and does not. Convicted here rather than
            # left to read as "there simply are no ETFs in this universe".
            fails.append(f"strategies.{sname}.exclude_etfs is set but no etfs: "
                         f"list is configured — nothing would be excluded")

    # ---- the sector-ETF list, the `sector_etfs` universe ----
    #
    # DELIBERATELY NOT required to be inside `symbols:`. `symbols:` is the
    # CORE universe — the 38 names the three enabled strategies were gated on —
    # and the whole point of a per-strategy universe (see `sectors:` above) is
    # to add names WITHOUT widening it. Forcing membership here would make
    # adding one sector fund a live behaviour change to three gated strategies.
    #
    # What IS convicted is the silent-failure class: `universe_for` builds a
    # SET from this list, so a duplicate collapses without a sound, and a fund
    # that also appears in the stock `sectors:` map would be ranked as a peer
    # of its own constituents by `reclaim` — the §49 category error (a basket
    # is not a peer of the names it is made of) arriving through a config edit.
    setfs = cfg.get("sector_etfs")
    if setfs is not None:
        if not isinstance(setfs, list) or not setfs:
            fails.append("sector_etfs must be a non-empty list of symbols")
        else:
            if len(set(setfs)) != len(setfs):
                fails.append("sector_etfs contains a duplicate symbol")
            stock_sectors = {s for syms in (cfg.get("sectors") or {}).values()
                             for s in (syms or ())}
            for s in setfs:
                if s in stock_sectors:
                    fails.append(f"sector_etfs lists {s}, which also appears "
                                 f"in the sectors: stock map — a basket must "
                                 f"not be ranked as a peer of its own "
                                 f"constituents (§49)")

    known_universes = {None, strategies.SECTORS_UNIVERSE,
                       strategies.SECTOR_ETFS_UNIVERSE,
                       strategies.SPY_ONLY_UNIVERSE,
                       strategies.GEM_LEGS_UNIVERSE}
    for sname, sparams in (cfg.get("strategies") or {}).items():
        key = (sparams or {}).get("universe")
        if key not in known_universes:
            # universe_for() returns the EMPTY set for an unknown key, so a typo
            # does not widen a universe — it silently EMPTIES one, and the
            # strategy stops trading with nothing in the logs to say why.
            fails.append(f"strategies.{sname}.universe {key!r} is not a known "
                         f"universe — the strategy would silently trade nothing")
        if key == strategies.SECTORS_UNIVERSE and not cfg.get("sectors"):
            fails.append(f"strategies.{sname}.universe is "
                         f"'{strategies.SECTORS_UNIVERSE}' but no sectors: "
                         f"block is configured")
        if key == strategies.SECTOR_ETFS_UNIVERSE and not cfg.get("sector_etfs"):
            fails.append(f"strategies.{sname}.universe is "
                         f"'{strategies.SECTOR_ETFS_UNIVERSE}' but no "
                         f"sector_etfs: list is configured")
        if key == strategies.SPY_ONLY_UNIVERSE and not cfg.get("spy_only"):
            fails.append(f"strategies.{sname}.universe is "
                         f"'{strategies.SPY_ONLY_UNIVERSE}' but no "
                         f"spy_only: list is configured")
        if key == strategies.GEM_LEGS_UNIVERSE and not cfg.get("gem_legs"):
            fails.append(f"strategies.{sname}.universe is "
                         f"'{strategies.GEM_LEGS_UNIVERSE}' but no "
                         f"gem_legs: list is configured")

    scfg = (cfg.get("risk") or {}).get("sector_concentration")
    if isinstance(scfg, dict) and scfg.get("enabled"):
        cap = scfg.get("max_per_sector")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            # 0 or negative would refuse EVERY entry in a mapped sector — an
            # outage wearing a rail's name. bool is rejected explicitly because
            # True is an int and would otherwise pass as a cap of 1.
            fails.append(f"risk.sector_concentration.max_per_sector must be an "
                         f"int >= 1 ({cap!r}) — 0 or less blocks every entry "
                         f"in a mapped sector")

    ledger_path = cfg.get("memory", {}).get("ledger_path", "memory/ledger.jsonl")
    mem_dir = os.path.dirname(ledger_path) or "."
    if os.path.isdir(mem_dir) and not os.access(mem_dir, os.W_OK):
        fails.append(f"memory dir {mem_dir} not writable")
    fails.extend(_ledger_tail_fails(cfg, ledger_path))
    fails.extend(_state_path_fails(cfg))

    return fails


def _state_path_fails(cfg: dict) -> list[str]:
    """`memory.heartbeat_path` / `memory.halt_path` may only hold their
    defaults in a process that trades.

    These keys exist so a QA sweep can point `/healthz` at a fixture instead of
    at whatever sits next to the process (F-14, docs/qa_findings.md). They
    redirect the MONITORS — `health.status` and `watchdog.check` — and
    deliberately NOT the trading rail: `risk.check_halt()` keeps its own
    cwd-relative constant, because a config key able to move the kill switch is
    a way to disable it, and `scripts/halt.py:80` already settled that question
    ("a HALT engaged into some other directory is a stop button wired to
    nothing").

    That split is only safe if it cannot exist while trading. This is the
    enforcement: refuse the cycle outright rather than run one in which the
    health check and the kill switch are reading different files. Fail-safe
    polarity, per this module's docstring.
    """
    import statepaths
    mem = cfg.get("memory") or {}
    fails = []
    for key, default in (("heartbeat_path", statepaths.DEFAULT_HEARTBEAT_PATH),
                         ("halt_path", statepaths.DEFAULT_HALT_PATH)):
        value = mem.get(key)
        if value is not None and value != default:
            fails.append(
                f"memory.{key} is {value!r}, not the default {default!r} — "
                f"that key exists to point a health check at a QA fixture, and "
                f"a cycle must never run with the monitors watching a "
                f"different file than the kill switch")
    return fails


def run_account_checks(cfg: dict, account: dict) -> list[str]:
    """The second pre-flight pass: failures only the broker's own state can
    convict, checked once the account is actually known.

    `run(cfg)` cannot do this and stay true to its own module docstring
    ("Pure checks, no network") — a strategy configured to short is
    internally consistent config; whether the ACCOUNT it will trade on can
    short at all is a fact about the broker, not the file. Kept as a
    separate function rather than an optional parameter on `run()` so a
    caller cannot half-run this by forgetting to pass `account=`: main.py's
    2026-08 defect was exactly that — `preflight.run(cfg)` ran before
    `Broker(cfg)` even existed, so no caller ever had an account to pass,
    and this check was unreachable in production despite passing every test
    written for it in isolation. Call this explicitly, after the broker and
    its account are both in hand, and treat a non-empty result exactly like
    a `run()` failure — abort the cycle before it trades.
    """
    fails: list[str] = []

    # A bot configured to short on an account that cannot short does not fail
    # loudly — it fails one order at a time, at submission, and quietly runs
    # long-only. Preflight's polarity is FAIL SAFE, so this is a refusal.
    if not account.get("shorting_enabled", False):
        shorting = [name for name, s in (cfg.get("strategies") or {}).items()
                    if s.get("enabled") and s.get("short_bottom_fraction")]
        if shorting:
            fails.append(
                f"shorting is disabled on this brokerage account, but "
                f"{', '.join(sorted(shorting))} is configured to short "
                f"(short_bottom_fraction). Enable margin/shorting at the "
                f"broker, or unset short_bottom_fraction.")
    return fails


def _ledger_tail_fails(cfg: dict, ledger_path: str) -> list[str]:
    """Integrity of the LIVE audit trail, whichever backend holds it.

    Preflight deliberately runs before store.configure(), so it must resolve the
    backend itself — reading the raw .jsonl under a sqlite deployment validated
    a file the agent no longer writes. store.read_only_reader() resolves from
    cfg without mutating the process-wide backend and cannot write."""
    try:
        import store as store_mod
        backend, _ = store_mod.backend_kind(cfg)
    except Exception:  # noqa: BLE001 — store unavailable: fall back to the file
        backend = "jsonl"

    if backend == "jsonl":
        tail = _last_nonempty_line(ledger_path)
        if tail is None:
            return []
        try:
            json.loads(tail)
        except ValueError:
            return [f"ledger tail line is not valid JSON "
                    f"(partial write / corruption): {tail[:80]!r}"]
        return []

    try:
        import store as store_mod
        records = store_mod.read_only_reader(cfg, ledger_path).read_all()
    except Exception as e:  # noqa: BLE001
        return [f"ledger unreadable via {backend} backend: {e}"]
    if records and not isinstance(records[-1], dict):
        return [f"ledger tail record is not a JSON object ({backend} backend)"]
    return []


def _last_nonempty_line(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        return lines[-1].decode("utf-8", "replace") if lines else None
    except OSError:
        return None  # no ledger yet — first run is fine


def host_warnings(cfg: dict) -> list[str]:
    """Faults only the RUNNING HOST can convict. Warn-only, never blocking.

    Deliberately a third channel, separate from both `run()` and `warnings()`,
    because those two are pure by contract — "failures the config alone can
    convict" — and this cannot be: it reads an env var and the platform.

    The first draft of this check lived inside `run()` and CI convicted it in
    one commit: the same config passed on a Mac (banner channel exists) and
    failed on the Linux runner (no webhook, fixture with no owner), which took
    down every cycle test — the exact "CI measures the laptop" class this
    project keeps cataloguing, committed by the check that was supposed to
    close an audit finding. Purity is what makes `run()`'s verdicts portable;
    an env-reading check inside it makes them host-dependent.

    Warn-only is also the correct STRENGTH, not just the correct channel:
    alerting's own contract is that it never raises into a trading path, and a
    bot that refuses to TRADE because MONITORING is unreachable would invert
    that — the failure it guards against is "nobody is told", and the answer
    to that is to tell someone loudly at startup, not to stand the book down.

    Flags only the combination: no channel that reaches anyone AND no named
    owner. A named owner with no webhook is a laptop; an unnamed owner with a
    webhook is a documentation gap. Both alone are survivable. Neither is the
    state deploy/SECRETS.md warns about: the failure "is written to a log file
    and nobody is told."

    Uses alerting.channel(), which was called from NOWHERE until 2026-08-21 —
    correct, tested, unused. It never probes, so this cannot page anyone.
    """
    out: list[str] = []
    owner = str(((cfg.get("ops") or {}).get("incident_owner")) or "").strip()
    try:
        import alerting
        reach = alerting.channel()
    except Exception as e:  # noqa: BLE001 — a warning must never break startup
        out.append(f"alert channel could not be determined: {e}")
        return out
    if reach == "log-only" and not owner:
        out.append(
            "no alert channel and no ops.incident_owner on this host — a "
            "failure would be written to a log file and NOBODY WOULD BE TOLD. "
            "Set ALERT_WEBHOOK_URL, or name an owner who checks the logs")

    # A keyless judge whose endpoint is not answering. Host-convictable only
    # (it is a network round-trip), hence this channel and not run() — and
    # warn-only for the same reason on_unavailable exists: a dead judge must
    # degrade entries, never stop the cycle that manages open positions. The
    # 2026-08-21→28 outage blocked 53 entries before a human noticed; this
    # line is the ~09:35 record of the same fault, hours before the main
    # cycle. probe() gates on needs_key itself and never raises.
    if (cfg.get("llm") or {}).get("enabled"):
        try:
            import llm_client
            reason = llm_client.probe(cfg)
        except Exception as e:  # noqa: BLE001 — a warning must never break startup
            reason = f"judge probe itself failed: {e}"
        if reason:
            out.append(f"{reason} — judged entries will degrade and "
                       f"on_unavailable={((cfg.get('llm') or {}).get('on_unavailable') or 'block')!r} "
                       f"will refuse them; exits are unaffected")
    return out


def warnings(cfg: dict) -> list[str]:
    """Config faults worth SAYING but not worth refusing to trade over.

    Deliberately a separate channel from `run()`. Everything `run()` returns
    blocks the cycle — it is fail-safe by design and `main.py` treats any
    entry as fatal. A context budget that oversubscribes its total produces a
    worse judge prompt, not an unsafe trade, and a config edit must not be able
    to take the bot down at 09:25.

    Pure, like `run()`: `memory.context_budgets` is a module function precisely
    so this can do the arithmetic without opening the lesson and judgment
    stores.
    """
    out: list[str] = []
    try:
        import memory as memory_mod
        over = memory_mod.budget_overage(cfg)
        total = (cfg.get("learning") or {}).get("max_context_chars", 4000)
        if over:
            budgets = memory_mod.context_budgets(cfg)
            named = sum(v for v in budgets.values() if v is not None)
            # Name the blocks that lose, not just the arithmetic. They are the
            # ones assembled LAST, because `context_for_llm` slices the tail.
            at_risk = [b for b in memory_mod.CONTEXT_BLOCKS[-4:]
                       if budgets.get(b) is not None]
            out.append(
                f"learning context budgets oversubscribe their total by "
                f"{over} chars ({named} budgeted vs max_context_chars "
                f"{total}) — context_for_llm slices the TAIL, so "
                f"{', '.join(at_risk)} are what the judge stops seeing. §61.")
        unbounded = [k for k, v in memory_mod.context_budgets(cfg).items()
                     if v is None]
        if unbounded:
            out.append(
                f"context blocks with no budget: {', '.join(sorted(unbounded))}"
                f" — their size is unbounded, so the {total}-char total can "
                f"still be exceeded without the check above noticing")
    except Exception as e:  # noqa: BLE001 — a warning must never break startup
        out.append(f"context budget check failed to run: {e}")
    return out
