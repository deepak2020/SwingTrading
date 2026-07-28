"""
Two Swedish equity swing strategies (4-6 week holds), honest backtest
=====================================================================
STRATEGY A — shortened momentum rotation (rebalance every 3 weeks / 15 td):
  rank by 3-month return skipping the last 2 weeks; hold top 6 equal-weight,
  positive-momentum only; 100% cash when ^OMX < its 200-day MA (regime filter);
  hard-exit any position at 6 weeks (30 td).
STRATEGY B — post-earnings-announcement-drift PROXY:
  no free earnings dates, so the signal is a gap+volume signature:
  daily return > +5% on volume >= 2x the 20-day average. Enter at close of
  day +2; hold 4-6 weeks (both tested); early-exit only on a close below the
  pre-signal close; cap 8 concurrent, equal weight, cash otherwise.

COSTS: applied per side = courtage 6 bps + slippage (16 bps base; also 10, 30).

DATA NOTE: yfinance is blocked behind this environment's egress proxy, so data
is pulled from Yahoo's public chart API with requests (same auto-adjusted closes
+ volume). If the download fails, the script stops rather than inventing numbers.

SURVIVORSHIP BIAS: the universe below is TODAY's listed names. Companies that
were delisted, acquired, or collapsed between 2010 and now are absent, which
inflates every result here — see the printed estimate at the end.
"""

import os
import time
from datetime import datetime, timezone, date

import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

START = "2010-01-01"
END = date.today().isoformat()
RF = 0.02
COURTAGE_BPS = 6
SLIP_BASE = 16
SLIP_GRID = [10, 16, 30]
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ~45 liquid Swedish large/mid caps (today's names — survivorship-biased set)
UNIVERSE = [
    "ABB.ST","ADDT-B.ST","ALFA.ST","ASSA-B.ST","ATCO-A.ST","ATCO-B.ST","AZN.ST",
    "BALD-B.ST","BOL.ST","CAST.ST","ELUX-B.ST","EPI-A.ST","ERIC-B.ST","ESSITY-B.ST",
    "EQT.ST","EVO.ST","FABG.ST","GETI-B.ST","HEXA-B.ST","HM-B.ST","HOLM-B.ST",
    "HPOL-B.ST","HUSQ-B.ST","INDU-C.ST","INVE-B.ST","KINV-B.ST","LATO-B.ST",
    "LIFCO-B.ST","LUND-B.ST","NDA-SE.ST","NIBE-B.ST","SAAB-B.ST","SAND.ST","SCA-B.ST",
    "SEB-A.ST","SECU-B.ST","SHB-A.ST","SINCH.ST","SKA-B.ST","SKF-B.ST","SSAB-A.ST",
    "SWED-A.ST","TEL2-B.ST","TELIA.ST","TREL-B.ST","VOLV-B.ST","WALL-B.ST",
]
BENCH = "^OMX"
_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch(ticker, start, end):
    from urllib.parse import quote
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"})
    for a in range(4):
        try:
            r = s.get(f"https://{_HOSTS[a % 2]}/v8/finance/chart/{quote(ticker)}",
                      params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"}, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                q = res["indicators"]["quote"][0]
                ind = res["indicators"]
                adj = ind.get("adjclose", [{}])[0].get("adjclose") if "adjclose" in ind else None
                close = adj if adj is not None else q["close"]
                df = pd.DataFrame({"px": close, "vol": q["volume"]}, index=idx).dropna(subset=["px"])
                return df
            time.sleep(0.5 * (a + 1))
        except Exception:
            time.sleep(0.5 * (a + 1))
    return None


print(f"Downloading {len(UNIVERSE)} names + {BENCH} from Yahoo chart API ({START}..{END})...")
data = {}
for t in UNIVERSE:
    d = fetch(t, START, END)
    if d is not None and len(d) > 250:
        data[t] = d
    time.sleep(0.08)
bench = fetch(BENCH, START, END)

if len(data) < 20 or bench is None:
    raise SystemExit(f"DATA DOWNLOAD FAILED (got {len(data)} names, bench={'ok' if bench is not None else 'FAIL'}). "
                     "Stopping rather than producing illustrative numbers.")

PX = pd.DataFrame({t: d["px"] for t, d in data.items()}).sort_index()
VOL = pd.DataFrame({t: d["vol"] for t, d in data.items()}).sort_index()
PX = PX[~PX.index.duplicated()]; VOL = VOL[~VOL.index.duplicated()]
OMX = bench["px"].reindex(PX.index).ffill()
OMX200 = OMX.rolling(200).mean()
dates = PX.index
print(f"Fetched {len(data)}/{len(UNIVERSE)} names. Rows: {len(dates)} ({dates[0].date()}..{dates[-1].date()})")


def cost_ps(slip_bps):
    return (COURTAGE_BPS + slip_bps) / 10000.0


# ---------------- metrics ----------------
def metrics(eq, trades, total_cost, mean_eq):
    eq = eq.dropna()
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(252)
    sharpe = (cagr - RF) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    tr = pd.DataFrame(trades)
    win = (tr["ret"] > 0).mean() if len(tr) else np.nan
    hold = tr["hold_days"].mean() if len(tr) else np.nan
    buy_notional = tr["entry_val"].sum() if len(tr) else 0.0
    turnover = buy_notional / mean_eq / years if mean_eq and years else np.nan   # one-way annual
    cost_drag = total_cost / mean_eq / years if mean_eq and years else np.nan
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, maxdd=dd, win=win, hold=hold,
                turnover=turnover, cost_drag=cost_drag, mult=eq.iloc[-1] / eq.iloc[0],
                n_trades=len(tr))


# ---------------- Strategy A ----------------
def run_A(lookback_td, top_n, slip_bps, regime=True, skip_td=10, hold_max_td=30, rebal_td=15):
    c = cost_ps(slip_bps)
    warm = lookback_td + 5
    cash = 1.0; pos = {}; eqc = []; trades = []; total_cost = 0.0; eqsum = 0.0; eqn = 0
    reb_days = set(range(warm, len(dates), rebal_td))
    px = PX.values; cols = list(PX.columns); colidx = {t: i for i, t in enumerate(cols)}
    for i, d in enumerate(dates):
        row = px[i]
        if i in reb_days:
            cooldown = set()
            # force-exit at 6 weeks
            for t in list(pos):
                if i - pos[t]["ei"] >= hold_max_td:
                    p = row[colidx[t]]
                    if not np.isnan(p):
                        val = pos[t]["sh"] * p; cost = val * c; cash += val - cost; total_cost += cost
                        trades.append(_tr("A", t, pos[t], p, dates[i], "6wk-exit")); del pos[t]; cooldown.add(t)
            # regime
            in_mkt = True
            if regime and (np.isnan(OMX200.iloc[i]) or OMX.iloc[i] < OMX200.iloc[i]):
                in_mkt = False
            target = []
            if in_mkt:
                mom = {}
                for t in cols:
                    p_now = px[i - skip_td][colidx[t]] if i - skip_td >= 0 else np.nan
                    p_ref = px[i - lookback_td][colidx[t]] if i - lookback_td >= 0 else np.nan
                    if not np.isnan(p_now) and not np.isnan(p_ref) and p_ref > 0 and not np.isnan(row[colidx[t]]):
                        m = p_now / p_ref - 1
                        if m > 0 and t not in cooldown:
                            mom[t] = m
                target = [t for t, _ in sorted(mom.items(), key=lambda x: x[1], reverse=True)[:top_n]]
            # sell those not in target
            for t in list(pos):
                if t not in target:
                    p = row[colidx[t]]
                    if not np.isnan(p):
                        val = pos[t]["sh"] * p; cost = val * c; cash += val - cost; total_cost += cost
                        trades.append(_tr("A", t, pos[t], p, dates[i], "rotate")); del pos[t]
            # buy new to equal weight = equity/top_n
            equity = cash + sum(pos[t]["sh"] * row[colidx[t]] for t in pos if not np.isnan(row[colidx[t]]))
            slot = equity / top_n
            for t in target:
                if t not in pos and cash > slot * 0.5:
                    p = row[colidx[t]]
                    if np.isnan(p) or p <= 0: continue
                    val = min(slot, cash / (1 + c)); sh = val / p
                    cash -= val * (1 + c); total_cost += val * c
                    pos[t] = {"sh": sh, "ep": p, "ei": i, "ed": dates[i]}
        equity = cash + sum(pos[t]["sh"] * row[colidx[t]] for t in pos if not np.isnan(row[colidx[t]]))
        eqc.append(equity); eqsum += equity; eqn += 1
    eq = pd.Series(eqc, index=dates)
    return eq, trades, total_cost, eqsum / eqn


# ---------------- Strategy B ----------------
def run_B(hold_td, slip_bps, maxpos=8):
    c = cost_ps(slip_bps)
    ret = PX.pct_change()
    vol20 = VOL.rolling(20).mean()
    sig = (ret > 0.05) & (VOL >= 2 * vol20)
    cash = 1.0; pos = {}; eqc = []; trades = []; total_cost = 0.0; eqsum = 0.0; eqn = 0
    px = PX.values; cols = list(PX.columns); colidx = {t: i for i, t in enumerate(cols)}
    sigv = sig.reindex(columns=cols).values
    for i, d in enumerate(dates):
        row = px[i]
        # exits
        for t in list(pos):
            p = row[colidx[t]]
            if np.isnan(p): continue
            if i >= pos[t]["xi"] or p < pos[t]["presig"]:
                val = pos[t]["sh"] * p; cost = val * c; cash += val - cost; total_cost += cost
                trades.append(_tr("B", t, pos[t], p, dates[i], "time" if i >= pos[t]["xi"] else "stop")); del pos[t]
        # entries: signals from day i-2, entered at today's close
        if i - 2 >= 0 and i - 3 >= 0:
            fired = [t for t in cols if sigv[i - 2][colidx[t]] and t not in pos]
            fired.sort(key=lambda t: (px[i - 2][colidx[t]] / px[i - 3][colidx[t]] - 1)
                       if not np.isnan(px[i - 3][colidx[t]]) and px[i - 3][colidx[t]] > 0 else -9, reverse=True)
            equity = cash + sum(pos[t]["sh"] * row[colidx[t]] for t in pos if not np.isnan(row[colidx[t]]))
            slot = equity / maxpos
            for t in fired:
                if len(pos) >= maxpos: break
                p = row[colidx[t]]; presig = px[i - 3][colidx[t]]  # close before the signal day
                if np.isnan(p) or p <= 0 or np.isnan(presig): continue
                if cash < slot * 0.5: continue
                val = min(slot, cash / (1 + c)); sh = val / p
                cash -= val * (1 + c); total_cost += val * c
                pos[t] = {"sh": sh, "ep": p, "ei": i, "ed": dates[i], "xi": i + hold_td, "presig": presig}
        equity = cash + sum(pos[t]["sh"] * row[colidx[t]] for t in pos if not np.isnan(row[colidx[t]]))
        eqc.append(equity); eqsum += equity; eqn += 1
    eq = pd.Series(eqc, index=dates)
    return eq, trades, total_cost, eqsum / eqn


def _tr(strat, t, p, exit_px, exit_date, reason):
    entry_val = p["sh"] * p["ep"]
    return dict(strategy=strat, ticker=t, entry_date=p["ed"].date(), entry_px=round(p["ep"], 2),
                exit_date=exit_date.date(), exit_px=round(exit_px, 2),
                hold_days=(exit_date - p["ed"]).days, ret=exit_px / p["ep"] - 1,
                entry_val=entry_val, reason=reason)


# ---------------- benchmarks ----------------
def bench_omx():
    eq = (OMX / OMX.iloc[0]).dropna()
    return eq

def bench_ew():
    r = PX.pct_change().mean(axis=1)   # equal-weight, daily rebalanced universe (dividend-adjusted)
    return (1 + r.fillna(0)).cumprod()


# ================= RUN =================
print("\nRunning strategies...")
A_eq, A_tr, A_cost, A_meq = run_A(63, 6, SLIP_BASE, regime=True)
A_norg_eq, A_norg_tr, A_norg_cost, A_norg_meq = run_A(63, 6, SLIP_BASE, regime=False)
B5_eq, B5_tr, B5_cost, B5_meq = run_B(30, SLIP_BASE)   # 6 weeks
B4_eq, B4_tr, B4_cost, B4_meq = run_B(20, SLIP_BASE)   # 4 weeks
omx = bench_omx(); ew = bench_ew()

def yearly(eq):
    return (eq.resample("YE").last().pct_change().dropna() * 100).round(1)

rows = [
    ("A: momentum+regime", metrics(A_eq, A_tr, A_cost, A_meq)),
    ("A: NO regime filter", metrics(A_norg_eq, A_norg_tr, A_norg_cost, A_norg_meq)),
    ("B: PEAD proxy 6wk", metrics(B5_eq, B5_tr, B5_cost, B5_meq)),
    ("B: PEAD proxy 4wk", metrics(B4_eq, B4_tr, B4_cost, B4_meq)),
    ("Bench ^OMX (price, no div)", metrics(omx, [], 0, 1)),
    ("Bench universe EW (adj)", metrics(ew, [], 0, 1)),
]
print("\n" + "=" * 118)
print(f"{'Strategy':28}{'CAGR':>7}{'Vol':>7}{'Sharpe':>8}{'MaxDD':>8}{'Win%':>7}{'HoldD':>7}{'Turn/yr':>9}{'CostDrag':>9}{'xMult':>8}{'Trades':>8}")
print("-" * 118)
for name, m in rows:
    print(f"{name:28}{m['cagr']*100:>6.1f}%{m['vol']*100:>6.1f}%{m['sharpe']:>8.2f}{m['maxdd']*100:>7.1f}%"
          f"{(m['win']*100 if not np.isnan(m['win']) else 0):>6.0f}%{(m['hold'] if not np.isnan(m['hold']) else 0):>7.0f}"
          f"{(m['turnover']*100 if not np.isnan(m['turnover']) else 0):>8.0f}%{(m['cost_drag']*100 if not np.isnan(m['cost_drag']) else 0):>8.2f}%"
          f"{m['mult']:>7.2f}x{m['n_trades']:>8}")
print("=" * 118)

# cost sensitivity (slippage 10/16/30) for A and B6
print("\nCOST SENSITIVITY (slippage bps; courtage 6 bps always) — CAGR after costs:")
print(f"{'slippage':>10}{'A CAGR':>10}{'B6 CAGR':>10}")
for sb in SLIP_GRID:
    a = run_A(63, 6, sb, regime=True); b = run_B(30, sb)
    print(f"{sb:>8}bp{metrics(a[0],a[1],a[2],a[3])['cagr']*100:>9.1f}%{metrics(b[0],b[1],b[2],b[3])['cagr']*100:>9.1f}%")

# year-by-year
print("\nYEAR-BY-YEAR RETURN (%):")
yy = pd.DataFrame({"A": yearly(A_eq), "A_norg": yearly(A_norg_eq), "B6": yearly(B5_eq),
                   "B4": yearly(B4_eq), "OMX": yearly(omx), "EW": yearly(ew)})
print(yy.to_string())

# sensitivity grid for A: lookback 1/2/3/6 months x top 4/6/10
print("\nSTRATEGY A SENSITIVITY — CAGR% (after 16bp costs, regime on):")
lbs = {"1m": 21, "2m": 42, "3m": 63, "6m": 126}
grid = pd.DataFrame(index=list(lbs), columns=["top4", "top6", "top10"], dtype=float)
for lname, lb in lbs.items():
    for tn in (4, 6, 10):
        e, tr, cst, mq = run_A(lb, tn, SLIP_BASE, regime=True)
        grid.loc[lname, f"top{tn}"] = round(metrics(e, tr, cst, mq)["cagr"] * 100, 1)
print(grid.to_string())

# chart
fig, ax = plt.subplots(figsize=(12, 6))
for eq, lab in [(A_eq, "A momentum+regime"), (A_norg_eq, "A no-regime"), (B5_eq, "B PEAD 6wk"),
                (omx, "^OMX (no div)"), (ew, "Universe EW")]:
    ax.plot(eq.index, eq / eq.iloc[0], label=lab, lw=1.3)
ax.set_yscale("log"); ax.set_title("Swedish swing strategies vs benchmarks (log scale)")
ax.set_ylabel("Growth of 1 (log)"); ax.legend(); ax.grid(alpha=0.3, which="both")
plt.tight_layout(); plt.savefig(os.path.join(OUTPUT_DIR, "swing_ab_equity.png"), dpi=150)

pd.DataFrame(A_tr + A_norg_tr + B5_tr + B4_tr).to_csv(os.path.join(OUTPUT_DIR, "swing_ab_trades.csv"), index=False)

# survivorship bias + verdict
best_strat = max([("A", metrics(A_eq,A_tr,A_cost,A_meq)["cagr"]),
                  ("B6", metrics(B5_eq,B5_tr,B5_cost,B5_meq)["cagr"]),
                  ("B4", metrics(B4_eq,B4_tr,B4_cost,B4_meq)["cagr"])], key=lambda x: x[1])
omx_cagr = metrics(omx, [], 0, 1)["cagr"]; ew_cagr = metrics(ew, [], 0, 1)["cagr"]
print("\n" + "=" * 70)
print("SURVIVORSHIP BIAS (read before trusting anything above)")
print("=" * 70)
print(f"""The {len(data)}-name universe is TODAY's listed large/mid caps. Names delisted,
acquired, or bankrupted 2010-{date.today().year} (e.g. Swedish Match, Nordic
telecoms, several property/finance names in 2022-23) are ABSENT. Both the
strategies AND the equal-weight benchmark only ever picked from survivors.
Academic estimates put survivorship overstatement at ~1.5-4% CAGR/yr for a
biased single-market equity universe over ~15 years. So treat every CAGR here
as ~2-3 percentage points too high in absolute terms. It biases the momentum
strategy and the EW benchmark similarly, so the RELATIVE comparison (does the
strategy beat buy-and-hold?) is more trustworthy than the absolute figures.""")
print("\n" + "=" * 70)
print("VERDICT")
print("=" * 70)
best_cagr = best_strat[1]
print(f"Best strategy after costs: {best_strat[0]} at {best_cagr*100:.1f}% CAGR.")
print(f"Benchmarks: ^OMX (price, no div) {omx_cagr*100:.1f}%, universe EW buy&hold {ew_cagr*100:.1f}%.")
if best_cagr < ew_cagr:
    print(">> NEITHER strategy beats equal-weight buy-and-hold after costs. Negative result.")
elif best_cagr < omx_cagr:
    print(">> Best strategy beats EW but not ^OMX price index. Weak/negative.")
else:
    print(">> Best strategy beats both benchmarks after costs (before survivorship haircut).")
print("Chart: outputs/swing_ab_equity.png   Trades: outputs/swing_ab_trades.csv")
