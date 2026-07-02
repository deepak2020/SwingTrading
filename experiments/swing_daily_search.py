"""
Daily-timeframe swing trading search — OMXS30
==============================================
The weekly S/R strategy holds for months; real swing trading lives on daily
bars. This experiment runs the same support/resistance bounce concept on
DAILY data: buy a bounce off the rolling N-day low, take profit at the rolling
N-day high, stop below the entry support, time-limited hold. Sweeps channel
length, stop width, max hold and trend filter at 100,000 SEK.

Reported per config: monthly SEK, CAGR, Sharpe, max DD, trades/year, win
rate, average holding days, fees. Fee model: Nordnet 0.15%, min 39 SEK.

Run:  python experiments/swing_daily_search.py
Writes experiments/swing_daily_results.csv.
"""

import itertools
import os

import numpy as np
import pandas as pd

from sr_trailing_search import fetch_ohlc, TICKERS, START_DATE, END_DATE

CAPITAL = 100_000
MAX_POSITIONS = 6
TOUCH_PCT = 0.03            # near support / near resistance = within 3%
COMMISSION_PCT = 0.0015
COMMISSION_MIN = 39


def run_daily(arrs, lookback, stop_pct, max_hold, use_trend):
    o, h, l, c, sma200, dates = arrs
    n_days, n_tkr = c.shape

    # rolling Donchian bands, shifted 1 day (no lookahead)
    sup = pd.DataFrame(l).rolling(lookback).min().shift(1).values
    res = pd.DataFrame(h).rolling(lookback).max().shift(1).values

    cash = CAPITAL
    pos = {}      # j -> [shares, entry_i, sup_at_entry, entry_price]
    eq = np.empty(n_days - lookback)
    wins = losses = 0
    hold_days_sum = 0
    fees = 0.0
    n_buys = 0

    for k, i in enumerate(range(lookback, n_days)):
        # exits
        for j in list(pos):
            price = c[i, j]
            if np.isnan(price):
                continue
            sh, ei, s0, ep = pos[j]
            held = i - ei
            reason = None
            if not np.isnan(res[i, j]) and price >= res[i, j] * (1 - TOUCH_PCT):
                reason = 1
            elif price <= s0 * (1 - stop_pct):
                reason = -1
            elif held >= max_hold:
                reason = 0
            if reason is not None:
                proceeds = sh * price
                fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                cash += proceeds - fee
                fees += fee
                hold_days_sum += held
                if price > ep:
                    wins += 1
                else:
                    losses += 1
                del pos[j]

        port = cash + sum(v[0] * (c[i, j] if not np.isnan(c[i, j]) else v[3]) for j, v in pos.items())
        slots = MAX_POSITIONS - len(pos)
        if slots > 0:
            cands = []
            for j in range(n_tkr):
                if j in pos:
                    continue
                s, low_t, close_t, open_t = sup[i, j], l[i, j], c[i, j], o[i, j]
                if np.isnan(s) or np.isnan(low_t) or np.isnan(close_t) or np.isnan(open_t):
                    continue
                if use_trend and (np.isnan(sma200[i, j]) or close_t <= sma200[i, j]):
                    continue
                if low_t <= s * (1 + TOUCH_PCT) and close_t > open_t:
                    cands.append((j, (close_t - s) / s))
            cands.sort(key=lambda x: x[1])
            alloc = port / MAX_POSITIONS
            for j, _ in cands[:slots]:
                price = c[i, j]
                spend = min(alloc, cash)
                if spend < 500 or price <= 0:
                    continue
                sh = spend // price
                if sh <= 0:
                    continue
                cost = sh * price
                fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
                if cost + fee > cash:
                    continue
                cash -= cost + fee
                fees += fee
                n_buys += 1
                pos[j] = [sh, i, s if not np.isnan(s) else price * 0.95, price]

        eq[k] = cash + sum(v[0] * (c[i, j] if not np.isnan(c[i, j]) else v[3]) for j, v in pos.items())

    years = (dates[-1] - dates[lookback]).days / 365.25
    cagr = (eq[-1] / CAPITAL) ** (1 / years) - 1
    dr = pd.Series(eq).pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else np.nan
    max_dd = (pd.Series(eq) / pd.Series(eq).cummax() - 1).min()
    closed = wins + losses
    return dict(final=eq[-1], cagr=cagr, sharpe=sharpe, max_dd=max_dd,
                monthly=(eq[-1] - CAPITAL) / (years * 12),
                trades_yr=n_buys / years,
                win_rate=wins / closed if closed else np.nan,
                avg_hold_d=hold_days_sum / closed if closed else np.nan,
                fees=fees, buys=n_buys)


if __name__ == "__main__":
    print("Downloading data...")
    raw = fetch_ohlc(TICKERS, START_DATE, END_DATE)
    close = raw["Close"].dropna(axis=1, thresh=int(len(raw["Close"]) * 0.8))
    cols = close.columns
    c = close.values
    o = raw["Open"][cols].values
    h = raw["High"][cols].values
    l = raw["Low"][cols].values
    sma200 = close.rolling(200).mean().values
    dates = close.index
    arrs = (o, h, l, c, sma200, dates)
    print(f"Usable tickers: {len(cols)}, days: {len(dates)}")

    grid = list(itertools.product(
        [20, 40, 60, 90],      # lookback days (Donchian window)
        [0.03, 0.05],          # stop below entry support
        [15, 30, 60],          # max hold (trading days)
        [True, False],         # 200d SMA trend filter
    ))
    rows = []
    for lb, sl, mh, tf in grid:
        r = run_daily(arrs, lb, sl, mh, tf)
        rows.append(dict(lookback_d=lb, stop=sl, max_hold_d=mh, trend=tf, **r))
    df = pd.DataFrame(rows).sort_values("monthly", ascending=False)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swing_daily_results.csv")
    df.to_csv(out, index=False)

    fmt = {"final": "{:,.0f}".format, "cagr": "{:.1%}".format, "sharpe": "{:.2f}".format,
           "max_dd": "{:.1%}".format, "monthly": "{:,.0f}".format, "trades_yr": "{:.0f}".format,
           "win_rate": "{:.0%}".format, "avg_hold_d": "{:.0f}".format, "fees": "{:,.0f}".format}
    print(f"\nTop 12 of {len(df)} configs at {CAPITAL:,} SEK (daily bars):")
    print(df.head(12).to_string(index=False, formatters=fmt))
    print("\nBottom 3:")
    print(df.tail(3).to_string(index=False, formatters=fmt))
    print(f"\nConfigs above 2,000 SEK/month: {(df.monthly > 2000).sum()} of {len(df)}")
    print(f"Results written to {out}")
