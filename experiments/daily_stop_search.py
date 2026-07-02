"""
Daily-checked and trailing stops on the champion momentum strategy — OMXS30
============================================================================
The champion (top-4, 12w momentum, buffer-4) ranks/rebalances WEEKLY, but its
15% stop is only checked on Fridays — so a mid-week crash exits at the next
weekly close (worst trade ~-24%). This tests two changes:

  1. Check the stop EVERY DAY (exit intra-week the day it triggers).
  2. Make it a TRAILING stop: ratchet the stop up from the position's peak
     close since entry, instead of a fixed level off the entry price.

Entries/ranking stay weekly (Friday); only the stop cadence/type changes.
Stops are evaluated on daily CLOSES (no intraday low — slightly optimistic;
a real broker stop order on the intraday low would trigger a touch more).

Run:  python experiments/daily_stop_search.py
Writes experiments/daily_stop_results.csv.
"""

import os

import numpy as np
import pandas as pd

CAP = 100_000
TOP_N = 4
MOM = 12
BUF = 4
SMA_W = 20
FEE_PCT = 0.0015
FEE_MIN = 39
EXPO = 0.85
PRICES = "/tmp/claude-0/-home-user-SwingTrading/31cffeec-15a6-5f82-b0bf-e77cd82b35d0/scratchpad/prices.csv"


def load():
    daily = pd.read_csv(PRICES, index_col=0, parse_dates=True)
    daily = daily.dropna(axis=1, thresh=int(len(daily) * 0.8)).ffill(limit=3)
    weekly = daily.resample("W-FRI").last().ffill(limit=2)
    return daily, weekly


def simulate(daily, weekly, stop_mode, stop_pct):
    """stop_mode: 'weekly_fixed' | 'daily_fixed' | 'daily_trail'."""
    mom = weekly.pct_change(MOM)
    sma = weekly.rolling(SMA_W).mean()
    trend = weekly > sma

    # rebalance schedule: map actual trading day -> weekly signal label
    labels = weekly.index[SMA_W + MOM:]
    pos_idx = daily.index.get_indexer(labels, method="ffill")
    rebal = {daily.index[i]: lab for i, lab in zip(pos_idx, labels) if i >= 0}
    week_of = {}  # weekly label -> integer week counter for hold length
    for k, lab in enumerate(labels):
        week_of[lab] = k

    cash = CAP
    pos = {}   # tkr -> dict(sh, entry, peak, wk)
    eq = []
    exits = {"STOP": 0, "RANK": 0}
    worst = 0.0
    holds = []
    start = daily.index.get_loc(daily.index[pos_idx[0]])

    last_wk = 0
    for d in daily.index[start:]:
        row = daily.loc[d]

        # ---- DAILY stop check (skip for weekly_fixed except on rebal days) ----
        do_stop = stop_mode != "weekly_fixed" or d in rebal
        if do_stop:
            for t in list(pos):
                p = row.get(t, np.nan)
                if pd.isna(p):
                    continue
                pos[t]["peak"] = max(pos[t]["peak"], p)
                if stop_mode == "daily_trail":
                    trig = p <= pos[t]["peak"] * (1 - stop_pct)
                else:
                    trig = p <= pos[t]["entry"] * (1 - stop_pct)
                if trig:
                    r = p / pos[t]["entry"] - 1
                    proceeds = pos[t]["sh"] * p
                    fee = max(proceeds * FEE_PCT, FEE_MIN)
                    cash += proceeds - fee
                    exits["STOP"] += 1
                    worst = min(worst, r)
                    holds.append(last_wk - pos[t]["wk"])
                    del pos[t]

        # ---- WEEKLY rebalance ----
        if d in rebal:
            lab = rebal[d]
            last_wk = week_of[lab]
            m = mom.loc[lab].dropna()
            tr = trend.loc[lab]
            ranked = m[[t for t in m.index if tr.get(t, False)]].sort_values(ascending=False)
            target = list(ranked.index[:TOP_N])
            hold_ok = set(ranked.index[:TOP_N + BUF])
            for t in list(pos):
                if t not in hold_ok:
                    p = row.get(t, np.nan)
                    if pd.isna(p):
                        continue
                    r = p / pos[t]["entry"] - 1
                    proceeds = pos[t]["sh"] * p
                    fee = max(proceeds * FEE_PCT, FEE_MIN)
                    cash += proceeds - fee
                    exits["RANK"] += 1
                    if r < 0:
                        worst = min(worst, r)
                    holds.append(last_wk - pos[t]["wk"])
                    del pos[t]
            port = cash + sum(pos[t]["sh"] * row.get(t, pos[t]["entry"]) for t in pos)
            alloc = port * EXPO / TOP_N
            for t in [x for x in target if x not in pos][:TOP_N - len(pos)]:
                p = row.get(t, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                spend = min(alloc, cash)
                if spend < 500:
                    continue
                sh = spend // p
                if sh <= 0:
                    continue
                cost = sh * p
                fee = max(cost * FEE_PCT, FEE_MIN)
                if cost + fee > cash:
                    continue
                cash -= cost + fee
                pos[t] = dict(sh=sh, entry=p, peak=p, wk=last_wk)

        eq.append((d, cash + sum(pos[t]["sh"] * row.get(t, pos[t]["entry"]) for t in pos)))

    eqs = pd.Series(dict(eq))
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    cagr = (eqs.iloc[-1] / CAP) ** (1 / yrs) - 1
    dr = eqs.pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else np.nan
    dd = (eqs / eqs.cummax() - 1).min()
    tot = exits["STOP"] + exits["RANK"]
    return dict(monthly=(eqs.iloc[-1] - CAP) / (yrs * 12), cagr=cagr, sharpe=sharpe,
                max_dd=dd, worst_trade=worst, stop_share=exits["STOP"] / tot if tot else 0,
                trades_yr=tot / yrs, mean_hold_w=float(np.mean(holds)) if holds else 0)


if __name__ == "__main__":
    daily, weekly = load()
    print(f"Daily bars: {len(daily)}, {daily.index[0].date()} -> {daily.index[-1].date()}\n")

    configs = [
        ("WEEKLY fixed 15% (champion)", "weekly_fixed", 0.15),
        ("DAILY  fixed 15%",           "daily_fixed",  0.15),
        ("DAILY  trail 15%",           "daily_trail",  0.15),
        ("DAILY  trail 20%",           "daily_trail",  0.20),
        ("DAILY  trail 25%",           "daily_trail",  0.25),
        ("DAILY  trail 30%",           "daily_trail",  0.30),
    ]
    rows = []
    for name, mode, pct in configs:
        r = simulate(daily, weekly, mode, pct)
        rows.append(dict(config=name, **r))
    df = pd.DataFrame(rows)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_stop_results.csv")
    df.to_csv(out, index=False)

    print("100,000 SEK — daily-checked & trailing stops (weekly ranking unchanged):\n")
    print(df.to_string(index=False, formatters={
        "monthly": "{:,.0f}".format, "cagr": "{:.1%}".format, "sharpe": "{:.2f}".format,
        "max_dd": "{:.1%}".format, "worst_trade": "{:.0%}".format, "stop_share": "{:.0%}".format,
        "trades_yr": "{:.0f}".format, "mean_hold_w": "{:.1f}".format}))
    print(f"\nResults written to {out}")
    print("Note: stops evaluated on daily CLOSES; a broker stop on the intraday low would trigger slightly more.")
