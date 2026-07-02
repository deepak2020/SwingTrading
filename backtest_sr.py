"""
Weekly Systematic Support/Resistance Swing Trading Backtest — OMXS30
=======================================================================
Fully systematic rules:
  1. Universe: OMXS30 constituents
  2. Support  = rolling N-week LOW  (Donchian channel lower band)
  3. Resistance = rolling N-week HIGH (Donchian channel upper band)
  4. Entry: weekly low touches near support + bullish close + (optional) uptrend filter
  5. Take-profit: close near resistance
  6. Stop-loss: close clearly below support (level failed)
  7. Max holding period: exit if stuck too long without hitting TP or SL
  8. Costs: Nordnet 0.15% commission (min 39 SEK)

Positions are independent per-stock (not ranked/rotated), so trade
frequency is driven by how often price actually reaches range extremes —
should be much lower than the momentum rotation strategy.
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
INITIAL_CAPITAL = int(os.environ.get("CAPITAL", 50000))   # SEK

SR_LOOKBACK_WEEKS = 12          # Donchian channel window for support/resistance
SUPPORT_TOUCH_PCT = 0.03        # "near support" = within 3% of rolling low
RESISTANCE_TOUCH_PCT = 0.03     # "near resistance" = within 3% of rolling high
STOP_BELOW_SUPPORT_PCT = 0.05   # stop-loss if close falls 5% below support level
MAX_HOLD_WEEKS = 12             # exit if neither TP nor SL hit within this many weeks
TREND_FILTER_SMA_WEEKS = 40     # only buy dips if price above this SMA (0 = disabled)
MAX_POSITIONS = 6
COMMISSION_PCT = 0.0015         # Nordnet 0.15%
COMMISSION_MIN = 39             # SEK minimum per trade

# Output directory. Defaults to ./outputs next to this script; override with the
# OUTPUT_DIR env var. Created automatically so the script runs in any environment.
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------
# 2. DOWNLOAD DATA (need full OHLC, not just Close)
# ---------------------------------------------------------------
# OHLC is pulled straight from Yahoo's public chart API with requests
# (yfinance's crumb/cookie flow gets rate-limited behind a TLS-re-terminating
# egress proxy). O/H/L are adjusted by the adjclose/close ratio so all four
# series are split/dividend-adjusted consistently.
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


print("Downloading data...")
raw = fetch_ohlc(TICKERS, START_DATE, END_DATE)
close = raw["Close"].dropna(axis=1, thresh=int(len(raw["Close"])*0.8))
high = raw["High"][close.columns]
low = raw["Low"][close.columns]
openp = raw["Open"][close.columns]
print(f"Usable tickers: {list(close.columns)}")

w_close = close.resample("W-FRI").last().ffill(limit=2)
w_high = high.resample("W-FRI").max()
w_low = low.resample("W-FRI").min()
w_open = openp.resample("W-FRI").first()

# ---------------------------------------------------------------
# 3. SUPPORT / RESISTANCE LEVELS (shifted to avoid lookahead)
# ---------------------------------------------------------------
support = w_low.rolling(SR_LOOKBACK_WEEKS).min().shift(1)
resistance = w_high.rolling(SR_LOOKBACK_WEEKS).max().shift(1)
trend_sma = w_close.rolling(TREND_FILTER_SMA_WEEKS).mean() if TREND_FILTER_SMA_WEEKS > 0 else None

# ---------------------------------------------------------------
# 4. BACKTEST LOOP
# ---------------------------------------------------------------
cash = INITIAL_CAPITAL
positions = {}   # ticker -> {shares, entry_price, entry_week_idx, support_at_entry}
equity_curve = []
trade_log = []

start_idx = max(SR_LOOKBACK_WEEKS, TREND_FILTER_SMA_WEEKS) + 1
dates = w_close.index[start_idx:]

for week_idx, date in enumerate(dates):
    c = w_close.loc[date]
    h = w_high.loc[date]
    l = w_low.loc[date]
    o = w_open.loc[date]
    sup = support.loc[date]
    res = resistance.loc[date]

    # --- manage open positions: take-profit / stop-loss / max hold ---
    for tkr in list(positions.keys()):
        price = c.get(tkr, np.nan)
        if pd.isna(price):
            continue
        pos = positions[tkr]
        held_weeks = week_idx - pos["entry_week_idx"]
        r = res.get(tkr, np.nan)

        exit_reason = None
        if pd.notna(r) and price >= r * (1 - RESISTANCE_TOUCH_PCT):
            exit_reason = "TAKE_PROFIT"
        elif price <= pos["support_at_entry"] * (1 - STOP_BELOW_SUPPORT_PCT):
            exit_reason = "STOP_LOSS"
        elif held_weeks >= MAX_HOLD_WEEKS:
            exit_reason = "MAX_HOLD"

        if exit_reason:
            shares = pos["shares"]
            proceeds = shares * price
            fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
            cash += proceeds - fee
            trade_log.append((date, tkr, exit_reason, price, shares, fee))
            del positions[tkr]

    # --- look for new entries (bounce off support) ---
    port_value = cash + sum(
        positions[t]["shares"] * c.get(t, positions[t]["entry_price"])
        for t in positions
    )
    slots_open = MAX_POSITIONS - len(positions)

    if slots_open > 0:
        candidates = []
        for tkr in w_close.columns:
            if tkr in positions:
                continue
            s = sup.get(tkr, np.nan)
            low_t = l.get(tkr, np.nan)
            close_t = c.get(tkr, np.nan)
            open_t = o.get(tkr, np.nan)
            if pd.isna(s) or pd.isna(low_t) or pd.isna(close_t) or pd.isna(open_t):
                continue
            near_support = low_t <= s * (1 + SUPPORT_TOUCH_PCT)
            bullish_close = close_t > open_t
            trend_ok = True
            if trend_sma is not None:
                sma_t = trend_sma.loc[date].get(tkr, np.nan)
                trend_ok = pd.notna(sma_t) and close_t > sma_t
            if near_support and bullish_close and trend_ok:
                candidates.append((tkr, (close_t - s) / s))
        candidates.sort(key=lambda x: x[1])  # closest to support first

        alloc_per_position = (port_value / MAX_POSITIONS) if MAX_POSITIONS > 0 else 0
        for tkr, _ in candidates[:slots_open]:
            price = c.get(tkr, np.nan)
            if pd.isna(price) or price <= 0:
                continue
            spend = min(alloc_per_position, cash)
            if spend < 500:
                continue
            shares = spend // price
            if shares <= 0:
                continue
            cost = shares * price
            fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
            if cost + fee > cash:
                continue
            cash -= (cost + fee)
            positions[tkr] = {
                "shares": shares, "entry_price": price,
                "entry_week_idx": week_idx, "support_at_entry": sup.get(tkr, price*0.9)
            }
            trade_log.append((date, tkr, "BUY", price, shares, fee))

    # --- record equity ---
    port_value = cash + sum(
        positions[t]["shares"] * c.get(t, positions[t]["entry_price"])
        for t in positions
    )
    equity_curve.append((date, port_value, cash, len(positions)))

# ---------------------------------------------------------------
# 5. BENCHMARK: equal-weight buy & hold, same universe
# ---------------------------------------------------------------
bh_start_prices = w_close.loc[dates[0]]
bh_alloc = INITIAL_CAPITAL / len(w_close.columns)
bh_shares = {t: bh_alloc / p for t, p in bh_start_prices.items() if pd.notna(p) and p > 0}
bh_equity = []
for date in dates:
    c = w_close.loc[date]
    val = sum(bh_shares[t] * c.get(t, 0) for t in bh_shares if pd.notna(c.get(t, np.nan)))
    bh_equity.append((date, val))
bh_eq = pd.DataFrame(bh_equity, columns=["date","equity"]).set_index("date")

# ---------------------------------------------------------------
# 6. RESULTS
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
n_tp = (trades["action"]=="TAKE_PROFIT").sum()
n_sl = (trades["action"]=="STOP_LOSS").sum()
n_maxhold = (trades["action"]=="MAX_HOLD").sum()
total_fees = trades["fee"].sum()
avg_monthly_sek = (eq["equity"].iloc[-1] - INITIAL_CAPITAL) / (years*12)

bh_cagr = (bh_eq["equity"].iloc[-1] / INITIAL_CAPITAL) ** (1/years) - 1
bh_total_return = bh_eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1

print("\n" + "="*65)
print("BACKTEST RESULTS — Weekly Support/Resistance Swing Strategy (OMXS30)")
print("="*65)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Initial capital: {INITIAL_CAPITAL:,.0f} SEK")
print(f"Final equity: {eq['equity'].iloc[-1]:,.0f} SEK")
print(f"Total return: {total_return*100:.1f}%")
print(f"CAGR: {cagr*100:.1f}%")
print(f"Max drawdown: {max_dd*100:.1f}%")
print(f"Sharpe ratio (weekly, annualized): {sharpe:.2f}")
print(f"Total trades: {n_buys} buys / {n_tp} take-profits / {n_sl} stop-losses / {n_maxhold} max-hold exits")
print(f"Total fees paid: {total_fees:,.0f} SEK  ({total_fees/max(total_return*INITIAL_CAPITAL,1)*100:.0f}% of gross profit)")
print(f"Average implied monthly profit: {avg_monthly_sek:,.0f} SEK  (target was 2,000 SEK)")
print("-"*65)
print(f"BENCHMARK — equal-weight buy & hold, same universe, zero rebalancing:")
print(f"  Total return: {bh_total_return*100:.1f}%   CAGR: {bh_cagr*100:.1f}%")
print("="*65)

# ---------------------------------------------------------------
# 7. PLOT
# ---------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(11,7), sharex=True, gridspec_kw={"height_ratios":[3,1]})
axes[0].plot(eq.index, eq["equity"], color="#7c3aed", linewidth=1.6, label="S/R strategy equity")
axes[0].plot(bh_eq.index, bh_eq["equity"], color="#16a34a", linewidth=1.2, linestyle="-.", label="Buy & hold benchmark")
axes[0].axhline(INITIAL_CAPITAL, color="gray", linestyle="--", linewidth=0.8, label="Initial capital")
axes[0].set_ylabel("Portfolio value (SEK)")
axes[0].set_title("Weekly Support/Resistance Swing Strategy — OMXS30 Backtest")
axes[0].legend()
axes[0].grid(alpha=0.3)

axes[1].fill_between(eq.index, drawdown*100, 0, color="#dc2626", alpha=0.4)
axes[1].set_ylabel("Drawdown (%)")
axes[1].grid(alpha=0.3)

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_sr_results.png"), dpi=150)
print("\nChart saved.")

eq.to_csv(os.path.join(OUTPUT_DIR, "sr_equity_curve.csv"))
trades.to_csv(os.path.join(OUTPUT_DIR, "sr_trade_log.csv"), index=False)
print("CSV files saved.")
