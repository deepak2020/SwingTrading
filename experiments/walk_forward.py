"""
Walk-forward fine-tuning test — OMXS30 momentum strategy
=========================================================
Answers "should we fine-tune the parameters?" honestly. Every January from
2019 on, re-optimize the momentum strategy's parameters on the trailing 4
years only (data available at the time), then trade the next year with those
parameters, chaining equity year to year. Two selection rules are tested
(pick trailing-window best by final equity, or by Sharpe) against two fixed
baselines that never re-tune.

If adaptive re-tuning beats robust fixed parameters out-of-sample, tuning
adds value; if not, the extra fitting is just memorizing noise.

Simplification: positions are liquidated (fee-free) at each year boundary
since each yearly segment is an independent run.

Run:  python experiments/walk_forward.py
Writes experiments/walk_forward_results.csv.
"""

import itertools
import os

import numpy as np
import pandas as pd

from strategy_search import run, fetch_adjusted_closes, TICKERS, START_DATE, END_DATE

CAPITAL0 = 100_000
TRAIN_YEARS = 4
TEST_YEARS = list(range(2019, 2027))          # 2026 is a partial year
GRID = list(itertools.product(
    [4, 6],              # top_n
    [8, 12, 26],         # momentum_weeks
    [1, 4],              # rebalance_weeks
    [0, 4, 8],           # rank_buffer
    [0.10, 0.15],        # stop_pct
))
FIXED_BEST = (4, 12, 1, 4, 0.15)     # full-sample sweep winner (biased pick)
FIXED_ORIG = (6, 12, 1, 0, 0.10)     # original backtest.py parameters


def year_slice(weekly, year, params):
    """Weekly data for one test year plus exactly the warmup the params need."""
    _, mom_w, _, _, _ = params
    warmup = 20 + mom_w + 1
    start = pd.Timestamp(f"{year}-01-01") - pd.Timedelta(weeks=warmup)
    end = pd.Timestamp(f"{year}-12-31")
    return weekly.loc[start:end]


def main():
    print("Downloading data...")
    raw = fetch_adjusted_closes(TICKERS, START_DATE, END_DATE)
    raw = raw.dropna(axis=1, thresh=int(len(raw) * 0.8))
    weekly = raw.resample("W-FRI").last().ffill(limit=2)
    print(f"Usable tickers: {len(raw.columns)}")

    equity = {"wf_final": CAPITAL0, "wf_sharpe": CAPITAL0,
              "fixed_best": CAPITAL0, "fixed_orig": CAPITAL0}
    picks = []

    for year in TEST_YEARS:
        train = weekly.loc[f"{year - TRAIN_YEARS}-01-01":f"{year - 1}-12-31"]

        results = []
        for params in GRID:
            r = run(train, 100_000, *params)
            results.append((params, r["final"], r["sharpe"]))
        pick_final = max(results, key=lambda x: x[1])[0]
        pick_sharpe = max(results, key=lambda x: (x[2] if not np.isnan(x[2]) else -9))[0]

        year_res = {}
        for name, params in (("wf_final", pick_final), ("wf_sharpe", pick_sharpe),
                             ("fixed_best", FIXED_BEST), ("fixed_orig", FIXED_ORIG)):
            seg = year_slice(weekly, year, params)
            r = run(seg, equity[name], *params)
            year_res[name] = r["final"] / equity[name] - 1
            equity[name] = r["final"]

        picks.append(dict(year=year, pick_by_final=pick_final, pick_by_sharpe=pick_sharpe,
                          **{f"{k}_ret": v for k, v in year_res.items()},
                          **{f"{k}_eq": equity[k] for k in equity}))
        print(f"{year}: tuned(final)={pick_final} ret {year_res['wf_final']:+.1%} | "
              f"tuned(sharpe)={pick_sharpe} ret {year_res['wf_sharpe']:+.1%} | "
              f"fixed_best {year_res['fixed_best']:+.1%} | fixed_orig {year_res['fixed_orig']:+.1%}")

    df = pd.DataFrame(picks)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "walk_forward_results.csv")
    df.to_csv(out, index=False)

    years_span = len(TEST_YEARS) - 0.5   # 2026 is half a year
    print("\n" + "=" * 60)
    print(f"WALK-FORWARD RESULT 2019–2026 (start {CAPITAL0:,} SEK)")
    print("=" * 60)
    for name, label in (("wf_final", "Re-tuned yearly (pick by return)"),
                        ("wf_sharpe", "Re-tuned yearly (pick by Sharpe)"),
                        ("fixed_best", f"Fixed {FIXED_BEST} (full-sample winner)"),
                        ("fixed_orig", f"Fixed {FIXED_ORIG} (original params)")):
        eq = equity[name]
        cagr = (eq / CAPITAL0) ** (1 / years_span) - 1
        monthly = (eq - CAPITAL0) / (years_span * 12)
        print(f"{label:42s} final {eq:>10,.0f}  CAGR {cagr:6.1%}  ~{monthly:,.0f} SEK/mo")
    print(f"\nResults written to {out}")


if __name__ == "__main__":
    main()
