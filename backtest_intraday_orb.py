"""
Intraday Opening-Range Breakout (ORB) — OMXS30, hourly bars
============================================================
The only intraday history Yahoo serves for free is ~60 days of minute bars but
~3 years of 60-minute bars, so this backtests on HOURLY data (Nordic session
09:00-17:00 = ~9 bars/day).

STRATEGY (long-only, flat overnight):
  - Each day the FIRST hourly bar (09:00) defines the opening range [OR_low, OR_high].
  - Go LONG when a later bar's high breaks above OR_high (fill at OR_high).
  - Hard stop: exit at OR_low if any later bar's low touches it.
  - Otherwise exit at the day's last bar close (17:00). Never hold overnight.
  - One trade per stock per day. Each day take the first MAX_POS breakouts
    (by time), equal-weight; empty slots sit in cash.
  - Costs: Nordnet 0.15% commission, min 39 SEK, charged both sides.

Run:  python backtest_intraday_orb.py
"""

import os
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TICKERS = [
    "ABB.ST", "ADDT-B.ST", "ALFA.ST", "ASSA-B.ST", "AZN.ST", "ATCO-A.ST",
    "BOL.ST", "EPI-A.ST", "EQT.ST", "ERIC-B.ST", "ESSITY-B.ST", "EVO.ST",
    "HM-B.ST", "HEXA-B.ST", "INVE-B.ST", "LIFCO-B.ST", "NIBE-B.ST", "NDA-SE.ST",
    "SAAB-B.ST", "SAND.ST", "SEB-A.ST", "SKA-B.ST", "SKF-B.ST", "SCA-B.ST",
    "SHB-A.ST", "SWED-A.ST", "TEL2-B.ST", "TELIA.ST", "VOLV-B.ST",
]

INITIAL_CAPITAL = 100000
MAX_POS = 5                 # max concurrent intraday positions per day
COMMISSION_PCT = 0.0015     # Nordnet 0.15%
COMMISSION_MIN = 39         # SEK min per side
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_hourly(ticker):
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        host = _YAHOO_HOSTS[attempt % 2]
        try:
            r = sess.get(f"https://{host}/v8/finance/chart/{ticker}",
                         params={"range": "730d", "interval": "60m"}, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("Europe/Stockholm")
                q = res["indicators"]["quote"][0]
                df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"]}, index=ts)
                return df.dropna()
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


print("Downloading hourly bars for OMXS30...")
data = {}
for t in TICKERS:
    df = fetch_hourly(t)
    if df is not None and len(df) > 50:
        data[t] = df
    time.sleep(0.1)
print(f"Usable tickers: {len(data)}")

# --- build per-(ticker, day) ORB trades ---
trades = []   # (date, ticker, breakout_order, gross_return, outcome)
for t, df in data.items():
    df = df.copy()
    df["day"] = df.index.date
    for day, bars in df.groupby("day"):
        bars = bars.sort_index()
        if len(bars) < 3:
            continue
        or_high = bars.iloc[0]["h"]
        or_low = bars.iloc[0]["l"]
        if or_low <= 0 or or_high <= or_low:
            continue
        rest = bars.iloc[1:]
        entered = False
        for j in range(len(rest)):
            bar = rest.iloc[j]
            if not entered and bar["h"] >= or_high:      # breakout
                entry = or_high
                exit_price, outcome = None, "EOD"
                for k in range(j, len(rest)):             # look for stop from breakout bar on
                    if rest.iloc[k]["l"] <= or_low:
                        exit_price, outcome = or_low, "STOP"
                        break
                if exit_price is None:
                    exit_price = rest.iloc[-1]["c"]
                trades.append((day, t, j, exit_price / entry - 1, outcome))
                entered = True
                break

tr = pd.DataFrame(trades, columns=["date", "ticker", "order", "gross", "outcome"])
tr["date"] = pd.to_datetime(tr["date"])

# --- portfolio: each day take first MAX_POS breakouts, equal weight, EOD flat ---
slice_cap = INITIAL_CAPITAL / MAX_POS
fee_pct = 2 * max(slice_cap * COMMISSION_PCT, COMMISSION_MIN) / slice_cap   # round-trip, per slice
tr["net"] = tr["gross"] - fee_pct

daily = []
for day, g in tr.groupby("date"):
    sel = g.sort_values("order").head(MAX_POS)
    day_ret = sel["net"].sum() / MAX_POS          # each slot = 1/MAX_POS of capital
    daily.append((day, day_ret, len(sel)))
dd = pd.DataFrame(daily, columns=["date", "ret", "n"]).set_index("date").sort_index()
dd["equity"] = INITIAL_CAPITAL * (1 + dd["ret"]).cumprod()

eq = dd["equity"]
years = (eq.index[-1] - eq.index[0]).days / 365.25
final = eq.iloc[-1]
cagr = (final / INITIAL_CAPITAL) ** (1 / years) - 1 if final > 0 else float("nan")
sharpe = (dd["ret"].mean() / dd["ret"].std()) * np.sqrt(252) if dd["ret"].std() > 0 else float("nan")
max_dd = (eq / eq.cummax() - 1).min()
avg_monthly = (final - INITIAL_CAPITAL) / (years * 12)
win_rate = (tr["net"] > 0).mean()
avg_net = tr["net"].mean()

print("\n" + "=" * 66)
print("INTRADAY OPENING-RANGE BREAKOUT — OMXS30, hourly bars")
print("=" * 66)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Tickers: {len(data)}   Total breakout trades: {len(tr):,}")
print(f"Initial capital: {INITIAL_CAPITAL:,.0f} SEK   Final equity: {final:,.0f} SEK")
print(f"CAGR: {cagr*100:.1f}%   Max drawdown: {max_dd*100:.1f}%   Sharpe: {sharpe:.2f}")
print(f"Avg implied monthly profit: {avg_monthly:,.0f} SEK  (target 2,000)")
print(f"Per-trade: win rate {win_rate*100:.1f}%   avg net return {avg_net*100:+.2f}%   "
      f"(round-trip fee {fee_pct*100:.2f}% per slice)")
print(f"Exits: {(tr.outcome=='EOD').sum()} EOD / {(tr.outcome=='STOP').sum()} stopped")
print(f"Gross (fee-free) per-trade avg: {tr['gross'].mean()*100:+.2f}%")
print("=" * 66)

fig, ax = plt.subplots(figsize=(11, 5))
ax.plot(eq.index, eq, color="#7c3aed", lw=1.2, label="ORB intraday equity")
ax.axhline(INITIAL_CAPITAL, color="gray", ls="--", lw=0.8)
ax.set_title("Intraday Opening-Range Breakout — OMXS30 (hourly)")
ax.set_ylabel("SEK"); ax.legend(); ax.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_intraday_orb_results.png"), dpi=150)
dd.to_csv(os.path.join(OUTPUT_DIR, "intraday_orb_equity.csv"))
tr.to_csv(os.path.join(OUTPUT_DIR, "intraday_orb_trades.csv"), index=False)
print("Saved chart + CSVs.")
