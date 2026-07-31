"""
Live simulated paper book for the Trailing Support/Resistance strategy.

This is the SAME strategy validated in ../backtest_sr_trailing.py, run forward on
live data. It is a *deterministic replay*: every time it runs it re-simulates the
whole strategy from the start of the fetched history to the latest bar, so there
is no drift and no missed days — the current holdings and today's signals are
always exactly what the rules dictate.

Rules (weekly entries on Friday close, daily trailing-stop exits):
  BUY  a name whose weekly LOW is within SR_SUPPORT_TOUCH of its SR_LOOKBACK_W
       -week low (support), while its weekly close is above its SR_TREND_SMA_W
       SMA (uptrend) and the week closed up (close > open). Fill the closest-to-
       support names first, up to SR_MAX_POS equal-weight positions.
  SELL on a 20% trailing stop from the position's peak, OR a 5% break below the
       support level it was bought at.

Everything here is paper/simulation. It never places an order.
"""

import pandas as pd

import config
import strategy
from strategy import NAMES

COMMISSION_PCT = config.COMMISSION_PCT
COMMISSION_MIN = config.COMMISSION_MIN


def _weeklies(ohlc):
    close, high, low, openp = ohlc["close"], ohlc["high"], ohlc["low"], ohlc["open"]
    return (
        close.resample("W-FRI").last(),
        high.resample("W-FRI").max(),
        low.resample("W-FRI").min(),
        openp.resample("W-FRI").first(),
    )


def simulate(ohlc, cap=None,
             lookback_w=None, support_touch=None, trend_sma_w=None,
             stop_below=None, trail=None, max_pos=None):
    """Replay the strategy over `ohlc` and return the current book + signals."""
    cap = float(config.CAPITAL if cap is None else cap)
    lookback_w = config.SR_LOOKBACK_W if lookback_w is None else lookback_w
    support_touch = config.SR_SUPPORT_TOUCH if support_touch is None else support_touch
    trend_sma_w = config.SR_TREND_SMA_W if trend_sma_w is None else trend_sma_w
    stop_below = config.SR_STOP_BELOW if stop_below is None else stop_below
    trail = config.SR_TRAIL if trail is None else trail
    max_pos = config.SR_MAX_POS if max_pos is None else max_pos

    close = ohlc["close"]
    if close is None or close.empty:
        return None
    wc, wh, wl, wo = _weeklies(ohlc)
    support = wl.rolling(lookback_w).min().shift(1)      # shift(1): no lookahead
    trend_sma = wc.rolling(trend_sma_w).mean()
    fri = {d: i for i, d in enumerate(wc.index)}

    start_idx = max(lookback_w, trend_sma_w) + 1
    if start_idx >= len(wc):
        return None
    start_date = wc.index[start_idx]

    cash = cap
    pos = {}          # ticker -> dict(sh, entry, entry_date, peak, sup_entry, last, stale)
    trades = []       # dict(date, action, ticker, shares, price, reason, fee)
    equity = []       # (date, value)

    def sell(t, price, date, reason):
        nonlocal cash
        sh = pos[t]["sh"]
        proceeds = sh * price
        fee = max(proceeds * COMMISSION_PCT, COMMISSION_MIN)
        cash += proceeds - fee
        trades.append({"date": date, "action": "SELL", "ticker": t, "shares": sh,
                       "price": round(price, 2), "reason": reason, "fee": round(fee)})
        del pos[t]

    dates = close.index[close.index >= start_date]
    for d in dates:
        px = close.loc[d]
        # --- daily exits: trailing stop, support-failure stop, delist safety ---
        for t in list(pos):
            p = px.get(t)
            if pd.isna(p):
                pos[t]["stale"] = pos[t].get("stale", 0) + 1
                if pos[t]["stale"] > 10:
                    sell(t, pos[t]["last"], d, "delisted")
                continue
            pos[t]["stale"] = 0
            pos[t]["last"] = float(p)
            pos[t]["peak"] = max(pos[t]["peak"], float(p))
            if p <= pos[t]["peak"] * (1 - trail):
                sell(t, float(p), d, "trailing stop")
            elif p <= pos[t]["sup_entry"] * (1 - stop_below):
                sell(t, float(p), d, "support broke")
        # --- weekly entries on Fridays ---
        if d in fri:
            c, l, o = wc.loc[d], wl.loc[d], wo.loc[d]
            sup, sma = support.loc[d], trend_sma.loc[d]
            slots = max_pos - len(pos)
            if slots > 0:
                pv = cash + sum(p_["sh"] * (px.get(t) if pd.notna(px.get(t)) else p_["last"])
                                for t, p_ in pos.items())
                cand = []
                for t in wc.columns:
                    if t in pos:
                        continue
                    sv, lo, cl, op, smv = (sup.get(t), l.get(t), c.get(t), o.get(t), sma.get(t))
                    if any(pd.isna(v) for v in (sv, lo, cl, op, smv)) or sv <= 0:
                        continue
                    if lo <= sv * (1 + support_touch) and cl > op and cl > smv:
                        cand.append((t, (cl - sv) / sv))
                cand.sort(key=lambda x: x[1])
                alloc = pv / max_pos
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
                    trades.append({"date": d, "action": "BUY", "ticker": t, "shares": sh,
                                   "price": round(float(p), 2), "reason": "dip to support",
                                   "fee": round(fee)})
                    pos[t] = {"sh": sh, "entry": float(p), "entry_date": d, "peak": float(p),
                              "sup_entry": float(sup.get(t, p)), "last": float(p), "stale": 0}
        pv = cash + sum(p_["sh"] * (px.get(t) if pd.notna(px.get(t)) else p_["last"])
                        for t, p_ in pos.items())
        equity.append((d, pv))

    # ---------------- assemble the current view ----------------
    last_date = dates[-1]
    px = close.loc[last_date]
    latest_week = wc.index[-1]

    # current holdings, with live stop levels
    positions = []
    invested = 0.0
    for t, p_ in pos.items():
        now = float(px.get(t)) if pd.notna(px.get(t)) else p_["last"]
        value = p_["sh"] * now
        invested += value
        trail_stop = p_["peak"] * (1 - trail)
        sup_stop = p_["sup_entry"] * (1 - stop_below)
        stop_price = max(trail_stop, sup_stop)
        positions.append({
            "ticker": t, "name": NAMES.get(t, t), "shares": p_["sh"],
            "entry": round(p_["entry"], 2), "entry_date": p_["entry_date"].date().isoformat(),
            "now": round(now, 2), "peak": round(p_["peak"], 2), "value": round(value, 0),
            "pl_pct": round((now / p_["entry"] - 1) * 100, 1),
            "pl_sek": round(p_["sh"] * (now - p_["entry"]), 0),
            "stop_price": round(stop_price, 2),
            "stop_dist_pct": round((now / stop_price - 1) * 100, 1) if stop_price else None,
        })
    positions.sort(key=lambda x: x["value"], reverse=True)

    account_value = cash + invested
    n_years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    cagr = (account_value / cap) ** (1 / n_years) - 1 if account_value > 0 else float("nan")

    # today's executed signals (trades on the latest bar) + a forward watchlist
    buy_today = [t for t in trades if t["date"] == last_date and t["action"] == "BUY"]
    sell_today = [t for t in trades if t["date"] == last_date and t["action"] == "SELL"]

    # watchlist: names meeting the entry test on the latest week but NOT held
    # (what would get bought at the next Friday close if a slot is free)
    watch = []
    c, l, o = wc.iloc[-1], wl.iloc[-1], wo.iloc[-1]
    sup, sma = support.iloc[-1], trend_sma.iloc[-1]
    for t in wc.columns:
        if t in pos:
            continue
        sv, lo, cl, op, smv = (sup.get(t), l.get(t), c.get(t), o.get(t), sma.get(t))
        if any(pd.isna(v) for v in (sv, lo, cl, op, smv)) or sv <= 0:
            continue
        if lo <= sv * (1 + support_touch) and cl > op and cl > smv:
            watch.append({"ticker": t, "name": NAMES.get(t, t), "price": round(float(cl), 2),
                          "support": round(float(sv), 2),
                          "pct_above": round((cl - sv) / sv * 100, 1)})
    watch.sort(key=lambda x: x["pct_above"])
    free_slots = max_pos - len(pos)

    fmt = lambda tr: {"name": NAMES.get(tr["ticker"], tr["ticker"]), "ticker": tr["ticker"],
                      "shares": tr["shares"], "price": tr["price"],
                      "reason": tr["reason"], "fee": tr["fee"]}

    recent = [{"date": t["date"].date().isoformat(), "action": t["action"],
               "name": NAMES.get(t["ticker"], t["ticker"]), "ticker": t["ticker"],
               "price": t["price"], "reason": t["reason"]} for t in trades[-8:]][::-1]

    return {
        "as_of": last_date.date().isoformat(),
        "week_of": latest_week.date().isoformat(),
        "start_capital": round(cap),
        "account_value": round(account_value),
        "cash": round(cash),
        "invested": round(invested),
        "exposure_pct": round(invested / account_value * 100, 1) if account_value else 0.0,
        "total_pl": round(account_value - cap),
        "total_pl_pct": round((account_value / cap - 1) * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "since": dates[0].date().isoformat(),
        "n_positions": len(positions), "max_pos": max_pos, "free_slots": free_slots,
        "positions": positions,
        "buy_today": [fmt(t) for t in buy_today],
        "sell_today": [fmt(t) for t in sell_today],
        "watch": watch,
        "recent": recent,
        "equity_history": [{"date": d.date().isoformat(), "value": round(v, 2)} for d, v in equity],
        "trail_pct": int(trail * 100),
    }
