"""
Short Iron Condor with Systematic Untested-Side Rolling — OMXS30 (Simulated)
==============================================================================
IMPORTANT — READ BEFORE TRUSTING RESULTS:
This is a SIMULATION, not a true historical backtest. Real historical OMXS30
options chain data (actual implied vols, actual bid/ask spreads) is not
freely available. Instead this script:
  1. Downloads real OMXS30 INDEX price history
  2. Estimates implied volatility as realized volatility * a vol risk premium
     multiplier (IV is typically above realized vol — this is what option
     sellers are harvesting, but the exact historical premium is unknown
     and simplified here)
  3. Prices all option legs with Black-Scholes (European, matches OMXS30's
     actual settlement style)
  4. Does NOT model bid/ask spreads, assignment risk, or real order-book
     liquidity — which we already flagged as a real concern for OMXS30 options

Treat results as a rough sanity check on the RULES, not a profit forecast.
Verify contract multiplier and real commission structure with your broker
before using this for actual capital decisions.

STRATEGY RULES (fully systematic) — UPDATED:
  - Always trade the "next week" expiry — i.e. open positions with OPEN_TERM_DAYS
    (14 days / ~2 weeks) to expiry, never the imminent/front week
  - COVER the position (close it in the market) once COVER_BUFFER_DAYS (7 days)
    remain — i.e. never hold into the final week before expiry — then
    immediately open a fresh condor in the next available "next week" expiry,
    keeping continuous exposure with a permanent 1-week buffer from expiry
  - Roll trigger: if a short leg's delta exceeds ROLL_DELTA_TRIGGER
    (i.e. that side is being tested), roll the OPPOSITE (untested) side
    in closer to spot to collect fresh premium and rebalance
  - Max ROLLS_MAX adjustments per position; after that, hold until covered
  - EARLY EXIT: if repeated rolling causes the short call and short put
    strikes to converge (the condor degrades into an iron BUTTERFLY —
    both short legs near the same strike, a much riskier ATM-concentrated
    position), close immediately regardless of days-to-expiry, then
    reopen a fresh, properly-spaced condor
"""

import os
import time
from datetime import datetime, timezone

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
INDEX_TICKER = "^OMX"          # OMXS30 index on Yahoo Finance
START_DATE = "2015-01-01"
END_DATE = "2026-06-30"

INITIAL_CAPITAL = 50000        # SEK
CONTRACT_MULTIPLIER = 100      # SEK per index point — VERIFY with Nasdaq/broker, this is an assumption
RISK_FREE_RATE = 0.025         # approximate, adjust to current rate
VOL_RISK_PREMIUM = 1.15        # implied vol assumed 15% above realized — a simplification

OPEN_TERM_DAYS = 14             # always open the "next week" expiry (~2 weeks out), never front week
COVER_BUFFER_DAYS = 7           # cover (close) once this many days remain — permanent 1-week buffer
OTM_PCT = 0.05                  # short strikes ~5% OTM each side
WING_PCT = 0.03                 # long wings a further 3% out (defines max loss)
ROLL_DELTA_TRIGGER = 0.30       # roll untested side when tested short leg's delta exceeds this
ROLLS_MAX = 3                   # cap adjustments per position
BUTTERFLY_CONVERGENCE_PCT = 0.02  # if short call & short put strikes converge within 2% of spot, close early
RISK_PER_TRADE_PCT = 0.05       # risk ~5% of capital per condor (position sizing)
COMMISSION_PER_LEG = 40         # SEK per option leg per transaction — placeholder, verify with broker
REALIZED_VOL_WINDOW = 20        # trading days for realized vol estimate

# --- LIQUIDITY FRICTION (the part the earlier version omitted) ---
# Every time you buy or sell an option leg, you cross the bid/ask spread.
# OMXS30 options are thinly traded vs. major indices, so this is modeled
# as a percentage cost applied to EVERY leg transaction (open, roll, close).
# This is still an estimate — real spreads vary by strike/expiry/moneyness —
# but it's a meaningfully more honest approximation than assuming zero cost.
BID_ASK_SPREAD_PCT = 0.08       # ~8% of option price lost to spread per transaction — thin-liquidity assumption


# ---------------------------------------------------------------
# 2. BLACK-SCHOLES PRICING (European, matches OMXS30 settlement)
# ---------------------------------------------------------------
def bs_price(S, K, T, r, sigma, kind="call"):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S-K) if kind=="call" else (K-S))
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if kind == "call":
        return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
    else:
        return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)

def bs_delta(S, K, T, r, sigma, kind="call"):
    if T <= 0 or sigma <= 0:
        return 1.0 if (kind=="call" and S>K) else (0.0 if kind=="call" else (-1.0 if S<K else 0.0))
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    return norm.cdf(d1) if kind=="call" else norm.cdf(d1) - 1

# ---------------------------------------------------------------
# 3. DOWNLOAD INDEX DATA & ESTIMATE VOL
# ---------------------------------------------------------------
# yfinance's crumb/cookie flow is rate-limited/blocked behind this environment's
# TLS-terminating egress proxy (Connection reset). The plain Yahoo chart endpoint
# returns 200 reliably, so fetch INDEX_TICKER (^OMX — confirmed OMX Stockholm 30
# Index) with requests. This is a data-transport fix only; the ticker and every
# strategy parameter are unchanged.
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
            r = sess.get(
                f"https://{host}/v8/finance/chart/{quote(ticker)}",
                params={"period1": p1, "period2": p2, "interval": "1d",
                        "events": "div,splits"},
                timeout=30,
            )
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


print("Downloading OMXS30 index data...")
idx = fetch_index_closes(INDEX_TICKER, START_DATE, END_DATE)
idx = idx.dropna()
log_ret = np.log(idx / idx.shift(1))
realized_vol = log_ret.rolling(REALIZED_VOL_WINDOW).std() * np.sqrt(252)
iv_proxy = realized_vol * VOL_RISK_PREMIUM

daily = pd.DataFrame({"spot": idx, "iv": iv_proxy}).dropna()

# ---------------------------------------------------------------
# 3b. BID/ASK SPREAD HELPERS
# ---------------------------------------------------------------
HALF_SPREAD = BID_ASK_SPREAD_PCT / 2

def adj_sell(price):
    """Price you actually receive when SELLING an option (hits the bid)."""
    return price * (1 - HALF_SPREAD)

def adj_buy(price):
    """Price you actually pay when BUYING an option (hits the ask)."""
    return price * (1 + HALF_SPREAD)

# ---------------------------------------------------------------
# 4. SIMULATION LOOP
# ---------------------------------------------------------------
cash = INITIAL_CAPITAL
position = None   # dict describing open condor
equity_curve = []
trade_log = []

dates = daily.index

def is_butterfly(call_short_K, put_short_K, spot):
    """True if short call & short put strikes have converged too close together —
    the condor has degraded into a risky ATM-concentrated iron butterfly shape."""
    return abs(call_short_K - put_short_K) / spot < BUTTERFLY_CONVERGENCE_PCT

def open_condor(date, spot, iv, capital):
    T = OPEN_TERM_DAYS / 365
    call_short_K = spot * (1 + OTM_PCT)
    call_long_K  = spot * (1 + OTM_PCT + WING_PCT)
    put_short_K  = spot * (1 - OTM_PCT)
    put_long_K   = spot * (1 - OTM_PCT - WING_PCT)

    cs = bs_price(spot, call_short_K, T, RISK_FREE_RATE, iv, "call")
    cl = bs_price(spot, call_long_K,  T, RISK_FREE_RATE, iv, "call")
    ps = bs_price(spot, put_short_K,  T, RISK_FREE_RATE, iv, "put")
    pl = bs_price(spot, put_long_K,   T, RISK_FREE_RATE, iv, "put")

    # Apply bid/ask spread: you SELL the short legs (receive less) and
    # BUY the long legs (pay more) than the theoretical mid price
    credit_per_point = (adj_sell(cs) - adj_buy(cl)) + (adj_sell(ps) - adj_buy(pl))
    wing_width = call_long_K - call_short_K  # symmetric wings assumed
    max_loss_per_point = wing_width - credit_per_point

    # position size: risk RISK_PER_TRADE_PCT of capital, based on max loss
    risk_budget = capital * RISK_PER_TRADE_PCT
    max_loss_per_contract = max_loss_per_point * CONTRACT_MULTIPLIER
    n_contracts = max(1, int(risk_budget // max(max_loss_per_contract, 1)))

    fee = COMMISSION_PER_LEG * 4 * n_contracts
    credit_received = credit_per_point * CONTRACT_MULTIPLIER * n_contracts - fee

    return {
        "open_date": date, "expiry_days_left": OPEN_TERM_DAYS, "n_contracts": n_contracts,
        "call_short_K": call_short_K, "call_long_K": call_long_K,
        "put_short_K": put_short_K, "put_long_K": put_long_K,
        "wing_width": wing_width, "max_loss_per_point": max_loss_per_point,
        "net_credit_collected": credit_received / n_contracts if n_contracts else 0,  # per contract, running
        "rolls_used": 0, "fee_paid_total": fee,
    }, credit_received

for date in dates:
    spot = daily.loc[date, "spot"]
    iv = daily.loc[date, "iv"]

    if position is None:
        pos, credit = open_condor(date, spot, iv, cash + 0)  # cash-based sizing
        cash += credit
        position = pos
        trade_log.append((date, "OPEN", spot, credit, position["n_contracts"]))
    else:
        position["expiry_days_left"] -= 1
        T = max(position["expiry_days_left"], 0) / 365
        n = position["n_contracts"]

        call_delta = bs_delta(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
        put_delta = bs_delta(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")

        # --- roll logic: if call side tested, roll put side in; and vice versa ---
        if position["rolls_used"] < ROLLS_MAX and T > 1/365:
            if call_delta >= ROLL_DELTA_TRIGGER:
                # close old put spread, open new one closer to spot
                old_ps = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")
                old_pl = bs_price(spot, position["put_long_K"], T, RISK_FREE_RATE, iv, "put")
                close_cost = (adj_buy(old_ps) - adj_sell(old_pl)) * CONTRACT_MULTIPLIER * n
                fee1 = COMMISSION_PER_LEG * 2 * n
                new_put_short_K = spot * (1 - OTM_PCT * 0.6)  # move closer
                new_put_long_K = new_put_short_K - position["wing_width"]
                new_ps = bs_price(spot, new_put_short_K, T, RISK_FREE_RATE, iv, "put")
                new_pl = bs_price(spot, new_put_long_K, T, RISK_FREE_RATE, iv, "put")
                new_credit = (adj_sell(new_ps) - adj_buy(new_pl)) * CONTRACT_MULTIPLIER * n
                fee2 = COMMISSION_PER_LEG * 2 * n
                cash += -close_cost + new_credit - fee1 - fee2
                position["put_short_K"] = new_put_short_K
                position["put_long_K"] = new_put_long_K
                position["rolls_used"] += 1
                position["fee_paid_total"] += fee1 + fee2
                trade_log.append((date, "ROLL_PUT_SIDE", spot, new_credit - close_cost - fee1 - fee2, n))
            elif put_delta <= -ROLL_DELTA_TRIGGER:
                old_cs = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
                old_cl = bs_price(spot, position["call_long_K"], T, RISK_FREE_RATE, iv, "call")
                close_cost = (adj_buy(old_cs) - adj_sell(old_cl)) * CONTRACT_MULTIPLIER * n
                fee1 = COMMISSION_PER_LEG * 2 * n
                new_call_short_K = spot * (1 + OTM_PCT * 0.6)
                new_call_long_K = new_call_short_K + position["wing_width"]
                new_cs = bs_price(spot, new_call_short_K, T, RISK_FREE_RATE, iv, "call")
                new_cl = bs_price(spot, new_call_long_K, T, RISK_FREE_RATE, iv, "call")
                new_credit = (adj_sell(new_cs) - adj_buy(new_cl)) * CONTRACT_MULTIPLIER * n
                fee2 = COMMISSION_PER_LEG * 2 * n
                cash += -close_cost + new_credit - fee1 - fee2
                position["call_short_K"] = new_call_short_K
                position["call_long_K"] = new_call_long_K
                position["rolls_used"] += 1
                position["fee_paid_total"] += fee1 + fee2
                trade_log.append((date, "ROLL_CALL_SIDE", spot, new_credit - close_cost - fee1 - fee2, n))

        # --- check for butterfly convergence (short strikes collapsed together) ---
        butterfly_triggered = is_butterfly(position["call_short_K"], position["put_short_K"], spot)

        # --- check for cover-before-expiry buffer trigger ---
        cover_triggered = position["expiry_days_left"] <= COVER_BUFFER_DAYS

        if butterfly_triggered or cover_triggered:
            cs_v = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
            cl_v = bs_price(spot, position["call_long_K"], T, RISK_FREE_RATE, iv, "call")
            ps_v = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")
            pl_v = bs_price(spot, position["put_long_K"], T, RISK_FREE_RATE, iv, "put")
            # closing in the market: buy back short legs (ask), sell long legs (bid)
            cost_to_close = ((adj_buy(cs_v) - adj_sell(cl_v)) + (adj_buy(ps_v) - adj_sell(pl_v))) * CONTRACT_MULTIPLIER * n
            fee = COMMISSION_PER_LEG * 4 * n
            cash += -cost_to_close - fee
            reason = "BUTTERFLY_CLOSE" if butterfly_triggered else "COVER_CLOSE"
            trade_log.append((date, reason, spot, -cost_to_close - fee, n))
            position = None

            # immediately open a fresh "next week" condor to maintain continuous coverage
            pos, credit = open_condor(date, spot, iv, cash + 0)
            cash += credit
            position = pos
            trade_log.append((date, "OPEN", spot, credit, position["n_contracts"]))

    # --- mark-to-market equity (theoretical mid-price, spread only paid on actual transactions) ---
    if position is not None:
        T = max(position["expiry_days_left"], 0) / 365
        n = position["n_contracts"]
        cs_v = bs_price(spot, position["call_short_K"], T, RISK_FREE_RATE, iv, "call")
        cl_v = bs_price(spot, position["call_long_K"], T, RISK_FREE_RATE, iv, "call")
        ps_v = bs_price(spot, position["put_short_K"], T, RISK_FREE_RATE, iv, "put")
        pl_v = bs_price(spot, position["put_long_K"], T, RISK_FREE_RATE, iv, "put")
        cost_to_close = ((cs_v - cl_v) + (ps_v - pl_v)) * CONTRACT_MULTIPLIER * n
        port_value = cash - cost_to_close
    else:
        port_value = cash

    equity_curve.append((date, port_value, cash))

# ---------------------------------------------------------------
# 5. RESULTS
# ---------------------------------------------------------------
eq = pd.DataFrame(equity_curve, columns=["date","equity","cash"]).set_index("date")
trades = pd.DataFrame(trade_log, columns=["date","action","spot","cashflow","n_contracts"])

total_return = eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1
years = (eq.index[-1] - eq.index[0]).days / 365.25
cagr = (eq["equity"].iloc[-1] / INITIAL_CAPITAL) ** (1/years) - 1
daily_returns = eq["equity"].pct_change().dropna()
sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252) if daily_returns.std() > 0 else np.nan
running_max = eq["equity"].cummax()
drawdown = eq["equity"] / running_max - 1
max_dd = drawdown.min()
avg_monthly_sek = (eq["equity"].iloc[-1] - INITIAL_CAPITAL) / (years*12)

n_opens = (trades["action"]=="OPEN").sum()
n_rolls = trades["action"].isin(["ROLL_PUT_SIDE","ROLL_CALL_SIDE"]).sum()
n_covers = (trades["action"]=="COVER_CLOSE").sum()
n_butterflies = (trades["action"]=="BUTTERFLY_CLOSE").sum()

print("\n" + "="*70)
print("SIMULATED RESULTS — Iron Condor, Next-Week Expiry, 1-Week Buffer (OMXS30)")
print("(Black-Scholes simulation w/ bid-ask spread cost, NOT a true historical backtest)")
print("="*70)
print(f"Period: {eq.index[0].date()} to {eq.index[-1].date()} ({years:.1f} years)")
print(f"Initial capital: {INITIAL_CAPITAL:,.0f} SEK")
print(f"Final equity: {eq['equity'].iloc[-1]:,.0f} SEK")
print(f"Total return: {total_return*100:.1f}%   CAGR: {cagr*100:.1f}%")
print(f"Max drawdown: {max_dd*100:.1f}%")
print(f"Sharpe ratio (annualized): {sharpe:.2f}")
print(f"Condors opened: {n_opens}   Rolls executed: {n_rolls}")
print(f"Covered (1-week buffer): {n_covers}   Butterfly early-exits: {n_butterflies}")
print(f"Average implied monthly profit: {avg_monthly_sek:,.0f} SEK  (target was 2,000 SEK)")
print("="*70)
print("\nASSUMPTIONS USED (verify before trusting numbers):")
print(f"  Contract multiplier: {CONTRACT_MULTIPLIER} SEK/point (VERIFY with Nasdaq/broker)")
print(f"  Implied vol = realized vol x {VOL_RISK_PREMIUM} (simplification, not real market IV)")
print(f"  Commission: {COMMISSION_PER_LEG} SEK/leg (placeholder, verify with broker)")
print(f"  Bid/ask spread: {BID_ASK_SPREAD_PCT*100:.0f}% of option price per transaction")
print(f"  (models OMXS30's thin liquidity — this is still an estimate, not real quoted spreads)")

# ---------------------------------------------------------------
# 6. PLOT
# ---------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(11,7), sharex=True, gridspec_kw={"height_ratios":[3,1]})
axes[0].plot(eq.index, eq["equity"], color="#ea580c", linewidth=1.2, label="Condor strategy equity (simulated)")
axes[0].axhline(INITIAL_CAPITAL, color="gray", linestyle="--", linewidth=0.8, label="Initial capital")
axes[0].set_ylabel("Portfolio value (SEK)")
axes[0].set_title("Short Iron Condor + Untested-Side Rolling — OMXS30 (Black-Scholes Simulation)")
axes[0].legend()
axes[0].grid(alpha=0.3)

axes[1].fill_between(eq.index, drawdown*100, 0, color="#dc2626", alpha=0.4)
axes[1].set_ylabel("Drawdown (%)")
axes[1].grid(alpha=0.3)

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "backtest_iron_condor_results.png"), dpi=150)
print("\nChart saved.")

eq.to_csv(os.path.join(OUTPUT_DIR, "ic_equity_curve.csv"))
trades.to_csv(os.path.join(OUTPUT_DIR, "ic_trade_log.csv"), index=False)
print("CSV files saved.")
