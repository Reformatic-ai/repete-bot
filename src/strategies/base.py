"""Shared strategy primitives: the Signal contract and pure indicators.

Every strategy module is deterministic plain code — the LLM never generates
signals. Modules follow a duck-typed contract:

    NAME: str
    NEEDS_CROSS_SECTION: bool
    def required_lookback(params) -> int
    NEEDS_ENTRY_TS: bool                        # optional, default False
    NEEDS_POSITION_SIDE: bool                   # optional, default False
    def prepare(all_bars, params, cfg=None) -> ctx   # cross-sectional only
    def generate(symbol, bars, params, holding, cross_section=None) -> Signal

`prepare` receives `cfg` as well as its own `params` because a cross-section can
depend on config outside the strategy's sub-dict (`reclaim` reads the top-level
`sectors:` map). `generate` deliberately does NOT: everything it needs must be
carried in the `cross_section` context `prepare` built, which keeps per-symbol
signal generation a pure function of (bars, params, ctx).

`NEEDS_ENTRY_TS = True` makes `strategies.generate` pass `entry_ts=` through —
required by any strategy with a max-hold rule. It is DECLARED rather than
inferred from the module name; the dispatch used to test `name == "meanrev"`,
which silently gave a second such strategy `entry_ts=None` and a max-hold that
measured nothing.

`NEEDS_POSITION_SIDE = True` makes it pass `position_side=` — `"long"`,
`"short"` or `None` when flat. `holding: bool` says only THAT a position exists,
never which way it points, and for a strategy with a short leg that is not
enough to choose an exit action:

    the owner-only exit branch (main.py) calls generate(owner, ..., True, ...)
    for any held name. Asked about a SHORT with only `holding=True` to go on,
    a long-biased strategy answers "sell" — which EXIT_ACTIONS accepts and
    broker.market_order maps to SELL, DOUBLING the short instead of closing it.

Declared, not inferred, for the same reason as NEEDS_ENTRY_TS: a strategy that
silently receives `None` where it expected a side does not fail, it just decides
wrongly. Every strategy that does not declare it keeps its exact call signature,
so adding the flag cannot move a strategy that has no short leg.
"""
from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    action: str                 # "buy" | "sell" | "short" | "cover" | "hold"
    reason: str                 # human-readable rationale
    indicators: dict = field(default_factory=dict)
    strategy: str = ""          # which strategy produced this signal


#: Actions that OPEN or ADD TO risk. Every entry rail keys off this set, so a
#: new direction added here is automatically subject to all of them — which is
#: the opposite of the failure where `action == "buy"` let shorts bypass the
#: concentration cap because nobody remembered to update the condition.
ENTRY_ACTIONS = ("buy", "short")

#: The mirror of ENTRY_ACTIONS: actions that CLOSE or REDUCE risk.
#:
#: This was removed on 2026-08-04 as having no consumer — correct for what was
#: visible then, when only `action == "buy"` existed and nothing read the exit
#: side of the vocabulary. It is reinstated because `swing_guard` (risk.py) IS
#: that consumer: `pre_trade_checks` gated the swing guard on `action ==
#: "sell"`, so a `"cover"` (closing a short) bypassed the minimum-holding-period
#: check entirely. Do not remove this again on the "no consumer" argument
#: without first checking whether `swing_guard`'s caller still needs it — that
#: is exactly the check that was missed the first time.
EXIT_ACTIONS = ("sell", "cover")


def side_of_qty(qty) -> str | None:
    """`"long"` | `"short"` | `None`, from a SIGNED quantity.

    The one implementation of "which way does this position point", because the
    live cycle and the offline simulator both have to answer it and a strategy
    that gets two different answers is a divergence by construction.

    A signed quantity is the input on purpose. `broker.positions()` copies
    Alpaca's own `float(p.qty)`, negative for a short, and main.py's in-cycle
    view was made to match that sign in Phase 2-C. Deriving the side anywhere
    else — from an action, from a market value, from the sign of unrealised P&L
    — would be a second source of truth for something the position record
    already carries.

    **Zero is `None`, not `"long"`.** A flat book is not a long one, and
    `qty == 0` reaches here from a fully-exited position whose record has not
    been dropped yet. Returning `"long"` there would tell a strategy it holds
    something it does not.
    """
    if qty is None:
        return None
    q = float(qty)
    if q > 0:
        return "long"
    if q < 0:
        return "short"
    return None


def sector_map(cfg: dict) -> dict:
    """`symbol -> sector name`, inverted from config's frozen `sectors:` block
    plus `core_sectors:` (the core names `sectors:` leaves out, 2026-11-02).

    Lives here, in the dependency-free base module, because `risk.py` (the
    sector-concentration rail) needs it and a strategy must never import
    `risk`. `risk.py` already imports ENTRY_ACTIONS/EXIT_ACTIONS from this
    module, so the direction is established. `reclaim` reads `sectors:` itself
    and never sees `core_sectors:`, so its universe and ranking are unchanged.

    A symbol in no sector is simply ABSENT from the result — callers treat that
    as "unmapped", not as a sector named None, so a name nobody mapped (a news
    nomination outside the core universe) is never lumped into a pseudo-sector
    with every other unmapped name and capped collectively.
    """
    out: dict = {}
    for block in ("sectors", "core_sectors"):
        for sector, syms in (cfg.get(block) or {}).items():
            for s in (syms or ()):
                out[s] = sector
    return out


def sector_of(cfg: dict, symbol: str):
    """The sector `symbol` belongs to, or None when it is unmapped."""
    return sector_map(cfg).get(symbol)


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def total_return(closes: list[float], bars_back: int, skip: int = 0) -> float | None:
    """Return over `bars_back` bars, measured up to `skip` bars ago
    (skip-month support): closes[-1-skip] / closes[-1-skip-bars_back] - 1."""
    end = len(closes) - skip          # exclusive index of the last counted close
    start = end - 1 - bars_back       # index of the base close
    if start < 0 or end < 1 or closes[start] <= 0:
        return None
    return closes[end - 1] / closes[start] - 1


def rsi(closes: list[float], period: int) -> float | None:
    """Wilder's RSI. Needs period+1 closes; smoothed over full history given."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(prev_close: float, bar: dict) -> float:
    """max(high-low, |high-prev_close|, |low-prev_close|)."""
    return max(bar["high"] - bar["low"],
               abs(bar["high"] - prev_close),
               abs(bar["low"] - prev_close))


def atr(bars: list[dict], period: int = 14) -> float | None:
    """Average True Range: simple mean of the last `period` true ranges.
    Needs period+1 bars; returns None on insufficient history."""
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    trs = [true_range(window[i - 1]["close"], window[i])
           for i in range(1, len(window))]
    return sum(trs) / period


def rvol(bars: list[dict], period: int = 20) -> float | None:
    """Relative volume: the last bar's volume over the mean of the `period`
    bars BEFORE it. 2.0 means "twice its recent normal".

    Added for §23. Every bar we load has carried a volume field since day one
    and no strategy ever read it, while two independent professional sources
    both treat volume expansion as core entry confirmation — a blind spot, not
    a decision.

    Returns None (never a number the caller might trust) when the answer is
    undefined: insufficient history, or a zero/absent baseline. That matters
    because callers FAIL OPEN on None — a missing volume feed must not silently
    block every entry, but it also must not fabricate an rvol of 0 or infinity.
    The current bar is excluded from its own baseline; including it would drag
    the mean toward today and understate a genuine spike.
    """
    if len(bars) < period + 1:
        return None
    prior = bars[-(period + 1):-1]
    base = sum(float(b.get("volume") or 0) for b in prior) / period
    if base <= 0:
        return None
    return float(bars[-1].get("volume") or 0) / base


def range_ratio(bars: list[dict], period: int = 20) -> float | None:
    """How wide the last `period` bars traded, as a fraction of price:

        (max high - min low) / last close

    Divided by price so the number is comparable across a $30 fund and a $600
    one, and across 2003 and 2026 for the same fund. An absolute range is not.

    ATR would be the obvious alternative and is deliberately not used: ATR is a
    MEAN of per-bar true ranges, so a quiet month containing one gap reads as
    moderately active. The high-water-to-low-water span reads it as what it is —
    a range that was breached. A coil is a claim about the envelope, not about
    the average day inside it.

    None (never a number) on insufficient history or a non-positive close.
    """
    if len(bars) < period or period < 1:
        return None
    window = bars[-period:]
    close = float(window[-1]["close"])
    if close <= 0:
        return None
    return (max(float(b["high"]) for b in window)
            - min(float(b["low"]) for b in window)) / close


def contraction_pctile(bars: list[dict], period: int = 20,
                       lookback: int = 252) -> float | None:
    """Where today's `range_ratio` sits inside its OWN trailing distribution,
    0-100. LOWER means tighter — 10.0 says "narrower than 90% of the last year".

    Ranked against itself rather than against a fixed threshold because there
    is no such thing as a universally narrow range: XLU's quiet is XLE's storm,
    and 2017's quiet is not 2008's. A percentile is the only form of this
    statistic that means the same thing on every symbol in a cross-section,
    which is what an ensemble rail needs.

    Volatility clustering is the empirical fact this leans on, and it is chosen
    partly BECAUSE it is not a return anomaly — a premium can be arbitraged
    away once published, whereas the tendency of quiet periods to sit next to
    quiet periods is a property of the price process itself.

    Needs `period + lookback` bars: `lookback` historical values of a statistic
    that each need `period` bars. Returns None below that, and callers FAIL
    OPEN on None — so a caller that is never given enough history silently
    blocks nothing. `strategies.max_lookback_bars` is what stops that being the
    normal case; see the note there.

    The rank is STRICT (`<`, not `<=`), so a flat series of identical ratios
    scores 0.0 rather than 50.0. That polarity is chosen on purpose: a symbol
    whose range never moves is genuinely coiled, and the alternative would have
    a dead instrument read as average.

    THE ARITHMETIC IS RESTATED HERE RATHER THAN LOOPING OVER `range_ratio`,
    and that duplication is a measured choice, not an oversight. The natural
    form — `range_ratio(bars[:len(bars) - i], period)` for each i — slices the
    whole (multi-thousand-bar) list `lookback` times and re-runs a Python-level
    `float()` over every element, which is three orders of magnitude slower
    than this and would dominate the runtime of every gate that turns the rail
    on. `test_contraction.py` pins the two against each other on the final
    window, so the copy cannot drift from the original without a red test.
    """
    need = period + lookback
    if len(bars) < need or lookback < 1 or period < 1:
        return None
    tail = bars[-need:]
    highs = [float(b["high"]) for b in tail]
    lows = [float(b["low"]) for b in tail]
    closes = [float(b["close"]) for b in tail]
    ratios = []
    for end in range(period, need + 1):        # `end` exclusive, oldest first
        close = closes[end - 1]
        if close <= 0:
            return None
        ratios.append((max(highs[end - period:end])
                       - min(lows[end - period:end])) / close)
    today = ratios[-1]
    below = sum(1 for h in ratios[:-1] if h < today)
    return 100.0 * below / lookback
