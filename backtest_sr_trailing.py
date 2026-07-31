"""
Trailing Support/Resistance Swing Strategy — OMXS30 (the "let winners run" fix)
================================================================================
This is the best ACTIVE strategy found in this repo's research. It is the
support/resistance idea (backtest_sr.py) with ONE change that transformed it:

    Entry  : buy a DIP to support in an uptrend
             (weekly low within 3% of the rolling 12-week low, bullish weekly
              close, and price above its 40-week SMA)
    Exit   : a 20% TRAILING STOP from the position's peak  --- NOT selling at
             resistance. Plus a hard stop 5% below the entry's support level to
             cut early failures.

Selling at resistance (the original S/R rule) capped every winner and produced
only ~6.5% CAGR. Replacing that cap with a trailing stop lets winners run and
roughly TRIPLED the return while cutting turnover ~6x.

HEAD-TO-HEAD, survivors, 2010-2026, same universe/fees (run this script):
  S/R + 20% trailing   14.8% CAGR / Sharpe 1.00 / DD -23% / 9.5k fees / 131 trades
  Momentum champion    12.9% CAGR / Sharpe 0.75 / DD -33% /  84k fees / 864 trades
  Equal-weight buy&hold 17.1% CAGR / Sharpe 0.96 / DD -33% / 0 fees
Vs the CHAMPION the win is clean and robust: higher return, higher Sharpe, much
lower drawdown, and ~9x lower fees from ~6x fewer trades.

HONEST CAVEATS vs BUY & HOLD (do NOT oversell this):
  * On SURVIVORS it trails buy&hold on return (14.8% vs 17.1%) but wins on Sharpe
    (1.00 vs 0.96) and drawdown (-23% vs -33%).
  * SURVIVORSHIP-ADJUSTED (test B, +delisted names) the return edge DISAPPEARS:
    S/R 11.9% vs buy&hold 14.6%, Sharpe ties (0.82 vs 0.83). Only the drawdown
    stays better (~-29% vs -35%), and even that gap narrows once collapsing names
    are in the basket. My earlier "beats buy&hold once survivorship-adjusted"
    claim did NOT hold up on a clean re-run -- it was flattered by 2-3 names.
  * The 20% stop is a Sharpe sweet spot; 25% gives MORE return (15.9%), 15% less
    (10.5%). Return is stop-sensitive -> mild overfit risk.
  * Time-unstable: it TRAILED buy&hold in 2010-2018 (10.5% vs 16.0%) and only
    dominated in 2018-2026 (25.7% vs 13.0%) -- see test C.
  * Bottom line vs buy&hold: think "similar-or-slightly-lower return with a
    somewhat lower drawdown", NOT "market-beater". Vs the champion it is simply
    better. This is the best ACTIVE strategy here; buy&hold is still the
    hardest benchmark to beat on raw return.

DATA: yfinance is blocked behind this environment's egress proxy, so prices are
pulled from Yahoo's public chart API with `requests` (auto-adjusted closes). If
a download fails the script skips that name rather than inventing data.

Run:  python backtest_sr_trailing.py
"""

import os
import time
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
CAP = float(os.environ.get("CAPITAL", 100_000))     # SEK
START = os.environ.get("START", "2010-01-01")
END = os.environ.get("END", "2026-07-31")
COMMISSION_PCT = 0.0015                              # Nordnet 0.15%
COMMISSION_MIN = 39                                  # SEK min per trade

# Strategy parameters
LOOKBACK_W = 12          # Donchian window (weeks) for support
SUPPORT_TOUCH = 0.03     # "near support" = weekly low within 3% of rolling low
TREND_SMA_W = 40         # only buy dips while price is above its 40-week SMA
STOP_BELOW_SUP = 0.05    # early-failure stop: 5% below entry support level
TRAIL_DEFAULT = 0.20     # trailing stop from peak (the champion parameter)
MAX_POSITIONS = 6

# Live OMXS30 constituents (today's survivors — see survivorship note above)
SURVIVORS = [
    "ABB.ST", "ADDT-B.ST", "ALFA.ST", "ASSA-B.ST", "AZN.ST", "ATCO-A.ST",
    "BOL.ST", "EPI-A.ST", "EQT.ST", "ERIC-B.ST", "ESSITY-B.ST", "EVO.ST",
    "HM-B.ST", "HEXA-B.ST", "INVE-B.ST", "LIFCO-B.ST", "NIBE-B.ST", "NDA-SE.ST",
    "SAAB-B.ST", "SAND.ST", "SEB-A.ST", "SKA-B.ST", "SKF-B.ST", "SCA-B.ST",
    "SHB-A.ST", "SWED-A.ST", "TEL2-B.ST", "TELIA.ST", "VOLV-B.ST",
]
# Delisted / acquired / collapsed / dropped-out names, added ONLY for the
# survivorship-bias robustness test (test B). Failures are skipped.
DELISTED = [
    "SWMA.ST", "LUPE.ST", "ALIV-SDB.ST", "TIGO-SDB.ST", "MTG-B.ST", "FING-B.ST",
    "ORI.ST", "ENRO.ST", "SCA-A.ST", "NOKIA-SEK.ST", "BILL.ST", "GETI-B.ST",
    "SECU-B.ST", "PEAB-B.ST", "JM.ST", "TREL-B.ST", "HUSQ-B.ST", "ELUX-B.ST",
    "SSAB-A.ST", "KINV-B.ST", "SINCH.ST", "BALD-B.ST", "CAST.ST", "INTRUM.ST",
]

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


# --------------------------------------------------------------------------- #
# DATA
# --------------------------------------------------------------------------- #
def fetch_ohlc(tickers):
    """Return {'Close','High','Low','Open'} DataFrames of split/div-adjusted OHLC."""
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.fromisoformat(END).replace(tzinfo=timezone.utc).timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    frames = {"Close": {}, "High": {}, "Low": {}, "Open": {}}
    got = []
    for t in tickers:
        for attempt in range(4):
            try:
                r = sess.get(f"https://{_HOSTS[attempt % 2]}/v8/finance/chart/{t}",
                             params={"period1": p1, "period2": p2, "interval": "1d",
                                     "events": "div,splits"}, timeout=30)
                if r.status_code == 200:
                    res = r.json()["chart"]["result"][0]
                    if "timestamp" not in res:
                        break
                    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                    q = res["indicators"]["quote"][0]
                    close = pd.Series(q["close"], index=idx, dtype=float)
                    if close.dropna().shape[0] < 60:
                        break
                    al = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
                    adj = pd.Series(al, index=idx, dtype=float) if al is not None else close
                    factor = (adj / close).fillna(1.0)
                    frames["Close"][t] = adj
                    for k, name in (("open", "Open"), ("high", "High"), ("low", "Low")):
                        frames[name][t] = pd.Series(q[k], index=idx, dtype=float) * factor
                    got.append(t)
                    break
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        time.sleep(0.08)
    return {k: pd.DataFrame(v).sort_index() for k, v in frames.items()}, got


# --------------------------------------------------------------------------- #
# METRICS
# --------------------------------------------------------------------------- #
def stats(eq):
    eq = eq.dropna()
    final = eq.iloc[-1]
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (final / eq.iloc[0]) ** (1 / years) - 1
    ret = eq.pct_change().dropna()
    sharpe = (ret.mean() / ret.std()) * np.sqrt(252) if ret.std() > 0 else float("nan")
    mdd = (eq / eq.cummax() - 1).min()
    return dict(final=final, cagr=cagr, sharpe=sharpe, mdd=mdd, years=years)


# --------------------------------------------------------------------------- #
# THE STRATEGY: buy dips to support in an uptrend, exit on a trailing stop
# --------------------------------------------------------------------------- #
def run_trailing_sr(close, high, low, openp, trail=TRAIL_DEFAULT, cap=CAP):
    """Weekly entries, daily trailing-stop exits. Delist-safe: a held name whose
    price goes stale for >10 trading days is liquidated at its last real price."""
    wc = close.resample("W-FRI").last()
    wh = high.resample("W-FRI").max()
    wl = low.resample("W-FRI").min()
    wo = openp.resample("W-FRI").first()
    support = wl.rolling(LOOKBACK_W).min().shift(1)       # shift(1): no lookahead
    trend_sma = wc.rolling(TREND_SMA_W).mean()
    fri = {d: i for i, d in enumerate(wc.index)}

    cash = cap
    pos = {}       # ticker -> shares/entry/peak/sup_entry/last/stale
    equity = []
    n_trades = 0
    fees = 0.0

    def sell(t, price):
        nonlocal cash, n_trades, fees
        proceeds = pos[t]["sh"] * price
        fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
        cash += proceeds - fee
        fees += fee
        n_trades += 1
        del pos[t]

    start_idx = max(LOOKBACK_W, TREND_SMA_W) + 1
    if start_idx >= len(wc):
        return None
    start_date = wc.index[start_idx]

    for d in close.index[close.index >= start_date]:
        px = close.loc[d]
        # --- daily exits: trailing stop + early-failure stop + delist safety ---
        for t in list(pos):
            p = px.get(t)
            if pd.isna(p):
                pos[t]["stale"] = pos[t].get("stale", 0) + 1
                if pos[t]["stale"] > 10:
                    sell(t, pos[t]["last"])          # delisted -> liquidate
                continue
            pos[t]["stale"] = 0
            pos[t]["last"] = p
            pos[t]["peak"] = max(pos[t]["peak"], p)
            if p <= pos[t]["peak"] * (1 - trail):
                sell(t, p)
            elif p <= pos[t]["sup_entry"] * (1 - STOP_BELOW_SUP):
                sell(t, p)
        # --- weekly entries on Fridays ---
        if d in fri:
            c = wc.loc[d]; l = wl.loc[d]; o = wo.loc[d]
            sup = support.loc[d]; sma = trend_sma.loc[d]
            slots = MAX_POSITIONS - len(pos)
            if slots > 0:
                pv = cash + sum(pos[t]["sh"] * (px.get(t) if pd.notna(px.get(t)) else pos[t]["last"])
                                for t in pos)
                cand = []
                for t in wc.columns:
                    if t in pos:
                        continue
                    sv, lo, cl, op, smv = (sup.get(t, np.nan), l.get(t, np.nan),
                                           c.get(t, np.nan), o.get(t, np.nan), sma.get(t, np.nan))
                    if pd.isna(sv) or pd.isna(lo) or pd.isna(cl) or pd.isna(op):
                        continue
                    near = lo <= sv * (1 + SUPPORT_TOUCH)
                    bullish = cl > op
                    uptrend = pd.notna(smv) and cl > smv
                    if near and bullish and uptrend:
                        cand.append((t, (cl - sv) / sv))    # closest to support first
                cand.sort(key=lambda x: x[1])
                alloc = pv / MAX_POSITIONS
                for t, _ in cand[:slots]:
                    p = c.get(t)
                    if pd.isna(p) or p <= 0:
                        continue
                    sh = int(alloc // p)
                    if sh <= 0:
                        continue
                    cost = sh * p
                    fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
                    if cost + fee > cash:
                        continue
                    cash -= cost + fee
                    fees += fee
                    n_trades += 1
                    pos[t] = {"sh": sh, "entry": p, "peak": p,
                              "sup_entry": sup.get(t, p), "last": p, "stale": 0}
        pv = cash + sum(pos[t]["sh"] * (px.get(t) if pd.notna(px.get(t)) else pos[t]["last"])
                        for t in pos)
        equity.append((d, pv))

    eq = pd.DataFrame(equity, columns=["date", "equity"]).set_index("date")["equity"]
    return eq, n_trades, fees


def run_champion(close, trail=0.20, cap=CAP,
                 top_n=4, buf=4, mom_w=12, sma_w=20, exposure=0.85):
    """The live momentum champion, for an apples-to-apples comparison in ONE
    script: weekly top-N by 12-week momentum above a 20-week SMA, held with a
    rank buffer, exited by a daily trailing stop. Same fees as everything else."""
    w = close.resample("W-FRI").last().ffill(limit=2)
    mom = w.pct_change(mom_w)
    sma = w.rolling(sma_w).mean()
    trend = w > sma
    warm = mom_w + sma_w
    sig = {}
    for i in range(warm, len(w)):
        d = w.index[i]; mi = mom.iloc[i]; ti = trend.iloc[i]
        r = [t for t in w.columns if pd.notna(mi[t]) and bool(ti[t])]
        r.sort(key=lambda t: mi[t], reverse=True)
        sig[d] = {"target": r[:top_n], "hold_ok": set(r[:top_n + buf])}
    wk = close.index.to_period("W-FRI")
    ld = pd.Series(close.index, index=wk).groupby(level=0).last()
    reb = {ts: per.end_time.normalize() for per, ts in ld.items()}

    cash = cap; pos = {}; equity = []; n_trades = 0; fees = 0.0

    def sell(t, p):
        nonlocal cash, n_trades, fees
        proceeds = pos[t]["sh"] * p
        fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
        cash += proceeds - fee; fees += fee; n_trades += 1; del pos[t]

    start = w.index[warm]
    for d in close.index[close.index >= start]:
        px = close.loc[d]
        for t in list(pos):
            p = px.get(t)
            if pd.isna(p):
                continue
            pos[t]["peak"] = max(pos[t]["peak"], p)
            if p <= pos[t]["peak"] * (1 - trail):
                sell(t, p)
        fri = reb.get(d)
        s = sig.get(fri) if fri is not None else None
        if s:
            for t in list(pos):
                if t not in s["hold_ok"]:
                    p = px.get(t)
                    if not pd.isna(p):
                        sell(t, p)
            slots = top_n - len(pos)
            if slots > 0:
                pv = cash + sum(pos[t]["sh"] * px.get(t, pos[t]["entry"]) for t in pos)
                alloc = pv * exposure / top_n
                for t in [x for x in s["target"] if x not in pos][:slots]:
                    p = px.get(t)
                    if pd.isna(p) or p <= 0:
                        continue
                    sh = int(alloc // p)
                    if sh <= 0:
                        continue
                    cost = sh * p; fee = max(cost * COMMISSION_PCT, COMMISSION_MIN)
                    if cost + fee > cash:
                        continue
                    cash -= cost + fee; fees += fee; n_trades += 1
                    pos[t] = {"sh": sh, "entry": p, "peak": p}
        pv = cash + sum(pos[t]["sh"] * px.get(t, pos[t]["entry"]) for t in pos)
        equity.append((d, pv))
    eq = pd.DataFrame(equity, columns=["date", "equity"]).set_index("date")["equity"]
    return eq, n_trades, fees


def buy_hold(close, cap=CAP):
    """Equal-weight, daily-rebalanced across whatever names have data each day.
    Naturally survivorship-adjusted: a name drops out of the average after its
    last trading day."""
    m = close.pct_change().mean(axis=1).fillna(0)
    return (m + 1).cumprod() * cap


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main():
    all_names = list(dict.fromkeys(SURVIVORS + DELISTED))
    print(f"Downloading {len(all_names)} names (survivors + delisted attempts)...")
    raw, got = fetch_ohlc(all_names)
    # NOTE: do NOT ffill here. The delist-safe logic in run_trailing_sr relies on
    # a delisted name's price becoming NaN so the position is liquidated at its
    # last real price. ffill would freeze it flat forever and distort test B.
    close = raw["Close"]
    cols = close.columns
    high, low, openp = raw["High"][cols], raw["Low"][cols], raw["Open"][cols]
    surv = [t for t in SURVIVORS if t in cols]
    extra = [t for t in got if t not in SURVIVORS]
    print(f"Usable: {len(cols)} names  ({len(surv)} survivors + {len(extra)} delisted/extra)\n")

    def line(name, eq, n, f):
        s = stats(eq)
        print(f"{name:30s} {s['final']:>11,.0f} {s['cagr']*100:>6.1f}% "
              f"{s['sharpe']:>7.2f} {s['mdd']*100:>7.1f}% {f:>8,.0f} {n:>5}")

    hdr = f"{'Strategy':30s} {'FinalSEK':>11} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>8} {'Fees':>8} {'Trd':>5}"

    # ---- headline: survivors — trailing-S/R vs the champion vs buy & hold ----
    print("=" * 82)
    print(f"HEADLINE  —  survivors only ({len(surv)} names), 20% stops, same Nordnet fees")
    print("=" * 82)
    print(hdr); print("-" * 82)
    eq, n, f = run_trailing_sr(close[surv], high[surv], low[surv], openp[surv])
    line("S/R + 20% trailing", eq, n, f)
    ce, cn, cf = run_champion(close[surv].ffill())
    line("Momentum champion (live)", ce, cn, cf)
    bh = buy_hold(close[surv])
    line("Equal-weight buy & hold", bh, 0, 0)

    # ---- robustness A: trailing-stop sensitivity (survivors) ----
    print("\n" + "=" * 82)
    print("ROBUSTNESS A  —  trailing-stop sensitivity (survivors). 20% is a sweet spot.")
    print("=" * 82)
    print(hdr); print("-" * 82)
    for tr in (0.15, 0.20, 0.25):
        e, nn, ff = run_trailing_sr(close[surv], high[surv], low[surv], openp[surv], trail=tr)
        line(f"S/R trail {int(tr*100)}%", e, nn, ff)

    # ---- robustness B: wider universe incl. delisted (survivorship-honest) ----
    print("\n" + "=" * 82)
    print(f"ROBUSTNESS B  —  wider universe incl. delisted ({len(cols)} names), survivorship-honest.")
    print("                 Return edge over buy&hold DISAPPEARS; only drawdown stays better.")
    print("=" * 82)
    print(hdr); print("-" * 82)
    for tr in (0.15, 0.20, 0.25):
        e, nn, ff = run_trailing_sr(close, high, low, openp, trail=tr)
        line(f"S/R trail {int(tr*100)}%", e, nn, ff)
    line("Equal-weight buy & hold", buy_hold(close), 0, 0)

    # ---- robustness C: split-half (wider universe, 20%) ----
    print("\n" + "=" * 82)
    print("ROBUSTNESS C  —  split-half (wider universe, 20%). Edge is time-UNSTABLE:")
    print("                 trailed buy&hold in H1, dominated in H2.")
    print("=" * 82)
    print(hdr); print("-" * 82)
    mid = pd.Timestamp("2018-06-01")
    for lab, a, b in (("H1 2010-2018", pd.Timestamp(START), mid),
                      ("H2 2018-2026", mid, pd.Timestamp(END))):
        sl = lambda df: df[(df.index >= a) & (df.index < b)]
        res = run_trailing_sr(sl(close), sl(high), sl(low), sl(openp), trail=0.20)
        if res is None:
            print(f"{lab}: insufficient data"); continue
        e, nn, ff = res
        line(f"{lab}  S/R", e, nn, ff)
        line(f"{lab}  buy&hold", buy_hold(sl(close)), 0, 0)

    # ---- equity chart (headline vs buy&hold) ----
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(eq.index, eq, color="#0ea5e9", lw=1.3, label="S/R + 20% trailing")
    ax.plot(bh.index, bh, color="gray", lw=1.0, ls="--", label="Equal-weight buy & hold")
    ax.axhline(CAP, color="black", lw=0.6, alpha=0.4)
    ax.set_title("Trailing Support/Resistance vs Buy & Hold — OMXS30 survivors")
    ax.set_ylabel("SEK"); ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout()
    out = os.path.join(OUTPUT_DIR, "backtest_sr_trailing_results.png")
    plt.savefig(out, dpi=150)
    print(f"\nSaved chart -> {out}")


if __name__ == "__main__":
    main()
