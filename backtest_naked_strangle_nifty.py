"""
Nifty 50 Naked Short Strangle with 2x Untested-Side Roll — matches the Shoonya bot
===================================================================================
This mirrors the user's live Shoonya bot's logic (check_position): it manages TWO
SHORT legs only (no protective wings) and, when one short's premium exceeds 2x the
other, buys back the cheaper (untested/profit) short and re-sells it toward spot at
~80% of the tested leg's premium. It is a NAKED strangle — losses are not capped by
wings, so tail risk is real (and margin, not max-loss, drives sizing).

SIMULATION caveats (same as the condor scripts): real Nifty option chains aren't
free, so premiums are Black-Scholes on a realized-vol IV proxy; bid/ask is a flat %.
The live bot also runs every 5 min intraday — this sim checks once daily, so it
rolls less often than the real bot would.

RULES:
  - Entry every Thursday, next-to-next-week expiry (~14 DTE); sell CE & PE at ~Rs 20.
  - Roll when one short > 2x the other: buy back the cheaper (profit) short, re-sell
    it toward spot at ~80% of the tested short's premium. (interpretation A)
  - Iron-straddle convergence (shorts within 2% of spot) or 7-day buffer -> close &
    reopen on the next Thursday.
  - Sizing by approximate margin (SPAN+exposure proxy), not by capped max loss.

Run:  python backtest_naked_strangle_nifty.py
"""

import os
import math
import time
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
from scipy.stats import norm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

INDEX_TICKER = "^NSEI"
START_DATE = "2015-01-01"
END_DATE = "2026-06-30"

INITIAL_CAPITAL = 500000        # INR
RISK_FREE_RATE = 0.065
VOL_RISK_PREMIUM = 1.15
REALIZED_VOL_WINDOW = 20

OPEN_TERM_DAYS = 14
COVER_BUFFER_DAYS = 7
SHORT_PREMIUM = 20.0            # sell CE & PE at ~Rs 20 (no wings)
ROLL_RATIO = 2.0               # roll when one short > 2x the other
ROLL_TARGET_FRAC = 0.80        # re-short at ~80% of the tested leg's premium
STRADDLE_CONVERGENCE_PCT = 0.02
COMMISSION_PER_ORDER = 100     # Rs flat per order (per leg), NOT per lot
STRIKE_STEP = 50
BID_ASK_SPREAD_PCT = 0.02
THURSDAY = 3

# Sizing: naked strangle needs margin, not max-loss. Approx SPAN+exposure per lot
# as a fraction of notional (spot * lot). Use MARGIN_UTIL of capital for margin.
MARGIN_FRACTION_OF_NOTIONAL = 0.12
MARGIN_UTIL = 0.60


def nifty_lot_size(d):
    d = pd.Timestamp(d)
    if d < pd.Timestamp("2021-07-01"):
        return 75
    if d < pd.Timestamp("2024-04-24"):
        return 50
    if d < pd.Timestamp("2024-11-20"):
        return 25
    if d < pd.Timestamp("2026-01-01"):
        return 75
    return 65


def bs_price(S, K, T, r, sigma, kind="call"):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if kind == "call" else (K - S))
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if kind == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def strike_for_premium(S, T, iv, kind, target):
    if kind == "call":
        K = math.ceil(S / STRIKE_STEP) * STRIKE_STEP
        step = STRIKE_STEP
    else:
        K = math.floor(S / STRIKE_STEP) * STRIKE_STEP
        step = -STRIKE_STEP
    best, berr = K, 1e18
    for _ in range(600):
        p = bs_price(S, K, T, RISK_FREE_RATE, iv, kind)
        if abs(p - target) < berr:
            berr, best = abs(p - target), K
        if p < target:
            break
        K += step
    return best


_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_index_closes(ticker, start, end):
    from urllib.parse import quote
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        host = _YAHOO_HOSTS[attempt % 2]
        try:
            r = sess.get(f"https://{host}/v8/finance/chart/{quote(ticker)}",
                         params={"period1": p1, "period2": p2, "interval": "1d",
                                 "events": "div,splits"}, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                index = pd.to_datetime(res["timestamp"], unit="s").normalize()
                ind = res["indicators"]
                adj = ind.get("adjclose", [{}])[0].get("adjclose") if "adjclose" in ind else None
                close = adj if adj is not None else ind["quote"][0]["close"]
                return pd.Series(close, index=index, dtype=float).dropna()
            time.sleep(0.6 * (attempt + 1))
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"could not download {ticker}")


print("Downloading Nifty 50 (^NSEI) index data...")
idx = fetch_index_closes(INDEX_TICKER, START_DATE, END_DATE)
log_ret = np.log(idx / idx.shift(1))
iv_proxy = log_ret.rolling(REALIZED_VOL_WINDOW).std() * np.sqrt(252) * VOL_RISK_PREMIUM
daily = pd.DataFrame({"spot": idx, "iv": iv_proxy}).dropna()

HALF_SPREAD = BID_ASK_SPREAD_PCT / 2
adj_sell = lambda p: p * (1 - HALF_SPREAD)
adj_buy = lambda p: p * (1 + HALF_SPREAD)

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

cash = INITIAL_CAPITAL
position = None
equity_curve = []
trade_log = []


def open_strangle(date, spot, iv, capital):
    T = OPEN_TERM_DAYS / 365
    lot = nifty_lot_size(date)
    csK = strike_for_premium(spot, T, iv, "call", SHORT_PREMIUM)
    psK = strike_for_premium(spot, T, iv, "put", SHORT_PREMIUM)
    cs = bs_price(spot, csK, T, RISK_FREE_RATE, iv, "call")
    ps = bs_price(spot, psK, T, RISK_FREE_RATE, iv, "put")
    credit_pts = adj_sell(cs) + adj_sell(ps)           # sell both, no wings

    margin_per_lot = spot * lot * MARGIN_FRACTION_OF_NOTIONAL
    n_lots = max(1, int((capital * MARGIN_UTIL) // max(margin_per_lot, 1)))

    fee = COMMISSION_PER_ORDER * 2                      # 2 legs, flat per order
    credit = credit_pts * lot * n_lots - fee
    pos = {"open_date": date, "expiry": date + timedelta(days=OPEN_TERM_DAYS),
           "n_lots": n_lots, "lot": lot, "call_short_K": csK, "put_short_K": psK,
           "rolls": 0}
    return pos, credit


for date in daily.index:
    spot = daily.loc[date, "spot"]
    iv = daily.loc[date, "iv"]

    if position is not None:
        days_left = (position["expiry"] - date).days
        T = max(days_left, 0) / 365
        n, mult = position["n_lots"], position["lot"]
        cs = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
        ps = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")

        # ROLL: one short > 2x the other -> buy back cheaper (profit) short, re-short
        #       toward spot at ~80% of the tested short's premium
        if days_left > 1:
            tested_is_call = cs >= ps
            tested = cs if tested_is_call else ps
            untested = ps if tested_is_call else cs
            if untested > 0 and tested > ROLL_RATIO * untested:
                target = ROLL_TARGET_FRAC * tested
                if tested_is_call:      # put is profit -> roll put up toward spot
                    newK = strike_for_premium(spot, T, iv, "put", target)
                    if newK < position["call_short_K"]:
                        new_ps = bs_price(spot, newK, T, RISK_FREE_RATE, iv, "put")
                        cash += (-adj_buy(ps) + adj_sell(new_ps)) * mult * n - COMMISSION_PER_ORDER * 2
                        position["put_short_K"] = newK
                        position["rolls"] += 1
                        trade_log.append((date, "ROLL_PUT", spot, 0, n))
                else:                   # call is profit -> roll call down toward spot
                    newK = strike_for_premium(spot, T, iv, "call", target)
                    if newK > position["put_short_K"]:
                        new_cs = bs_price(spot, newK, T, RISK_FREE_RATE, iv, "call")
                        cash += (-adj_buy(cs) + adj_sell(new_cs)) * mult * n - COMMISSION_PER_ORDER * 2
                        position["call_short_K"] = newK
                        position["rolls"] += 1
                        trade_log.append((date, "ROLL_CALL", spot, 0, n))

        straddle = abs(position["call_short_K"] - position["put_short_K"]) / spot < STRADDLE_CONVERGENCE_PCT
        cover = days_left <= COVER_BUFFER_DAYS
        if straddle or cover:
            cs = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
            ps = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")
            cost_to_close = (adj_buy(cs) + adj_buy(ps)) * mult * n     # buy back both shorts
            cash += -cost_to_close - COMMISSION_PER_ORDER * 2
            trade_log.append((date, "STRADDLE_CLOSE" if straddle else "COVER_CLOSE",
                              spot, -cost_to_close, n))
            position = None

    if position is None and date.weekday() == THURSDAY:
        position, credit = open_strangle(date, spot, iv, cash)
        cash += credit
        trade_log.append((date, "OPEN", spot, credit, position["n_lots"]))

    if position is not None:
        days_left = (position["expiry"] - date).days
        T = max(days_left, 0) / 365
        n, mult = position["n_lots"], position["lot"]
        cs = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
        ps = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")
        port_value = cash - (cs + ps) * mult * n
    else:
        port_value = cash
    equity_curve.append((date, port_value, cash))

eq = pd.DataFrame(equity_curve, columns=["date", "equity", "cash"]).set_index("date")
trades = pd.DataFrame(trade_log, columns=["date", "action", "spot", "cashflow", "n_lots"])

total_return = eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1
years = (eq.index[-1] - eq.index[0]).days / 365.25
final_eq = eq["equity"].iloc[-1]
cagr = (final_eq / INITIAL_CAPITAL) ** (1 / years) - 1 if final_eq > 0 else float("nan")
dr = eq["equity"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
sharpe = (dr.mean() / dr.std()) * np.sqrt(252) if dr.std() > 0 else float("nan")
max_dd = (eq["equity"] / eq["equity"].cummax() - 1).min()
avg_monthly = (final_eq - INITIAL_CAPITAL) / (years * 12)
worst_day = (eq["equity"].diff()).min()

n_opens = (trades["action"] == "OPEN").sum()
n_rolls = trades["action"].isin(["ROLL_PUT", "ROLL_CALL"]).sum()
n_covers = (trades["action"] == "COVER_CLOSE").sum()
n_str = (trades["action"] == "STRADDLE_CLOSE").sum()

print("\n" + "=" * 72)
print("SIMULATED — Nifty 50 NAKED Short Strangle + 2x Untested-Side Roll (the bot)")
print("(Black-Scholes sim, NOT a true backtest; NO wings => uncapped tail risk)")
print("=" * 72)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Initial capital: Rs {INITIAL_CAPITAL:,.0f}")
print(f"Final equity: Rs {final_eq:,.0f}")
print(f"Total return: {total_return*100:.1f}%   CAGR: {cagr*100:.1f}%")
print(f"Max drawdown: {max_dd*100:.1f}%   Worst single day: Rs {worst_day:,.0f}")
print(f"Sharpe ratio (annualized): {sharpe:.2f}")
print(f"Strangles opened: {n_opens}   Rolls executed: {n_rolls}")
print(f"Covered (7-day buffer): {n_covers}   Straddle-converge closes: {n_str}")
print(f"Average implied monthly profit: Rs {avg_monthly:,.0f}")
print("=" * 72)
print("\nASSUMPTIONS USED (verify before trusting numbers):")
print(f"  Underlying: {INDEX_TICKER} (real); premiums BS-modelled; IV = realized x {VOL_RISK_PREMIUM}")
print(f"  NAKED shorts sold at ~Rs {SHORT_PREMIUM:.0f} (no protective wings)")
print(f"  Roll when one short > {ROLL_RATIO:.0f}x other; re-short at ~{ROLL_TARGET_FRAC*100:.0f}% of tested premium")
print(f"  Lot size: period-accurate; Brokerage: Rs {COMMISSION_PER_ORDER}/order (flat, not per lot)")
print(f"  Bid/ask: {BID_ASK_SPREAD_PCT*100:.0f}%/transaction; margin/lot ~ {MARGIN_FRACTION_OF_NOTIONAL:.0%} of notional")
print(f"  Daily check (live bot runs every 5 min); STT/GST/exchange NOT added")

fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
axes[0].plot(eq.index, eq["equity"], color="#b91c1c", linewidth=1.2, label="Naked strangle equity (sim)")
axes[0].axhline(INITIAL_CAPITAL, color="gray", linestyle="--", linewidth=0.8, label="Initial capital")
axes[0].set_ylabel("Portfolio value (Rs)")
axes[0].set_title("Nifty 50 Naked Short Strangle + 2x Roll — Black-Scholes Simulation")
axes[0].legend(); axes[0].grid(alpha=0.3)
axes[1].fill_between(eq.index, (eq["equity"] / eq["equity"].cummax() - 1) * 100, 0, color="#dc2626", alpha=0.4)
axes[1].set_ylabel("Drawdown (%)"); axes[1].grid(alpha=0.3)
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_naked_strangle_nifty_results.png"), dpi=150)
print("\nChart saved.")
eq.to_csv(os.path.join(OUTPUT_DIR, "naked_strangle_nifty_equity.csv"))
trades.to_csv(os.path.join(OUTPUT_DIR, "naked_strangle_nifty_trades.csv"), index=False)
print("CSV files saved.")
