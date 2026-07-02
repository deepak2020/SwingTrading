"""
Weekly Systematic Momentum Swing Trading Backtest — OMXS30
=============================================================
Fully systematic rules:
  1. Universe: OMXS30 constituents
  2. Trend filter: price must be above its 20-week SMA
  3. Signal: rank by 12-week momentum (total return)
  4. Entry: buy top N ranked stocks (equal weight), every Friday close
  5. Exit: sell if stock drops out of top N or breaks trend filter
  6. Risk mgmt: per-position stop-loss, max exposure cap
  7. Costs: modeled using Nordnet 0.15% commission (min 39 SEK) + 0.25% FX (n/a, all SEK)
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

# ---------------------------------------------------------------
# 1. CONFIG
# ---------------------------------------------------------------
TICKERS = [
    "ABB.ST","ADDT-B.ST","ALFA.ST","ASSA-B.ST","AZN.ST","ATCO-A.ST",
    "BOL.ST","EPI-A.ST","EQT.ST","ERIC-B.ST","ESSITY-B.ST","EVO.ST",
    "HM-B.ST","HEXA-B.ST","INVE-B.ST","LIFCO-B.ST","NIBE-B.ST","NDA-SE.ST",
    "SAAB-B.ST","SAND.ST","SEB-A.ST","SKA-B.ST","SKF-B.ST","SCA-B.ST",
    "SHB-A.ST","SWED-A.ST","TEL2-B.ST","TELIA.ST","VOLV-B.ST"
]

START_DATE = "2015-01-01"
END_DATE = "2026-06-30"
INITIAL_CAPITAL = 50000       # SEK
TOP_N = 6                     # positions held at once
MOMENTUM_WEEKS = 12
TREND_SMA_WEEKS = 20
STOP_LOSS_PCT = 0.10          # 10% stop per position
MAX_EXPOSURE = 0.85           # max % of capital invested at once
COMMISSION_PCT = 0.0015       # Nordnet 0.15%
COMMISSION_MIN = 39           # SEK minimum per trade

# Output directory. Defaults to ./outputs next to this script; override with the
# OUTPUT_DIR env var. Created automatically so the script runs in any environment.
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------
# 2. DOWNLOAD DATA
# ---------------------------------------------------------------
# Adjusted daily closes are pulled straight from Yahoo's public chart API with
# requests. (yfinance's crumb/cookie flow gets rate-limited behind a TLS-
# re-terminating egress proxy; the plain chart endpoint returns 200 reliably.)
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


print("Downloading data...")
raw = fetch_adjusted_closes(TICKERS, START_DATE, END_DATE)
raw = raw.dropna(axis=1, thresh=int(len(raw)*0.8))  # drop tickers with too much missing data
print(f"Usable tickers: {list(raw.columns)}")

weekly = raw.resample("W-FRI").last()
weekly = weekly.ffill(limit=2)

# ---------------------------------------------------------------
# 3. SIGNALS
# ---------------------------------------------------------------
momentum = weekly.pct_change(MOMENTUM_WEEKS)
sma = weekly.rolling(TREND_SMA_WEEKS).mean()
trend_ok = weekly > sma

# ---------------------------------------------------------------
# 4. BACKTEST LOOP
# ---------------------------------------------------------------
cash = INITIAL_CAPITAL
positions = {}   # ticker -> {shares, entry_price}
equity_curve = []
trade_log = []

dates = weekly.index[TREND_SMA_WEEKS+MOMENTUM_WEEKS:]

for date in dates:
    prices_today = weekly.loc[date]

    # --- check stop losses first ---
    for tkr in list(positions.keys()):
        entry = positions[tkr]["entry_price"]
        price = prices_today.get(tkr, np.nan)
        if pd.isna(price):
            continue
        if price <= entry * (1 - STOP_LOSS_PCT):
            shares = positions[tkr]["shares"]
            proceeds = shares * price
            fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
            cash += proceeds - fee
            trade_log.append((date, tkr, "STOP_LOSS", price, shares, fee))
            del positions[tkr]

    # --- rank candidates ---
    mom_today = momentum.loc[date].dropna()
    trend_today = trend_ok.loc[date]
    valid = [t for t in mom_today.index if trend_today.get(t, False)]
    ranked = mom_today[valid].sort_values(ascending=False)
    target = list(ranked.index[:TOP_N])

    # --- exit positions no longer in target (and not already stopped) ---
    for tkr in list(positions.keys()):
        if tkr not in target:
            price = prices_today.get(tkr, np.nan)
            if pd.isna(price):
                continue
            shares = positions[tkr]["shares"]
            proceeds = shares * price
            fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
            cash += proceeds - fee
            trade_log.append((date, tkr, "RANK_EXIT", price, shares, fee))
            del positions[tkr]

    # --- compute portfolio value & entry new positions ---
    port_value = cash + sum(
        positions[t]["shares"] * prices_today.get(t, positions[t]["entry_price"])
        for t in positions
    )
    max_invest = port_value * MAX_EXPOSURE
    slots_open = TOP_N - len(positions)

    if slots_open > 0:
        candidates = [t for t in target if t not in positions]
        alloc_per_position = max_invest / TOP_N if TOP_N > 0 else 0
        for tkr in candidates[:slots_open]:
            price = prices_today.get(tkr, np.nan)
            if pd.isna(price) or price <= 0:
                continue
            spend = min(alloc_per_position, cash)
            if spend < 500:  # too small to bother
                continue
            shares = spend // price
            if shares <= 0:
                continue
            cost = shares * price
            fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
            if cost + fee > cash:
                continue
            cash -= (cost + fee)
            positions[tkr] = {"shares": shares, "entry_price": price}
            trade_log.append((date, tkr, "BUY", price, shares, fee))

    # --- record equity ---
    port_value = cash + sum(
        positions[t]["shares"] * prices_today.get(t, positions[t]["entry_price"])
        for t in positions
    )
    equity_curve.append((date, port_value, cash, len(positions)))

# ---------------------------------------------------------------
# 5. RESULTS
# ---------------------------------------------------------------
eq = pd.DataFrame(equity_curve, columns=["date","equity","cash","n_positions"]).set_index("date")
trades = pd.DataFrame(trade_log, columns=["date","ticker","action","price","shares","fee"])

total_return = eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1
years = (eq.index[-1] - eq.index[0]).days / 365.25
cagr = (eq["equity"].iloc[-1] / INITIAL_CAPITAL) ** (1/years) - 1
weekly_returns = eq["equity"].pct_change().dropna()
sharpe = (weekly_returns.mean() / weekly_returns.std()) * np.sqrt(52) if weekly_returns.std() > 0 else np.nan
running_max = eq["equity"].cummax()
drawdown = eq["equity"] / running_max - 1
max_dd = drawdown.min()

n_buys = (trades["action"]=="BUY").sum()
n_stops = (trades["action"]=="STOP_LOSS").sum()
n_rankexits = (trades["action"]=="RANK_EXIT").sum()
total_fees = trades["fee"].sum()

avg_monthly_sek = (eq["equity"].iloc[-1] - INITIAL_CAPITAL) / (years*12)

print("\n" + "="*60)
print("BACKTEST RESULTS — Weekly Momentum Swing Strategy (OMXS30)")
print("="*60)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Initial capital: {INITIAL_CAPITAL:,.0f} SEK")
print(f"Final equity: {eq['equity'].iloc[-1]:,.0f} SEK")
print(f"Total return: {total_return*100:.1f}%")
print(f"CAGR: {cagr*100:.1f}%")
print(f"Max drawdown: {max_dd*100:.1f}%")
print(f"Sharpe ratio (weekly, annualized): {sharpe:.2f}")
print(f"Total trades: {n_buys} buys / {n_stops} stop-losses / {n_rankexits} rank exits")
print(f"Total fees paid: {total_fees:,.0f} SEK")
print(f"Average implied monthly profit: {avg_monthly_sek:,.0f} SEK  (target was 2,000 SEK)")
print("="*60)

# ---------------------------------------------------------------
# 6. PLOT
# ---------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(11,7), sharex=True, gridspec_kw={"height_ratios":[3,1]})
axes[0].plot(eq.index, eq["equity"], color="#2563eb", linewidth=1.6, label="Strategy equity")
axes[0].axhline(INITIAL_CAPITAL, color="gray", linestyle="--", linewidth=0.8, label="Initial capital")
axes[0].set_ylabel("Portfolio value (SEK)")
axes[0].set_title("Weekly Momentum Swing Strategy — OMXS30 Backtest")
axes[0].legend()
axes[0].grid(alpha=0.3)

axes[1].fill_between(eq.index, drawdown*100, 0, color="#dc2626", alpha=0.4)
axes[1].set_ylabel("Drawdown (%)")
axes[1].grid(alpha=0.3)

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_results.png"), dpi=150)
print("\nChart saved.")

eq.to_csv(os.path.join(OUTPUT_DIR, "equity_curve.csv"))
trades.to_csv(os.path.join(OUTPUT_DIR, "trade_log.csv"), index=False)
print("CSV files saved.")
