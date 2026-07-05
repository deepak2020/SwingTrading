"""
Nifty 50 Weekly Iron Condor -> Iron Fly, Premium-Selected & Ratio-Rolled (Simulated)
=====================================================================================
IMPORTANT — READ BEFORE TRUSTING RESULTS:
This is a SIMULATION, not a true historical backtest. Free historical Nifty
options chains (real IV, real bid/ask) do not exist, so this script:
  1. Downloads the real Nifty 50 INDEX (^NSEI) price history from Yahoo.
  2. Estimates implied vol as realized vol * a vol-risk-premium multiplier.
  3. Prices every option leg with Black-Scholes (European).
  4. Models bid/ask as a flat % of option price (Nifty weeklies are very liquid,
     so this is set LOW vs. the thin OMXS30 case — still an estimate).
Treat it as a sanity check on the RULES, not a profit forecast. Verify lot size,
real charges (STT/GST/exchange), and IV before risking capital.

STRATEGY RULES (as specified):
  - ENTRY: every THURSDAY, open the "next-to-next week" expiry (~14 days out),
    never the front week. Select strikes BY PREMIUM: short call & short put at
    ~SHORT_PREMIUM (Rs 20) each; long "cover" wings at ~COVER_PREMIUM (Rs 10) each.
  - COVER: close the whole position once <= COVER_BUFFER_DAYS (7) remain, then
    reopen a fresh condor on the next Thursday.
  - ROLL: when one short leg's premium is more than DOUBLE the other short leg's
    (ROLL_RATIO = 2.0), "cover the profit side" — buy back the cheaper/untested
    (in-profit) short — and re-sell a new short on that side at the strike whose
    premium ~= ROLL_TARGET_FRAC (0.80) of the tested (expensive) short's premium,
    moving it toward spot. The long COVER wings are NEVER moved.
  - IRON FLY: keep rolling as the market moves until the two shorts converge
    within BUTTERFLY_CONVERGENCE_PCT (2%) of spot -> close everything immediately
    and reopen a fresh, properly-spaced condor (on the next Thursday).

Run:  python backtest_iron_condor_nifty.py
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

# ---------------------------------------------------------------
# 1. CONFIG — ASSUMPTIONS, VERIFY BEFORE REAL USE
# ---------------------------------------------------------------
INDEX_TICKER = "^NSEI"          # Nifty 50 index on Yahoo Finance
START_DATE = "2015-01-01"
END_DATE = "2026-06-30"

INITIAL_CAPITAL = 500000        # INR
RISK_FREE_RATE = 0.065          # ~India repo/T-bill area, approximate
VOL_RISK_PREMIUM = 1.15         # implied vol assumed 15% above realized — a simplification
REALIZED_VOL_WINDOW = 20        # trading days for realized vol

OPEN_TERM_DAYS = 14             # open ~2 weeks out (next-to-next Thursday), never front week
COVER_BUFFER_DAYS = 7           # cover once this many days remain, then reopen next Thursday
SHORT_PREMIUM = 20.0            # sell short legs at ~Rs 20 premium
COVER_PREMIUM = 10.0            # buy long cover wings at ~Rs 10 premium
ROLL_RATIO = 2.0                # roll when one short leg premium > 2x the other short leg
ROLL_TARGET_FRAC = 0.80         # re-short the covered side at ~80% of the tested short's premium
BUTTERFLY_CONVERGENCE_PCT = 0.02  # shorts within 2% of spot => iron fly => close & reopen
RISK_PER_TRADE_PCT = 0.05       # risk ~5% of capital per condor (sizing)
COMMISSION_PER_LEG = 100        # Rs per leg per transaction (all-in placeholder; verify)
STRIKE_STEP = 50                # Nifty option strike interval (points)
BID_ASK_SPREAD_PCT = 0.02       # ~2% of option price per transaction (liquid Nifty weeklies)
THURSDAY = 3                    # weekday() for Thursday (Nifty weekly expiry day)


def nifty_lot_size(d):
    """Period-accurate Nifty 50 F&O lot size (sourced from 2021 on; carried back
    to the 2015 start as an approximation — risk-based sizing self-normalizes)."""
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


# ---------------------------------------------------------------
# 2. BLACK-SCHOLES PRICING (European)
# ---------------------------------------------------------------
def bs_price(S, K, T, r, sigma, kind="call"):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if kind == "call" else (K - S))
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if kind == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def strike_for_premium(S, T, iv, kind, target):
    """Grid strike (multiple of STRIKE_STEP) whose BS premium is closest to target.
    Premium falls monotonically as the strike moves further OTM."""
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
        if p < target:          # already past the target moving OTM
            break
        K += step
    return best


# ---------------------------------------------------------------
# 3. DOWNLOAD INDEX DATA & ESTIMATE VOL
# ---------------------------------------------------------------
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
    raise RuntimeError(f"could not download {ticker} from Yahoo")


print("Downloading Nifty 50 (^NSEI) index data...")
idx = fetch_index_closes(INDEX_TICKER, START_DATE, END_DATE)
log_ret = np.log(idx / idx.shift(1))
realized_vol = log_ret.rolling(REALIZED_VOL_WINDOW).std() * np.sqrt(252)
iv_proxy = (realized_vol * VOL_RISK_PREMIUM)
daily = pd.DataFrame({"spot": idx, "iv": iv_proxy}).dropna()

HALF_SPREAD = BID_ASK_SPREAD_PCT / 2
adj_sell = lambda p: p * (1 - HALF_SPREAD)   # receive on a SELL (hit bid)
adj_buy = lambda p: p * (1 + HALF_SPREAD)    # pay on a BUY (hit ask)

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------
# 4. SIMULATION LOOP
# ---------------------------------------------------------------
cash = INITIAL_CAPITAL
position = None
equity_curve = []
trade_log = []


def open_condor(date, spot, iv, capital):
    T = OPEN_TERM_DAYS / 365
    lot = nifty_lot_size(date)
    csK = strike_for_premium(spot, T, iv, "call", SHORT_PREMIUM)
    clK = strike_for_premium(spot, T, iv, "call", COVER_PREMIUM)
    psK = strike_for_premium(spot, T, iv, "put", SHORT_PREMIUM)
    plK = strike_for_premium(spot, T, iv, "put", COVER_PREMIUM)
    if clK <= csK:
        clK = csK + STRIKE_STEP
    if plK >= psK:
        plK = psK - STRIKE_STEP

    cs = bs_price(spot, csK, T, RISK_FREE_RATE, iv, "call")
    cl = bs_price(spot, clK, T, RISK_FREE_RATE, iv, "call")
    ps = bs_price(spot, psK, T, RISK_FREE_RATE, iv, "put")
    pl = bs_price(spot, plK, T, RISK_FREE_RATE, iv, "put")
    credit_pts = (adj_sell(cs) - adj_buy(cl)) + (adj_sell(ps) - adj_buy(pl))

    call_wing = clK - csK
    put_wing = psK - plK
    max_loss_pts = max(max(call_wing, put_wing) - credit_pts, 1.0)
    risk_budget = capital * RISK_PER_TRADE_PCT
    n_lots = max(1, int(risk_budget // max(max_loss_pts * lot, 1)))

    fee = COMMISSION_PER_LEG * 4 * n_lots
    credit = credit_pts * lot * n_lots - fee
    pos = {
        "open_date": date, "expiry": date + timedelta(days=OPEN_TERM_DAYS),
        "n_lots": n_lots, "lot": lot,
        "call_short_K": csK, "call_long_K": clK,
        "put_short_K": psK, "put_long_K": plK,
        "rolls": 0, "fee_total": fee,
    }
    return pos, credit


def legs_value(pos, spot, T, iv):
    cs = bs_price(spot, pos["call_short_K"], T, RISK_FREE_RATE, iv, "call")
    cl = bs_price(spot, pos["call_long_K"], T, RISK_FREE_RATE, iv, "call")
    ps = bs_price(spot, pos["put_short_K"], T, RISK_FREE_RATE, iv, "put")
    pl = bs_price(spot, pos["put_long_K"], T, RISK_FREE_RATE, iv, "put")
    return cs, cl, ps, pl


for date in daily.index:
    spot = daily.loc[date, "spot"]
    iv = daily.loc[date, "iv"]

    if position is not None:
        days_left = (position["expiry"] - date).days
        T = max(days_left, 0) / 365
        n, mult = position["n_lots"], position["lot"]
        cs, cl, ps, pl = legs_value(position, spot, T, iv)

        # --- ROLL: one short > 2x the other -> cover the profit (untested) side,
        #     re-short at ~80% of the tested short's premium, toward spot ---
        if days_left > 1:
            tested_is_call = cs >= ps
            tested = cs if tested_is_call else ps
            untested = ps if tested_is_call else cs
            if untested > 0 and tested > ROLL_RATIO * untested:
                target = ROLL_TARGET_FRAC * tested
                if tested_is_call:   # put is the profit side -> roll it up toward spot
                    newK = strike_for_premium(spot, T, iv, "put", target)
                    if newK < position["call_short_K"]:
                        new_ps = bs_price(spot, newK, T, RISK_FREE_RATE, iv, "put")
                        cash += (-adj_buy(ps) + adj_sell(new_ps)) * mult * n
                        fee = COMMISSION_PER_LEG * 2 * n
                        cash -= fee
                        position["put_short_K"] = newK
                        position["rolls"] += 1
                        position["fee_total"] += fee
                        trade_log.append((date, "ROLL_PUT", spot,
                                          (-adj_buy(ps) + adj_sell(new_ps)) * mult * n - fee, n))
                else:                # call is the profit side -> roll it down toward spot
                    newK = strike_for_premium(spot, T, iv, "call", target)
                    if newK > position["put_short_K"]:
                        new_cs = bs_price(spot, newK, T, RISK_FREE_RATE, iv, "call")
                        cash += (-adj_buy(cs) + adj_sell(new_cs)) * mult * n
                        fee = COMMISSION_PER_LEG * 2 * n
                        cash -= fee
                        position["call_short_K"] = newK
                        position["rolls"] += 1
                        position["fee_total"] += fee
                        trade_log.append((date, "ROLL_CALL", spot,
                                          (-adj_buy(cs) + adj_sell(new_cs)) * mult * n - fee, n))

        # --- IRON FLY (shorts converged) or COVER (buffer) -> close everything ---
        fly = abs(position["call_short_K"] - position["put_short_K"]) / spot < BUTTERFLY_CONVERGENCE_PCT
        cover = days_left <= COVER_BUFFER_DAYS
        if fly or cover:
            cs, cl, ps, pl = legs_value(position, spot, T, iv)
            cost_to_close = ((adj_buy(cs) - adj_sell(cl)) + (adj_buy(ps) - adj_sell(pl))) * mult * n
            fee = COMMISSION_PER_LEG * 4 * n
            cash += -cost_to_close - fee
            trade_log.append((date, "FLY_CLOSE" if fly else "COVER_CLOSE", spot,
                              -cost_to_close - fee, n))
            position = None

    # --- ENTRY: only when flat AND it's Thursday (front-week avoided by 14d term) ---
    if position is None and date.weekday() == THURSDAY:
        position, credit = open_condor(date, spot, iv, cash)
        cash += credit
        trade_log.append((date, "OPEN", spot, credit, position["n_lots"]))

    # --- mark to market (mid price, no spread) ---
    if position is not None:
        days_left = (position["expiry"] - date).days
        T = max(days_left, 0) / 365
        n, mult = position["n_lots"], position["lot"]
        cs, cl, ps, pl = legs_value(position, spot, T, iv)
        cost_to_close = ((cs - cl) + (ps - pl)) * mult * n
        port_value = cash - cost_to_close
    else:
        port_value = cash
    equity_curve.append((date, port_value, cash))

# ---------------------------------------------------------------
# 5. RESULTS
# ---------------------------------------------------------------
eq = pd.DataFrame(equity_curve, columns=["date", "equity", "cash"]).set_index("date")
trades = pd.DataFrame(trade_log, columns=["date", "action", "spot", "cashflow", "n_lots"])

total_return = eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1
years = (eq.index[-1] - eq.index[0]).days / 365.25
final_eq = eq["equity"].iloc[-1]
cagr = (final_eq / INITIAL_CAPITAL) ** (1 / years) - 1 if final_eq > 0 else float("nan")
daily_returns = eq["equity"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252) if daily_returns.std() > 0 else float("nan")
running_max = eq["equity"].cummax()
drawdown = eq["equity"] / running_max - 1
max_dd = drawdown.min()
avg_monthly = (final_eq - INITIAL_CAPITAL) / (years * 12)

n_opens = (trades["action"] == "OPEN").sum()
n_rolls = trades["action"].isin(["ROLL_PUT", "ROLL_CALL"]).sum()
n_covers = (trades["action"] == "COVER_CLOSE").sum()
n_flies = (trades["action"] == "FLY_CLOSE").sum()

print("\n" + "=" * 72)
print("SIMULATED — Nifty 50 Weekly Iron Condor -> Iron Fly (premium-selected, 2x roll)")
print("(Black-Scholes simulation w/ bid-ask cost, NOT a true historical backtest)")
print("=" * 72)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Initial capital: Rs {INITIAL_CAPITAL:,.0f}")
print(f"Final equity: Rs {final_eq:,.0f}")
print(f"Total return: {total_return*100:.1f}%   CAGR: {cagr*100:.1f}%")
print(f"Max drawdown: {max_dd*100:.1f}%")
print(f"Sharpe ratio (annualized): {sharpe:.2f}")
print(f"Condors opened: {n_opens}   Rolls executed: {n_rolls}")
print(f"Covered (7-day buffer): {n_covers}   Iron-fly early-exits: {n_flies}")
print(f"Average implied monthly profit: Rs {avg_monthly:,.0f}")
print("=" * 72)
print("\nASSUMPTIONS USED (verify before trusting numbers):")
print(f"  Underlying: {INDEX_TICKER} index (real prices); option premiums BS-modelled")
print(f"  Implied vol = realized vol x {VOL_RISK_PREMIUM} (simplification, not real market IV)")
print(f"  Short legs sold at ~Rs {SHORT_PREMIUM:.0f}, cover wings bought at ~Rs {COVER_PREMIUM:.0f}")
print(f"  Roll when one short > {ROLL_RATIO:.0f}x the other; re-short at ~{ROLL_TARGET_FRAC*100:.0f}% of tested premium")
print(f"  Lot size: period-accurate (75 pre-2021, 50, 25, 75, 65 from 2026)")
print(f"  Brokerage: Rs {COMMISSION_PER_LEG}/leg (all-in placeholder; STT/GST/exchange NOT added)")
print(f"  Bid/ask spread: {BID_ASK_SPREAD_PCT*100:.0f}% of option price per transaction (liquid Nifty)")
print(f"  Risk-free: {RISK_FREE_RATE*100:.1f}%   Sizing: {RISK_PER_TRADE_PCT*100:.0f}% capital risk/trade")

# ---------------------------------------------------------------
# 6. PLOT
# ---------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
axes[0].plot(eq.index, eq["equity"], color="#0891b2", linewidth=1.2, label="Condor->Fly equity (simulated)")
axes[0].axhline(INITIAL_CAPITAL, color="gray", linestyle="--", linewidth=0.8, label="Initial capital")
axes[0].set_ylabel("Portfolio value (Rs)")
axes[0].set_title("Nifty 50 Weekly Iron Condor -> Iron Fly — Black-Scholes Simulation")
axes[0].legend(); axes[0].grid(alpha=0.3)
axes[1].fill_between(eq.index, drawdown * 100, 0, color="#dc2626", alpha=0.4)
axes[1].set_ylabel("Drawdown (%)"); axes[1].grid(alpha=0.3)
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_iron_condor_nifty_results.png"), dpi=150)
print("\nChart saved.")

eq.to_csv(os.path.join(OUTPUT_DIR, "ic_nifty_equity_curve.csv"))
trades.to_csv(os.path.join(OUTPUT_DIR, "ic_nifty_trade_log.csv"), index=False)
print("CSV files saved.")
