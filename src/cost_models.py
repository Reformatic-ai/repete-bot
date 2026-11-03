# Copied from reformatic-console reformatic/trading/costs.py; keep the two in step.
"""What a trade costs, market by market (trading settings schema 2).

Peter's first setting (migration 0032) was one flat cost per trade, $9 on every buy
and every sell. The research of 2026-10-27 found that wrong both ways: about nine
times too high for stocks and option spreads, and too low for crypto above about
$1,125 an order. So each market now has its own model, priced the way the broker
the research picked for that market charges:

  equities  per_share      IBKR Fixed: US$0.005 a share, at least US$1.00 an order,
                           at most 1% of the order's value.
  options   per_contract   IBKR: US$0.65 a contract plus US$0.025 OCC fees, at least
                           US$1.00 an order (the minimum is on the commission only),
                           so a 1-lot two-leg spread order is US$1.35. A spread that
                           expires worthless sends no order and pays nothing.
  fx        per_10k_units  OANDA Canada core: CAD 1 per 10,000 units a side, plus
                           about 0.2 pip of spread.
  crypto    percent        Kraken base tier, taker: 0.80% of the order's value a side.

Every market can instead be `flat` (one amount per order, the old way) or `off`.
Each market keeps all its values whatever model it is on, so switching back and
forth loses nothing.

A bot charges the model's cost LESS what its own venue already charged on that fill
(`charge = max(0, cost - venue_fee)`): Repete1's simulator already takes Kraken's
old 0.26% taker fee from its cash, so the operator cost tops it up to the model
rather than paying twice. The other four bots trade on commission-free paper, so
their venue fee is 0.

Two additions (Peter, 2026-11-03), both optional so a bot on the older copy of this
file still reads every settings file and simply leaves them out:

  fx_conversion_pct  on the markets priced in US dollars (stocks, options, crypto): what
                     a Canadian broker keeps for turning Canadian dollars into US dollars
                     to buy, and back to sell. A percent of the order's value, a side,
                     on top of the market's model. 0 (the default) charges nothing.
  entry_filter       per bot, off unless saved on: skip an entry whose expected edge is
                     not more than `multiple` times what opening and closing it would
                     cost (round_trip_cost, edge_clears_cost). Each bot says how it
                     estimates its own edge.

BROKER_PRESETS holds what three Canadian brokers publish, each figure with the page it
came from and the day it was read, and Peter's flat C$9. A preset only fills in the
form (apply_preset); nothing changes until it is saved.

This module is the one place the models are written down. It uses the standard
library only, so `scripts/trading_settings_sync.py` loads it by path, and each bot
keeps a copy of it (named in that bot's operator-settings module).
"""

from __future__ import annotations

import math

SCHEMA = 2
MARKETS = ("equities", "options", "fx", "crypto")

# The model a market uses when it is priced like its broker.
NATURAL = {
    "equities": "per_share",
    "options": "per_contract",
    "fx": "per_10k_units",
    "crypto": "percent",
}
MODELS = {m: ("off", "flat", NATURAL[m]) for m in MARKETS}

# The account currency of the bots that trade each market. Stocks, options and crypto
# are priced in US dollars, so a Canadian account pays to convert on those.
ACCOUNT = {"equities": "USD", "options": "USD", "fx": "CAD", "crypto": "USD"}
CONVERTED = tuple(m for m in MARKETS if ACCOUNT[m] == "USD")

# Values added after a row could already have been saved, and what a row without one
# means: nothing charged.
OPTIONAL = {"fx_conversion_pct": 0.0}

# The amounts that are money (the rest are percents or pips): a preset written in
# another currency is converted on these only.
MONEY = frozenset(
    {
        "flat_per_order",
        "per_share",
        "min_per_order",
        "per_contract",
        "fees_per_contract",
        "per_10k_units",
    }
)

# The bots with an entry cost filter, and the largest multiple of the round-trip cost
# the edge can be asked to beat.
FILTER_BOTS = ("repete", "repete1", "repete2", "repete3", "repete4")
FILTER_OFF = {"on": False, "multiple": 1.0}
FILTER_MULTIPLE_MAX = 10.0

# Every value a market carries, whatever its model, with its upper bound (all are
# >= 0). `max_pct_of_value` 0 means no cap.
LIMITS = {
    "equities": {
        "flat_per_order": 1000.0,
        "per_share": 1.0,
        "min_per_order": 1000.0,
        "max_pct_of_value": 100.0,
        "fx_conversion_pct": 10.0,
    },
    "options": {
        "flat_per_order": 1000.0,
        "per_contract": 100.0,
        "fees_per_contract": 100.0,
        "min_per_order": 1000.0,
        "fx_conversion_pct": 10.0,
    },
    "fx": {"flat_per_order": 1000.0, "per_10k_units": 1000.0, "spread_pips": 100.0},
    "crypto": {"flat_per_order": 1000.0, "pct_of_value": 10.0, "fx_conversion_pct": 10.0},
}

# The research's values (2026-10-27). The flat amounts are what a flat order would
# cost at that broker for a typical order of these bots. Its brokers hold US dollars
# (IBKR) or trade in the account's own currency, so it converts nothing.
RECOMMENDED = {
    "equities": {
        "model": "per_share",
        "flat_per_order": 1.0,
        "per_share": 0.005,
        "min_per_order": 1.0,
        "max_pct_of_value": 1.0,
        "fx_conversion_pct": 0.0,
    },
    "options": {
        "model": "per_contract",
        "flat_per_order": 1.4,
        "per_contract": 0.65,
        "fees_per_contract": 0.025,
        "min_per_order": 1.0,
        "fx_conversion_pct": 0.0,
    },
    "fx": {
        "model": "per_10k_units",
        "flat_per_order": 1.0,
        "per_10k_units": 1.0,
        "spread_pips": 0.2,
    },
    "crypto": {
        "model": "percent",
        "flat_per_order": 1.0,
        "pct_of_value": 0.8,
        "fx_conversion_pct": 0.0,
    },
}

# How the amounts read on the page. Stocks and options are priced in US dollars (the
# accounts that trade them are USD); FX in Canadian dollars (Repete2's account is
# CAD). A bot charges in its own account currency.
UNIT = {"equities": "US$", "options": "US$", "fx": "CAD ", "crypto": "$"}
LABELS = {"equities": "Stocks", "options": "Options", "fx": "FX", "crypto": "Crypto"}
BOTS = {
    "equities": "Repete, Repete3",
    "options": "Repete4",
    "fx": "Repete2",
    "crypto": "Repete1",
}


def _number(market: str, key: str, value) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{market}.{key} must be a number")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{market}.{key} must be a number") from None
    if not math.isfinite(x) or not 0 <= x <= LIMITS[market][key]:
        raise ValueError(f"{market}.{key} {x} is outside 0..{LIMITS[market][key]:g}")
    return round(x, 6)


def validate(raw) -> dict:
    """The cost models, checked and normalised, or ValueError naming the bad value.
    Every market must be there with a known model and every one of its values (an
    OPTIONAL value left out reads as its default); `entry_filter` is read by
    validate_filter; anything else in `raw` is dropped."""
    if not isinstance(raw, dict):
        raise ValueError("costs must be an object")
    out = {}
    for market in MARKETS:
        m = raw.get(market)
        if not isinstance(m, dict):
            raise ValueError(f"costs.{market} is missing")
        model = m.get("model")
        if model not in MODELS[market]:
            raise ValueError(f"{market}.model {model!r} is not one of {', '.join(MODELS[market])}")
        out[market] = {"model": model}
        for key in LIMITS[market]:
            if key not in m and key in OPTIONAL:
                out[market][key] = OPTIONAL[key]
                continue
            if key not in m:
                raise ValueError(f"{market}.{key} is missing")
            out[market][key] = _number(market, key, m[key])
    out["entry_filter"] = validate_filter(raw.get("entry_filter"))
    return out


def validate_filter(raw) -> dict:
    """Each bot's entry cost filter, checked: {"on": bool, "multiple": 1..10}. A bot
    left out, or no filter at all, is off with a multiple of 1."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("entry_filter must be an object")
    out = {}
    for bot in FILTER_BOTS:
        f = raw.get(bot, FILTER_OFF)
        if not isinstance(f, dict):
            raise ValueError(f"entry_filter.{bot} must be an object")
        on = f.get("on", False)
        if not isinstance(on, bool):
            raise ValueError(f"entry_filter.{bot}.on must be true or false")
        mult = f.get("multiple", 1.0)
        if isinstance(mult, bool) or not isinstance(mult, (int, float)):
            raise ValueError(f"entry_filter.{bot}.multiple must be a number")
        if not math.isfinite(mult) or not 1.0 <= mult <= FILTER_MULTIPLE_MAX:
            raise ValueError(f"entry_filter.{bot}.multiple {mult} is outside 1..10")
        out[bot] = {"on": on, "multiple": round(float(mult), 6)}
    return out


def from_v1(fee_per_trade: float, fee_on_crypto: bool, fee_on_fx: bool) -> dict:
    """Schema-1 settings as cost models that charge exactly what they charged: the
    flat fee on stocks and options, and on crypto and FX only when switched on. The
    per-market values are the research's, ready for when a market is switched to
    its broker's model."""
    fee = round(float(fee_per_trade), 6)
    out: dict = {m: dict(RECOMMENDED[m], flat_per_order=fee) for m in MARKETS}
    for m in ("equities", "options"):
        out[m]["model"] = "flat"
    out["crypto"]["model"] = "flat" if fee_on_crypto else "off"
    out["fx"]["model"] = "flat" if fee_on_fx else "off"
    out["entry_filter"] = validate_filter(None)
    return out


def v1_equivalent(costs: dict) -> dict:
    """The schema-1 fields for a bot that cannot read cost models yet. Schema 1 has
    one amount for every market, so it takes the stock order's: the flat amount, or
    the per-order minimum under per_share, or 0 when stocks are off. Crypto and FX
    pay it only when they are on `flat`: a flat amount cannot stand in for a
    percentage or a per-unit price, so under those models a schema-1 bot charges
    nothing there, as it did before."""
    eq = costs["equities"]
    fee = {"off": 0.0, "flat": eq["flat_per_order"], "per_share": eq["min_per_order"]}[eq["model"]]
    return {
        "fee_per_trade": round(fee, 2),
        "fee_on_crypto": costs["crypto"]["model"] == "flat",
        "fee_on_fx": costs["fx"]["model"] == "flat",
    }


def from_settings(raw: dict) -> tuple[dict, str]:
    """(cost models, problem) from a settings file as the sync writes it. Schema 2
    carries `costs`; a schema-1 file (or a schema-2 file whose `costs` fail to
    validate) is read from its flat fields. `problem` is "" when the costs were read
    as written. Raises ValueError (or KeyError/TypeError) only when the flat fields
    are bad too; the caller then charges nothing, as with no file at all."""
    problem = ""
    if raw.get("schema") == SCHEMA:
        try:
            return validate(raw.get("costs")), ""
        except ValueError as e:
            problem = f"costs invalid ({e}); using the flat schema-1 cost"
    fee = float(raw["fee_per_trade"])
    if not math.isfinite(fee) or not 0 <= fee <= 1000:
        raise ValueError(f"fee_per_trade {fee} out of range")
    return from_v1(fee, raw.get("fee_on_crypto") is True, raw.get("fee_on_fx") is True), problem


def _need(model: str, name: str, value: float | None) -> float:
    """`value` as a float, or ValueError when the model needs it and it was not given."""
    if value is None:
        raise ValueError(f"{model} needs {name}")
    return float(value)


def order_cost(
    costs: dict,
    market: str,
    *,
    shares: float | None = None,
    contracts: float | None = None,
    units: float | None = None,
    notional: float | None = None,
    pip: float | None = None,
    quote_rate: float | None = None,
) -> tuple[float, dict]:
    """(cost, basis) of ONE order in `market`, in the account's currency, before any
    venue fee is taken off. `basis` says how it was worked out, for the fee log.

      shares     equities per_share: shares in the order (sign ignored)
      notional   the order's value (per_share's 1% cap; percent's base)
      contracts  options per_contract: contracts across ALL legs of the order
                 (a 1-lot two-leg spread is 2)
      units      fx per_10k_units: units of the base currency
      pip        fx: the pair's pip size (0.0001; 0.01 for JPY pairs)
      quote_rate fx: account currency per one unit of the QUOTE currency (1 for
                 USD_CAD in a CAD account). Without it, or without `pip`, the spread
                 part is left out and `basis` says so.

    On a market with `fx_conversion_pct` above 0 the conversion is added on top, as a
    percent of `notional` (for an option order, the premium it moves); without
    `notional` it is left out and `basis` says so. A market that is `off` costs
    nothing, conversion included.

    Raises ValueError when the model needs an input that was not given."""
    c = costs[market]
    model = c["model"]
    basis: dict = {"model": model}
    if model == "off":
        return 0.0, basis
    fee = _commission(c, model, basis, shares, contracts, units, notional, pip, quote_rate)
    pct = float(c.get("fx_conversion_pct") or 0.0)
    if pct > 0:
        if notional is None:
            basis["conversion_skipped"] = "no order value"
        else:
            conversion = pct / 100 * abs(float(notional))
            fee += conversion
            basis["conversion"] = round(conversion, 4)
    return round(fee, 4), basis


def _commission(c, model, basis, shares, contracts, units, notional, pip, quote_rate) -> float:
    """The market model's own cost of one order (order_cost without the conversion)."""
    if model == "flat":
        return c["flat_per_order"]
    if model == "per_share":
        n = abs(_need(model, "shares", shares))
        fee = max(n * c["per_share"], c["min_per_order"])
        basis["shares"] = n
        if c["max_pct_of_value"] > 0 and notional is not None:
            cap = c["max_pct_of_value"] / 100 * abs(float(notional))
            basis["notional"] = round(abs(float(notional)), 4)
            if cap < fee:
                fee, basis["capped"] = cap, True
        return fee
    if model == "per_contract":
        n = abs(_need(model, "contracts", contracts))
        fee = max(n * c["per_contract"], c["min_per_order"]) + n * c["fees_per_contract"]
        basis["contracts"] = n
        return fee
    if model == "per_10k_units":
        u = abs(_need(model, "units", units))
        fee = u / 10_000 * c["per_10k_units"]
        basis["units"] = u
        if c["spread_pips"] > 0:
            if pip is None or quote_rate is None:
                basis["spread_skipped"] = "no pip size or quote rate"
            else:
                spread = c["spread_pips"] * float(pip) * u * float(quote_rate)
                fee += spread
                basis["spread"] = round(spread, 4)
        return fee
    if model == "percent":
        v = abs(_need(model, "notional", notional))
        basis["notional"] = round(v, 4)
        return c["pct_of_value"] / 100 * v
    raise ValueError(f"unknown model {model!r}")


def round_trip_cost(costs: dict, market: str, **order) -> tuple[float, dict]:
    """(cost, basis) of opening an order and later closing it, priced as two orders of
    the same size and value: at entry the close's price is not known yet. `order`
    takes order_cost's keywords. A bot whose close may send no order (an option
    spread that expires worthless) says so where it calls this."""
    one, basis = order_cost(costs, market, **order)
    return round(2 * one, 4), basis


def entry_filter(costs: dict, bot: str) -> dict:
    """{"on", "multiple"} for one bot; off when the costs carry no filter for it."""
    f = (costs.get("entry_filter") or {}).get(bot)
    return dict(f) if isinstance(f, dict) else dict(FILTER_OFF)


def edge_clears_cost(edge: float | None, round_trip: float, multiple: float = 1.0) -> bool:
    """True when the expected edge is more than `multiple` times the round-trip cost.
    No estimate (None) clears it: the filter only stops a trade it can judge."""
    if edge is None:
        return True
    return float(edge) > float(multiple) * float(round_trip)


def charge_amount(gross: float, venue_fee: float | None) -> float:
    """What the operator cost adds on top of what the venue already charged."""
    return round(max(0.0, gross - max(0.0, float(venue_fee or 0.0))), 4)


def _money(unit: str, x: float) -> str:
    s = f"{x:,.3f}" if round(x, 2) != round(x, 3) else f"{x:,.2f}"
    return f"{unit}{s}"


def describe(costs: dict, market: str) -> str:
    """One line for the page and the bots' logs."""
    c = costs[market]
    if c["model"] == "off":
        return "no cost"
    s = _describe_model(c, UNIT[market])
    pct = float(c.get("fx_conversion_pct") or 0.0)
    if pct > 0:
        s += f", plus {pct:g}% to convert Canadian dollars each way"
    return s


def _describe_model(c: dict, u: str) -> str:
    model = c["model"]
    if model == "flat":
        return f"{_money(u, c['flat_per_order'])} an order, flat"
    if model == "per_share":
        s = f"{_money(u, c['per_share'])} a share, at least {_money(u, c['min_per_order'])}"
        s += " an order"
        if c["max_pct_of_value"] > 0:
            s += f", at most {c['max_pct_of_value']:g}% of its value"
        return s
    if model == "per_contract":
        s = f"{_money(u, c['per_contract'])} a contract"
        if c["fees_per_contract"] > 0:
            s += f" + {_money(u, c['fees_per_contract'])} fees"
        return s + f", at least {_money(u, c['min_per_order'])} an order"
    if model == "per_10k_units":
        s = f"{_money(u, c['per_10k_units'])} per 10,000 units a side"
        if c["spread_pips"] > 0:
            s += f" + {c['spread_pips']:g} pip"
        return s
    return f"{c['pct_of_value']:g}% of the order's value a side"


# ------------------------------------------------------------------ broker presets
#
# What three Canadian brokers publish, read on their own pages on 2026-11-03, and the
# flat C$9 Peter asked for. A preset only fills in the /trading form: nothing is
# charged until someone saves it.
#
# Each market is None when the broker does not offer it (filling the preset in leaves
# that market as it is), or its model and values in `currency`. A value of None means
# the broker does not publish it, and filling the preset in leaves that value as it is
# rather than guess. Money is converted into the account's currency (ACCOUNT) at
# CAD_PER_USD.

CAD_PER_USD = 1.3978
CAD_PER_USD_SOURCE = (
    "Bank of Canada daily USD/CAD rate for 2026-11-02, read 2026-11-03: "
    "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
)

BROKER_PRESETS: dict = {
    "flat_c9": {
        "label": "Flat C$9 a trade",
        "checked": "2026-11-03",
        "sources": [],
        "markets": {
            "equities": {
                "currency": "CAD",
                "model": "flat",
                "flat_per_order": 9.0,
                "fx_conversion_pct": 1.5,
            },
            "options": {
                "currency": "CAD",
                "model": "flat",
                "flat_per_order": 9.0,
                "fx_conversion_pct": 1.5,
            },
            "fx": {"currency": "CAD", "model": "flat", "flat_per_order": 9.0},
            "crypto": {
                "currency": "CAD",
                "model": "flat",
                "flat_per_order": 9.0,
                "fx_conversion_pct": 1.5,
            },
        },
        "notes": [
            "Peter's default (2026-11-03): C$9 on every buy and every sell, in every market.",
            "Converting Canadian dollars on US-dollar trades at 1.5% a side: Wealthsimple's "
            "published fee, the lowest any of the three brokers publishes (TD's works out "
            "to 1.68%).",
        ],
    },
    "td": {
        "label": "TD Direct Investing",
        "checked": "2026-11-03",
        "sources": [
            "https://www.td.com/ca/en/investing/direct-investing/pricing",
            "https://www.td.com/content/dam/wealth/document/pdf/direct-investing/521778-en.pdf",
            "https://www.td.com/ca/en/investing/direct-investing/pricing/fx-pricing",
        ],
        "markets": {
            "equities": {
                "currency": "USD",
                "model": "flat",
                "flat_per_order": 9.99,
                "fx_conversion_pct": 1.68,
            },
            "options": {
                "currency": "USD",
                "model": "per_contract",
                "per_contract": 0.0,
                "min_per_order": 9.99,
                "fees_per_contract": 1.25,
                "fx_conversion_pct": 1.68,
            },
            "fx": None,
            "crypto": None,
        },
        "notes": [
            "Stocks: $9.99 a trade, Canadian or US ($7.00 at 150 or more trades a quarter). "
            "Commissions on US-dollar trades are charged in US dollars.",
            "Options: $9.99 + $1.25 a contract. TD publishes no online price for "
            "multi-leg orders, so a spread is priced as one order; it may cost more.",
            "Conversion: a 1.26% spread under $25,000, added to an affiliate rate that "
            "carries a surcharge of one third of it, so 1.26% x 4/3 = 1.68% a side. "
            "A US-dollar account avoids it.",
            "No crypto and no currency-pair trading.",
        ],
    },
    "scotia": {
        "label": "Scotia iTRADE",
        "checked": "2026-11-03",
        "sources": [
            "https://www.scotiaitrade.com/en/home/pricing.html",
            "https://www.scotiaitrade.com/content/dam/itrade/documents/disclosures.pdf",
        ],
        "markets": {
            "equities": {
                "currency": "USD",
                "model": "flat",
                "flat_per_order": 9.99,
                "fx_conversion_pct": None,
            },
            "options": {
                "currency": "USD",
                "model": "per_contract",
                "per_contract": 0.0,
                "min_per_order": 19.98,
                "fees_per_contract": 1.25,
                "fx_conversion_pct": None,
            },
            "fx": None,
            "crypto": None,
        },
        "notes": [
            "Stocks: $9.99 a trade, Canadian or US ($4.99 at 150 or more trades a "
            "quarter). Fees on US trades are charged in US dollars.",
            'Options: $9.99 + $1.25 a contract, and "full commission charges apply to '
            'each side of spread and combination orders": a two-leg spread pays $9.99 '
            "twice, written here as a $19.98 minimum (a lone leg would be charged $9.99 "
            "too much).",
            'Conversion: Scotia publishes no rate ("available upon request"), so the '
            "field is left as it is.",
            "No crypto and no currency-pair trading.",
        ],
    },
    "wealthsimple": {
        "label": "Wealthsimple",
        "checked": "2026-11-03",
        "sources": [
            "https://www.wealthsimple.com/en-ca/legal/fees/trade",
            "https://www.wealthsimple.com/en-ca/legal/fees/crypto",
            "https://www.wealthsimple.com/en-ca/pricing",
        ],
        "markets": {
            "equities": {
                "currency": "USD",
                "model": "flat",
                "flat_per_order": 0.0,
                "fx_conversion_pct": 1.5,
            },
            "options": {
                "currency": "USD",
                "model": "flat",
                "flat_per_order": 0.0,
                "fx_conversion_pct": 1.5,
            },
            "fx": None,
            "crypto": {
                "currency": "CAD",
                "model": "percent",
                "pct_of_value": 0.5,
                "fx_conversion_pct": 0.0,
            },
        },
        "notes": [
            "Stocks and US-listed options: $0 commission (exchange and regulatory fees "
            "passed through are not counted).",
            "Conversion: its corporate rate x 1.5% a side. That rate already includes a "
            "spread Wealthsimple does not publish, so 1.5% is low. Trading from a "
            "US-dollar account avoids it ($10 a month on the Core plan).",
            "Crypto: 0.5% an order, priced in Canadian dollars, plus a market-order "
            "spread it does not publish and $1 more on an order under $100.",
            "No currency-pair trading.",
        ],
    },
}


def apply_preset(costs: dict, key: str, cad_per_usd: float = CAD_PER_USD) -> tuple[dict, list[str]]:
    """(costs with the preset filled in, what was left as it was and why). Money is
    turned into each market's account currency at `cad_per_usd`."""
    preset = BROKER_PRESETS[key]
    out = {m: dict(costs[m]) for m in MARKETS}
    out["entry_filter"] = validate_filter(costs.get("entry_filter"))
    kept: list[str] = []
    for market in MARKETS:
        spec = preset["markets"].get(market)
        if spec is None:
            kept.append(f"{LABELS[market]}: {preset['label']} does not offer it")
            continue
        rate = 1.0
        if spec["currency"] != ACCOUNT[market]:
            rate = 1 / cad_per_usd if spec["currency"] == "CAD" else cad_per_usd
        out[market]["model"] = spec["model"]
        for k, v in spec.items():
            if k in ("currency", "model") or k not in LIMITS[market]:
                continue
            if v is None:
                kept.append(f"{LABELS[market]}: {preset['label']} does not publish {k}")
                continue
            out[market][k] = round(v * rate, 4) if k in MONEY else v
    return validate(out), kept
