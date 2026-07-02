"""
Hybrid experiment: S/R entries + trailing-stop exits — OMXS30
==============================================================
The support/resistance strategy (backtest_sr.py) has a 74% hit rate but caps
every winner at the channel top. This experiment keeps its entry logic
untouched (pullback to the 12-week Donchian low + bullish weekly close +
40-week SMA uptrend) and swaps the exit:

  exit_mode="resistance": original — TP near 12-week high, 12-week max hold
  exit_mode="trail":      exit when close falls trail_pct below the highest
                          close since entry; no TP cap, no max hold

Both modes keep the initial stop 5% below the support level at entry.
Sweeps trail widths at 50k and 100k SEK.

Run:  python experiments/sr_trailing_search.py
Writes experiments/sr_trailing_results.csv.
"""

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

SR_LOOKBACK_WEEKS = 12
SUPPORT_TOUCH_PCT = 0.03
RESISTANCE_TOUCH_PCT = 0.03
STOP_BELOW_SUPPORT_PCT = 0.05
MAX_HOLD_WEEKS = 12            # only used by exit_mode="resistance"
TREND_FILTER_SMA_WEEKS = 40
MAX_POSITIONS = 6
COMMISSION_PCT = 0.0015
COMMISSION_MIN = 39

_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_ohlc(tickers, start, end):
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    frames = {k: {} for k in ("Open", "High", "Low", "Close")}
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
                    q = res["indicators"]["quote"][0]
                    close = pd.Series(q["close"], index=idx, dtype=float)
                    adj_list = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
                    adj = pd.Series(adj_list, index=idx, dtype=float) if adj_list is not None else close
                    factor = (adj / close).fillna(1.0)
                    frames["Close"][tkr] = adj
                    for key, name in (("open", "Open"), ("high", "High"), ("low", "Low")):
                        frames[name][tkr] = pd.Series(q[key], index=idx, dtype=float) * factor
                    break
                time.sleep(0.6 * (attempt + 1))
            except Exception:
                time.sleep(0.6 * (attempt + 1))
        else:
            print(f"  ! could not fetch {tkr} (skipped)")
        time.sleep(0.15)
    return {k: pd.DataFrame(v).sort_index() for k, v in frames.items()}


def run(data, capital, exit_mode, trail_pct=0.15):
    w_close, w_high, w_low, w_open = data
    support = w_low.rolling(SR_LOOKBACK_WEEKS).min().shift(1)
    resistance = w_high.rolling(SR_LOOKBACK_WEEKS).max().shift(1)
    trend_sma = w_close.rolling(TREND_FILTER_SMA_WEEKS).mean()

    cash = capital
    positions = {}
    eq_dates, eq_vals = [], []
    counts = {"BUY": 0, "TAKE_PROFIT": 0, "STOP_LOSS": 0, "MAX_HOLD": 0, "TRAIL_STOP": 0}
    fees = 0.0

    start_idx = max(SR_LOOKBACK_WEEKS, TREND_FILTER_SMA_WEEKS) + 1
    dates = w_close.index[start_idx:]

    for week_idx, date in enumerate(dates):
        c = w_close.loc[date]
        l = w_low.loc[date]
        o = w_open.loc[date]
        sup = support.loc[date]
        res = resistance.loc[date]

        for tkr in list(positions):
            price = c.get(tkr, np.nan)
            if pd.isna(price):
                continue
            pos = positions[tkr]
            pos["peak"] = max(pos["peak"], price)
            held = week_idx - pos["entry_week_idx"]

            reason = None
            if price <= pos["support_at_entry"] * (1 - STOP_BELOW_SUPPORT_PCT):
                reason = "STOP_LOSS"
            elif exit_mode == "resistance":
                r = res.get(tkr, np.nan)
                if pd.notna(r) and price >= r * (1 - RESISTANCE_TOUCH_PCT):
                    reason = "TAKE_PROFIT"
                elif held >= MAX_HOLD_WEEKS:
                    reason = "MAX_HOLD"
            else:  # trail
                if price <= pos["peak"] * (1 - trail_pct):
                    reason = "TRAIL_STOP"

            if reason:
                proceeds = pos["shares"] * price
                fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
                cash += proceeds - fee
                fees += fee
                counts[reason] += 1
                del positions[tkr]

        port = cash + sum(positions[t]["shares"] * c.get(t, positions[t]["entry_price"]) for t in positions)
        slots = MAX_POSITIONS - len(positions)
        if slots > 0:
            cands = []
            for tkr in w_close.columns:
                if tkr in positions:
                    continue
                s = sup.get(tkr, np.nan)
                low_t, close_t, open_t = l.get(tkr, np.nan), c.get(tkr, np.nan), o.get(tkr, np.nan)
                if pd.isna(s) or pd.isna(low_t) or pd.isna(close_t) or pd.isna(open_t):
                    continue
                sma_t = trend_sma.loc[date].get(tkr, np.nan)
                if (low_t <= s * (1 + SUPPORT_TOUCH_PCT) and close_t > open_t
                        and pd.notna(sma_t) and close_t > sma_t):
                    cands.append((tkr, (close_t - s) / s))
            cands.sort(key=lambda x: x[1])
            alloc = port / MAX_POSITIONS
            for tkr, _ in cands[:slots]:
                price = c.get(tkr, np.nan)
                if pd.isna(price) or price <= 0:
                    continue
                spend = min(alloc, cash)
                if spend < 500:
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
                counts["BUY"] += 1
                positions[tkr] = {"shares": sh, "entry_price": price, "peak": price,
                                  "entry_week_idx": week_idx,
                                  "support_at_entry": sup.get(tkr, price * 0.9)}

        port = cash + sum(positions[t]["shares"] * c.get(t, positions[t]["entry_price"]) for t in positions)
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
                monthly=monthly, fees=fees, **counts)


if __name__ == "__main__":
    print("Downloading data...")
    raw = fetch_ohlc(TICKERS, START_DATE, END_DATE)
    close = raw["Close"].dropna(axis=1, thresh=int(len(raw["Close"]) * 0.8))
    data = (
        close.resample("W-FRI").last().ffill(limit=2),
        raw["High"][close.columns].resample("W-FRI").max(),
        raw["Low"][close.columns].resample("W-FRI").min(),
        raw["Open"][close.columns].resample("W-FRI").first(),
    )
    print(f"Usable tickers: {len(close.columns)}")

    rows = []
    for cap in (50_000, 100_000):
        r = run(data, cap, "resistance")
        rows.append(dict(capital=cap, exit="TP@resistance", trail=np.nan, **r))
        for trail in (0.08, 0.10, 0.15, 0.20, 0.25):
            r = run(data, cap, "trail", trail_pct=trail)
            rows.append(dict(capital=cap, exit="trail", trail=trail, **r))

    df = pd.DataFrame(rows)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sr_trailing_results.csv")
    df.to_csv(out, index=False)
    print(df.to_string(index=False,
          formatters={"final": "{:,.0f}".format, "cagr": "{:.1%}".format,
                      "sharpe": "{:.2f}".format, "max_dd": "{:.1%}".format,
                      "monthly": "{:,.0f}".format, "fees": "{:,.0f}".format,
                      "trail": lambda v: "-" if pd.isna(v) else f"{v:.0%}"}))
    print(f"\nResults written to {out}")
