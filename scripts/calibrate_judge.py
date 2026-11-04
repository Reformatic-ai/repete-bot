#!/usr/bin/env python3
"""Derive the LLM judge's observed sizing behaviour from the live ledger.

    python scripts/calibrate_judge.py                 # print, write nothing
    python scripts/calibrate_judge.py --write         # update the calibration
    python scripts/calibrate_judge.py --since 2026-09-01   # a later window

Only rows judged ON OR AFTER `--since` count (default 2026-08-28, the day the
judge moved from claude-sonnet-5 to the self-hosted qwen3.8-27b and
`context_version` went 2 -> 3). Before it, one fit would pool two different
judges' verdicts, which is the mixing the version guard exists to refuse.

Why this exists
---------------
`src/backtest.py` deliberately never imports `llm` (see its module docstring),
so the judge cannot be replayed over history — there is no way to ask what a
model would have said about a bar in 2024. But live, the judge downsizes a
majority of buys, and the simulator has never modelled that at all. §26 named
this as the live half of divergence #8:

    the backtest never applies the judge's verdict: there is no
    review["scale"] anywhere in backtest.py

Every OOS figure in `knowledge/backtest_candidates.md` was therefore produced by
a simulator sizing entries the live bot would have cut. This script measures the
cut, so `backtest.judge_model` can apply it instead of inventing a number.

What it deliberately does NOT do
--------------------------------
It does not condition on strategy or confidence. Both look tempting and both
are unsupportable at this sample: meanrev is n=7 and ma_crossover is n=3, and
106 of 146 decisions carry no confidence field at all. Fitting a per-strategy
haircut to seven observations would be exactly the overfit this project's gate
discipline exists to prevent. One pooled distribution, honestly labelled.

Read-only against the ledger (invariant #9). It never writes to memory/.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "memory", "ledger.jsonl")
OUT = os.path.join(ROOT, "knowledge", "judge_calibration.json")

sys.path.insert(0, os.path.join(ROOT, "src"))
# Shared with src/judgments.py so the "was this actually judged?" rule is one
# definition. A second copy here is how the two would silently disagree about
# which rows count.
from llm import is_fallback_review  # noqa: E402

# Below this many judged buys the distribution is noise dressed as data. The
# haircut model refuses to load a calibration that did not clear it.
MIN_SAMPLE = 50

# The first day of the CURRENT judge (context_version 3): 2026-08-28, when the
# judge moved from claude-sonnet-5 to the self-hosted qwen3.8-27b (config.yaml
# backtest.judge_model). Rows before it were judged by a different model, so a
# fit that included them would describe neither judge. The first judged buy on
# or after this date is 2026-08-31 (the judge was unreachable 2026-08-20..30,
# so those days carry fallback rows only, which are excluded anyway).
#
# Bump it in the SAME change that bumps context_version, so the default window
# always starts where the current judge does.
DEFAULT_SINCE = "2026-08-28"


def _since_ok(ts, since: str | None) -> bool:
    """True when a row's timestamp is on or after `since` (a YYYY-MM-DD day).

    Compared on the date part as a string: every ledger `ts` is ISO-8601 UTC,
    so "2026-08-28T13:35..."[:10] >= "2026-08-28" is exact, and no timezone
    parsing can move a row across the boundary. A row with no `ts` cannot say
    which judge saw it, so with a window set it is OUT, never in.
    """
    if since is None:
        return True
    return isinstance(ts, str) and ts[:10] >= since


def collect(ledger_path: str, since: str | None = None
            ) -> tuple[list[dict], str, int]:
    """Every buy decision ACTUALLY JUDGED, plus the ledger's sha and the count
    of fallback rows excluded.

    `since` (YYYY-MM-DD, inclusive) keeps only rows judged on or after that
    day, and the fallback count is taken inside the same window. None = the
    whole ledger, which is what this function did before 2026-11-04 and what a
    caller that names no window still gets. The CLI passes DEFAULT_SINCE.
    The sha is always of the WHOLE file, so it still identifies the input.

    The exclusion was added 2026-08-21 and it is not cosmetic. Until then this
    filter took any row with a truthy `llm_review`, which includes the fallback
    dict written when the judge was unreachable — and a fallback is always
    `scale: 1.0`, so every one of them pulled the modelled haircut toward "no
    haircut". Measured on the ledger at the time: 15 degraded rows of 301,
    inflating mean_scale from 0.6482 to 0.6664 in the PERMISSIVE direction.

    That number is not confined to a report. It lands in
    knowledge/judge_calibration.json, which src/judge_model.py applies to every
    simulated entry, so the contamination sized up entries in every backtest
    scored with the judge on.

    The count is returned rather than silently dropped so the emitted
    calibration can state how many rows it refused — an exclusion nobody can
    see is indistinguishable from a filter that never fired.
    """
    h = hashlib.sha256()
    rows = []
    n_degraded = 0
    with open(ledger_path, "rb") as f:
        for raw in f:
            h.update(raw)
            if not raw.strip():
                continue
            r = json.loads(raw)
            if (r.get("type") == "decision" and r.get("action") == "buy"
                    and r.get("llm_review") and _since_ok(r.get("ts"), since)):
                if is_fallback_review(r["llm_review"]):
                    n_degraded += 1
                    continue
                rows.append(r)
    return rows, h.hexdigest()[:16], n_degraded


def calibrate(rows: list[dict]) -> dict:
    """Split the judge's behaviour into two independent effects.

    A veto and a downsize are different acts and the simulator must model them
    differently: a veto removes the trade, a downsize shrinks it. Folding the
    vetoes' `scale: 0.1` into the haircut histogram would understate the mean
    cut AND silently let vetoed trades through at 10% size — wrong twice.
    """
    vetoes = [r for r in rows if r["llm_review"].get("verdict") == "veto"]
    sized = [r for r in rows if r["llm_review"].get("verdict") != "veto"]

    scales = [r["llm_review"].get("scale") for r in sized]
    scales = [s for s in scales if isinstance(s, (int, float))]
    hist = collections.Counter(scales)
    mean = sum(scales) / len(scales) if scales else 1.0
    downsized = [s for s in scales if s < 1.0]

    ts = [r["ts"] for r in rows if r.get("ts")]
    span_days = 0
    if ts:
        span_days = (datetime.fromisoformat(max(ts))
                     - datetime.fromisoformat(min(ts))).days

    return {
        "n_judged_buys": len(rows),
        "n_sized": len(sized),
        "veto_rate": round(len(vetoes) / len(rows), 6) if rows else 0.0,
        "downsize_rate": round(len(downsized) / len(scales), 6) if scales else 0.0,
        "mean_scale": round(mean, 6),
        # Sorted so the JSON is stable across runs and diffs are readable.
        "scale_histogram": {str(k): hist[k] for k in sorted(hist)},
        "observed_span_days": span_days,
        "observed_from": min(ts)[:10] if ts else None,
        "observed_to": max(ts)[:10] if ts else None,
    }


def _context_version(path: str = "config.yaml"):
    """`backtest.judge_model.context_version` from config, or None.

    None when the config cannot be read — and None is written through as None
    rather than defaulting to 1. A calibration that cannot say which judge it
    saw must not claim to have seen the current one; judge_model treats a
    missing version as a mismatch, which is the safe direction.
    """
    try:
        import yaml
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — a calibration tool must not need a config
        return None
    return (((cfg.get("backtest") or {}).get("judge_model")) or {}).get(
        "context_version")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true",
                   help=f"write {os.path.relpath(OUT, ROOT)} (default: print only)")
    p.add_argument("--ledger", default=LEDGER)
    p.add_argument("--since", default=DEFAULT_SINCE, metavar="YYYY-MM-DD",
                   help=f"only rows judged on or after this day (default "
                        f"{DEFAULT_SINCE}, the current judge's first day)")
    args = p.parse_args()
    try:
        datetime.strptime(args.since, "%Y-%m-%d")
    except ValueError:
        print(f"--since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
        return 2

    if not os.path.exists(args.ledger):
        print(f"no ledger at {args.ledger}", file=sys.stderr)
        return 2

    rows, sha, n_degraded = collect(args.ledger, since=args.since)
    cal = calibrate(rows)
    cal["source_ledger_sha256_16"] = sha
    # The window, written into the file: `observed_from` says when the first
    # counted row was, and this says where counting was ALLOWED to start, so a
    # reader can tell "no rows before X" from "rows before X were refused".
    cal["since"] = args.since
    cal["min_sample"] = MIN_SAMPLE
    # Emitted into the calibration file, not just printed. A reader six months
    # from now needs to know this number was measured on judged rows only —
    # and if the count is ever large relative to n_judged_buys, that is a
    # degradation problem showing up in the one place someone will look.
    cal["n_degraded_excluded"] = n_degraded
    # WHICH JUDGE THIS DESCRIBES. Read from config rather than hardcoded, so a
    # re-fit after a context bump stamps the NEW regime automatically.
    # Without this the guard in judge_model._check_version would have no
    # escape hatch: every re-fit would produce a file that fails the check
    # forever, and the obvious workaround would be to delete the check.
    cal["fitted_context_version"] = _context_version()

    for k, v in cal.items():
        print(f"  {k}: {v}")

    if cal["n_judged_buys"] < MIN_SAMPLE:
        print(f"\nREFUSING: {cal['n_judged_buys']} judged buys is below the "
              f"{MIN_SAMPLE} minimum. Not enough to model.", file=sys.stderr)
        return 1

    # Stated here rather than in a commit message, because whoever reads a gate
    # result six months from now will have this file and not the commit.
    print(f"\n  NOTE: {cal['n_judged_buys']} decisions over "
          f"{cal['observed_span_days']} days are heavily SERIALLY CORRELATED —"
          f"\n  the same names re-judged daily in one market regime. The"
          f"\n  effective sample is far below {cal['n_judged_buys']}. Treat the"
          f"\n  mean scale as a usable central estimate and the tails as unmeasured.")

    if args.write:
        with open(OUT, "w") as f:
            json.dump(cal, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"\n  wrote {OUT}")
    else:
        print(f"\n  (dry run — pass --write to update "
              f"{os.path.relpath(OUT, ROOT)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
