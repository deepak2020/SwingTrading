"""
Intraday Opening-Range Breakout — OMXS30 INDEX only (^OMX), hourly
==================================================================
Trades the index itself (one instrument, one position/day) so the cost is a
single round-trip spread/commission instead of 5 stock fees. This isolates the
question: does ORB have any GROSS edge on the index, and what round-trip cost
would it survive?

STRATEGY (long-only, flat overnight):
  - First hourly bar (09:00) sets the opening range [OR_low, OR_high].
  - Long when a later bar breaks OR_high (fill at OR_high); stop at OR_low;
    else exit at the day's last close. One trade/day, whole book each time.
  - Cost swept as a round-trip in basis points (instrument-dependent).

You'd trade this via an OMXS30 future / CFD / mini-future. Leverage would scale
returns AND risk; this runs unleveraged (1x) to measure the raw edge.

Run:  python backtest_intraday_orb_index.py
"""

import os
import time

import requests
import pandas as pd
import numpy as np

INDEX_TICKER = "^OMX"
INITIAL_CAPITAL = 100000
COST_BPS_GRID = [0, 2, 5, 10, 20]     # round-trip cost scenarios (basis points)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_hourly(ticker):
    from urllib.parse import quote
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        host = _YAHOO_HOSTS[attempt % 2]
        try:
            r = sess.get(f"https://{host}/v8/finance/chart/{quote(ticker)}",
                         params={"range": "730d", "interval": "60m"}, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("Europe/Stockholm")
                q = res["indicators"]["quote"][0]
                return pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"]},
                                    index=ts).dropna()
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("fetch failed")


print(f"Downloading hourly {INDEX_TICKER}...")
df = fetch_hourly(INDEX_TICKER)
df["day"] = df.index.date

trades = []
for day, bars in df.groupby("day"):
    bars = bars.sort_index()
    if len(bars) < 3:
        continue
    or_high, or_low = bars.iloc[0]["h"], bars.iloc[0]["l"]
    if or_low <= 0 or or_high <= or_low:
        continue
    rest = bars.iloc[1:]
    for j in range(len(rest)):
        if rest.iloc[j]["h"] >= or_high:                  # breakout
            entry, exit_price, outcome = or_high, None, "EOD"
            for k in range(j, len(rest)):
                if rest.iloc[k]["l"] <= or_low:
                    exit_price, outcome = or_low, "STOP"
                    break
            if exit_price is None:
                exit_price = rest.iloc[-1]["c"]
            trades.append((day, exit_price / entry - 1, outcome))
            break

tr = pd.DataFrame(trades, columns=["date", "gross", "outcome"])
tr["date"] = pd.to_datetime(tr["date"])
tr = tr.set_index("date").sort_index()
years = (tr.index[-1] - tr.index[0]).days / 365.25

print("\n" + "=" * 64)
print("INTRADAY ORB — OMXS30 INDEX (^OMX), hourly, long-only, flat EOD")
print("=" * 64)
print(f"Period: {tr.index[0].date()} to {tr.index[-1].date()} ({years:.1f} years)")
print(f"Breakout trades: {len(tr):,}   ({(tr.outcome=='EOD').sum()} EOD / {(tr.outcome=='STOP').sum()} stopped)")
print(f"GROSS edge per trade (fee-free): {tr['gross'].mean()*100:+.3f}%   "
      f"win rate {(tr['gross']>0).mean()*100:.1f}%")
print(f"GROSS total if never a fee: {INITIAL_CAPITAL*(1+tr['gross']).prod():,.0f} SEK\n")

print(f"{'round-trip cost':>16} | {'final SEK':>12} | {'CAGR':>7} | {'monthly':>9} | {'maxDD':>7} | {'Sharpe':>6}")
print("-" * 74)
for bps in COST_BPS_GRID:
    net = tr["gross"] - bps / 10000.0
    eq = INITIAL_CAPITAL * (1 + net).cumprod()
    final = eq.iloc[-1]
    cagr = (final / INITIAL_CAPITAL) ** (1 / years) - 1 if final > 0 else float("nan")
    sharpe = (net.mean() / net.std()) * np.sqrt(252) if net.std() > 0 else float("nan")
    max_dd = (eq / eq.cummax() - 1).min()
    monthly = (final - INITIAL_CAPITAL) / (years * 12)
    print(f"{bps:>13} bps | {final:>12,.0f} | {cagr*100:>6.1f}% | {monthly:>9,.0f} | "
          f"{max_dd*100:>6.1f}% | {sharpe:>6.2f}")
print("=" * 64)
print("Note: unleveraged. A future/CFD adds leverage (scales both return and DD).")
print("Typical round-trip: OMXS30 future ~1-3 bps, tight CFD ~2-5 bps, mini-future/cert wider.")

tr.to_csv(os.path.join(OUTPUT_DIR, "intraday_orb_index_trades.csv"))
print("Saved trades CSV.")
