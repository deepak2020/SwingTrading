"""
Parameter sweep of the OMXS30 weekly momentum strategy at 100,000 SEK.
=======================================================================
Searches variants of the base strategy in backtest.py to find configurations
that clear the 2,000 SEK/month target. Same universe, trend filter, and fee
model (Nordnet 0.15%, min 39 SEK). Two turnover-control knobs are added on
top of the base rules:

  - rebalance_weeks: re-rank every k weeks instead of every week
                     (stop-losses are still checked weekly)
  - rank_buffer:     keep holding a position while its momentum rank stays
                     within top_n + buffer (only buy from the top_n)

Run:  python experiments/strategy_search.py
Writes experiments/sweep_results_100k.csv sorted by avg monthly SEK profit.
"""

import itertools
import os
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

TICKERS = [
    "ABB.ST","ADDT-B.ST","ALFA.ST","ASSA-B.ST","AZN.ST","ATCO-A.ST",
    "BOL.ST","EPI-A.ST","EQT.ST","ERIC-B.ST","ESSITY-B.ST","EVO.ST",
    "HM-B.ST","HEXA-B.ST","INVE-B.ST","LIFCO-B.ST","NIBE-B.ST","NDA-SE.ST",
    "SAAB-B.ST","SAND.ST","SEB-A.ST","SKA-B.ST","SKF-B.ST","SCA-B.ST",
    "SHB-A.ST","SWED-A.ST","TEL2-B.ST","TELIA.ST","VOLV-B.ST"
]
START_DATE = "2015-01-01"
END_DATE = "2026-06-30"
CAPITAL = 100_000
COMMISSION_PCT = 0.0015
COMMISSION_MIN = 39
MAX_EXPOSURE = 0.85
TREND_SMA_WEEKS = 20

_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_adjusted_closes(tickers, start, end):
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    series = {}
    for tkr in tickers:
        for attempt in range(4):
            host = _YAHOO_HOSTS[attempt % 2]
            try:
                r = sess.get(
                    f"https://{host}/v8/finance/chart/{tkr}",
                    params={"period1": p1, "period2": p2, "interval": "1d",
                            "events": "div,splits"},
                    timeout=30,
                )
                if r.status_code == 200:
                    res = r.json()["chart"]["result"][0]
                    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                    ind = res["indicators"]
                    adj = ind.get("adjclose", [{}])[0].get("adjclose") if "adjclose" in ind else None
                    close = adj if adj is not None else ind["quote"][0]["close"]
                    series[tkr] = pd.Series(close, index=idx)
                    break
                time.sleep(0.6 * (attempt + 1))
            except Exception:
                time.sleep(0.6 * (attempt + 1))
        else:
            print(f"  ! could not fetch {tkr} (skipped)")
        time.sleep(0.15)
    return pd.DataFrame(series).sort_index()


def run(weekly, capital, top_n, momentum_weeks, rebalance_weeks, rank_buffer, stop_pct):
    momentum = weekly.pct_change(momentum_weeks)
    sma = weekly.rolling(TREND_SMA_WEEKS).mean()
    trend_ok = weekly > sma

    cash = capital
    positions = {}
    eq_dates, eq_vals = [], []
    n_buys = n_stops = n_exits = 0
    fees = 0.0

    dates = weekly.index[TREND_SMA_WEEKS + momentum_weeks:]
    for wk, date in enumerate(dates):
        prices = weekly.loc[date]

        # stop-losses: checked weekly regardless of rebalance cadence
        for tkr in list(positions):
            p = prices.get(tkr, np.nan)
            if pd.isna(p):
                continue
            if p <= positions[tkr]["entry"] * (1 - stop_pct):
                proceeds = positions[tkr]["sh"] * p
                fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                cash += proceeds - fee
                fees += fee
                n_stops += 1
                del positions[tkr]

        if wk % rebalance_weeks == 0:
            mom = momentum.loc[date].dropna()
            tr = trend_ok.loc[date]
            valid = [t for t in mom.index if tr.get(t, False)]
            ranked = mom[valid].sort_values(ascending=False)
            target = list(ranked.index[:top_n])
            hold_ok = set(ranked.index[:top_n + rank_buffer])

            for tkr in list(positions):
                if tkr not in hold_ok:
                    p = prices.get(tkr, np.nan)
                    if pd.isna(p):
                        continue
                    proceeds = positions[tkr]["sh"] * p
                    fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                    cash += proceeds - fee
                    fees += fee
                    n_exits += 1
                    del positions[tkr]

            port = cash + sum(positions[t]["sh"] * prices.get(t, positions[t]["entry"]) for t in positions)
            alloc = port * MAX_EXPOSURE / top_n
            slots = top_n - len(positions)
            if slots > 0:
                for tkr in [t for t in target if t not in positions][:slots]:
                    p = prices.get(tkr, np.nan)
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
                    positions[tkr] = {"sh": sh, "entry": p}

        port = cash + sum(positions[t]["sh"] * prices.get(t, positions[t]["entry"]) for t in positions)
        eq_dates.append(date)
        eq_vals.append(port)

    eq = pd.Series(eq_vals, index=eq_dates)
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / capital) ** (1 / years) - 1
    wr = eq.pct_change().dropna()
    sharpe = wr.mean() / wr.std() * np.sqrt(52) if wr.std() > 0 else np.nan
    max_dd = (eq / eq.cummax() - 1).min()
    monthly = (eq.iloc[-1] - capital) / (years * 12)
    return dict(final=eq.iloc[-1], cagr=cagr, sharpe=sharpe, max_dd=max_dd,
                monthly=monthly, fees=fees, trades=n_buys + n_stops + n_exits,
                buys=n_buys, stops=n_stops, exits=n_exits)


if __name__ == "__main__":
    print("Downloading data...")
    raw = fetch_adjusted_closes(TICKERS, START_DATE, END_DATE)
    raw = raw.dropna(axis=1, thresh=int(len(raw) * 0.8))
    weekly = raw.resample("W-FRI").last().ffill(limit=2)
    print(f"Usable tickers: {len(raw.columns)}")

    grid = list(itertools.product(
        [4, 6, 8],           # top_n
        [8, 12, 26],         # momentum_weeks
        [1, 2, 4],           # rebalance_weeks
        [0, 4, 8],           # rank_buffer
        [0.10, 0.15],        # stop_pct
    ))
    rows = []
    for tn, mw, rb, buf, sl in grid:
        r = run(weekly, CAPITAL, tn, mw, rb, buf, sl)
        rows.append(dict(top_n=tn, mom_w=mw, reb_w=rb, buf=buf, stop=sl, **r))
    df = pd.DataFrame(rows).sort_values("monthly", ascending=False)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sweep_results_100k.csv")
    df.to_csv(out, index=False)

    base = run(weekly, CAPITAL, 6, 12, 1, 0, 0.10)
    print("\nBaseline (original params) at 100k:",
          f"monthly {base['monthly']:,.0f} SEK, CAGR {base['cagr']:.1%}, fees {base['fees']:,.0f}")
    print(f"\nTop 10 of {len(df)} combos by avg monthly SEK:")
    print(df.head(10).to_string(index=False,
          formatters={"final": "{:,.0f}".format, "cagr": "{:.1%}".format,
                      "sharpe": "{:.2f}".format, "max_dd": "{:.1%}".format,
                      "monthly": "{:,.0f}".format, "fees": "{:,.0f}".format}))
    print(f"\nCombos above 2,000 SEK/month: {(df.monthly > 2000).sum()} of {len(df)}")
    print(f"Results written to {out}")
