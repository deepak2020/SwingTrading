"""
Shorter-hold "classic swing" variants of the momentum strategy — OMXS30
=======================================================================
The champion (top-4, 12-week momentum, rank-buffer 4) holds ~6 weeks. This
searches for configs that hold 2-3 weeks instead — faster momentum lookback,
smaller/zero rank buffer, and an optional hard max-hold cap — and reports the
economics so the turnover/fee tradeoff is explicit.

Reports per config: median & mean hold (weeks), trades/year, fees, monthly
SEK, CAGR, Sharpe, max drawdown — at 100,000 SEK.

Run:  python experiments/hold_period_search.py
Writes experiments/hold_period_results.csv.
"""

import itertools
import os

import numpy as np
import pandas as pd

from strategy_search import fetch_adjusted_closes, TICKERS, START_DATE, END_DATE

CAPITAL = 100_000
TREND_SMA_WEEKS = 20
MAX_EXPOSURE = 0.85
COMMISSION_PCT = 0.0015
COMMISSION_MIN = 39


def run(weekly, top_n, momentum_weeks, rank_buffer, stop_pct, max_hold_weeks=0):
    momentum = weekly.pct_change(momentum_weeks)
    sma = weekly.rolling(TREND_SMA_WEEKS).mean()
    trend_ok = weekly > sma

    cash = CAPITAL
    pos = {}   # tkr -> [shares, entry_price, entry_week]
    eq_vals = []
    holds = []
    fees = 0.0
    n_buys = 0

    dates = weekly.index[TREND_SMA_WEEKS + momentum_weeks:]
    for wi, date in enumerate(dates):
        px = weekly.loc[date]

        # stop-loss + max-hold exits
        for t in list(pos):
            p = px.get(t, np.nan)
            if pd.isna(p):
                continue
            hit_stop = p <= pos[t][1] * (1 - stop_pct)
            hit_time = max_hold_weeks and (wi - pos[t][2]) >= max_hold_weeks
            if hit_stop or hit_time:
                proceeds = pos[t][0] * p
                fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                cash += proceeds - fee
                fees += fee
                holds.append(wi - pos[t][2])
                del pos[t]

        mom = momentum.loc[date].dropna()
        tr = trend_ok.loc[date]
        ranked = mom[[t for t in mom.index if tr.get(t, False)]].sort_values(ascending=False)
        target = list(ranked.index[:top_n])
        hold_ok = set(ranked.index[:top_n + rank_buffer])

        for t in list(pos):
            if t not in hold_ok:
                p = px.get(t, np.nan)
                if pd.isna(p):
                    continue
                proceeds = pos[t][0] * p
                fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                cash += proceeds - fee
                fees += fee
                holds.append(wi - pos[t][2])
                del pos[t]

        port = cash + sum(pos[t][0] * px.get(t, pos[t][1]) for t in pos)
        alloc = port * MAX_EXPOSURE / top_n
        for t in [x for x in target if x not in pos][:top_n - len(pos)]:
            p = px.get(t, np.nan)
            if pd.isna(p) or p <= 0:
                continue
            spend = min(alloc, cash)
            if spend < 500:
                continue
            sh = spend // p
            if sh <= 0:
                continue
            cost = sh * p
            fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
            if cost + fee > cash:
                continue
            cash -= cost + fee
            fees += fee
            n_buys += 1
            pos[t] = [sh, p, wi]

        eq_vals.append(cash + sum(pos[t][0] * px.get(t, pos[t][1]) for t in pos))

    eq = pd.Series(eq_vals, index=dates)
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / CAPITAL) ** (1 / years) - 1
    wr = eq.pct_change().dropna()
    sharpe = wr.mean() / wr.std() * np.sqrt(52) if wr.std() > 0 else np.nan
    max_dd = (eq / eq.cummax() - 1).min()
    holds = holds or [0]
    return dict(median_hold=float(np.median(holds)), mean_hold=float(np.mean(holds)),
                trades_yr=n_buys / years, fees=fees,
                monthly=(eq.iloc[-1] - CAPITAL) / (years * 12),
                cagr=cagr, sharpe=sharpe, max_dd=max_dd)


if __name__ == "__main__":
    print("Downloading data...")
    raw = fetch_adjusted_closes(TICKERS, START_DATE, END_DATE)
    raw = raw.dropna(axis=1, thresh=int(len(raw) * 0.8))
    weekly = raw.resample("W-FRI").last().ffill(limit=2)
    print(f"Usable tickers: {len(raw.columns)}\n")

    configs = [
        ("CHAMPION mom12 buf4", dict(top_n=4, momentum_weeks=12, rank_buffer=4, stop_pct=0.15)),
        ("mom6  buf0",          dict(top_n=4, momentum_weeks=6,  rank_buffer=0, stop_pct=0.15)),
        ("mom4  buf0",          dict(top_n=4, momentum_weeks=4,  rank_buffer=0, stop_pct=0.15)),
        ("mom4  buf1",          dict(top_n=4, momentum_weeks=4,  rank_buffer=1, stop_pct=0.15)),
        ("mom3  buf0",          dict(top_n=4, momentum_weeks=3,  rank_buffer=0, stop_pct=0.15)),
        ("mom6  buf0 cap3w",    dict(top_n=4, momentum_weeks=6,  rank_buffer=0, stop_pct=0.15, max_hold_weeks=3)),
        ("mom4  buf0 cap2w",    dict(top_n=4, momentum_weeks=4,  rank_buffer=0, stop_pct=0.15, max_hold_weeks=2)),
        ("mom8  buf2",          dict(top_n=4, momentum_weeks=8,  rank_buffer=2, stop_pct=0.15)),
    ]
    rows = []
    for name, cfg in configs:
        r = run(weekly, **cfg)
        rows.append(dict(config=name, **r))
    df = pd.DataFrame(rows)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hold_period_results.csv")
    df.to_csv(out, index=False)

    print("100,000 SEK — shorter-hold momentum variants (target 2-3 week holds):\n")
    print(df.to_string(index=False, formatters={
        "median_hold": "{:.0f}w".format, "mean_hold": "{:.1f}w".format,
        "trades_yr": "{:.0f}".format, "fees": "{:,.0f}".format,
        "monthly": "{:,.0f}".format, "cagr": "{:.1%}".format,
        "sharpe": "{:.2f}".format, "max_dd": "{:.1%}".format}))
    print(f"\nResults written to {out}")
