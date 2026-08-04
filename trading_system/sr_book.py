"""
Editable Trailing Support/Resistance paper book.

This is a REAL second portfolio you record your own S/R fills into (like the
momentum book), evaluated against the backtested strategy's rules so the
dashboard can tell you:

  * SELL now  — a holding has hit its exit: price at/below its 20% trailing stop
                (from the peak close since you bought) or 5% below the support it
                was bought at.
  * BUY now   — names currently at support in an uptrend that the strategy would
                open, up to SR_MAX_POS positions (skipping names you already hold).

Strategy (see ../backtest_sr_trailing.py):
  entry = weekly LOW within SR_SUPPORT_TOUCH of the SR_LOOKBACK_W-week low, weekly
          close above the SR_TREND_SMA_W SMA, and an up week (close > open).
  exit  = 20% trailing stop from the peak, or 5% below the entry support level.

Nothing here places an order — it evaluates your recorded positions and the live
signal. Peaks and support levels are derived from live price history, so you only
need to record what you bought (shares, price, date).
"""

import pandas as pd

import config
from strategy import NAMES


def _weeklies(ohlc):
    return (
        ohlc["close"].resample("W-FRI").last(),
        ohlc["high"].resample("W-FRI").max(),
        ohlc["low"].resample("W-FRI").min(),
        ohlc["open"].resample("W-FRI").first(),
    )


def _signal_week_pos(week_index):
    """Position of the last COMPLETED weekly bar. A week labelled Friday F counts as
    complete once it is past F 17:30 Stockholm (the market close). This freezes the
    buy signal to the most recent Friday close — stable intraweek, rolls forward
    right after Friday's close — so the signal never flickers during the day."""
    try:
        now = pd.Timestamp.now(tz="Europe/Stockholm").tz_localize(None)
    except Exception:
        now = pd.Timestamp.now()
    completed = [i for i, d in enumerate(week_index)
                 if now >= d.normalize() + pd.Timedelta(hours=17, minutes=30)]
    return completed[-1] if completed else len(week_index) - 1


def _watchlist(ohlc, held, lookback_w, support_touch, trend_sma_w):
    """Names meeting the entry test at the last COMPLETED Friday close, not already held.

    Entry test (matches backtest_sr_trailing.py): the week's LOW dipped to within
    `support_touch` of the PRIOR `lookback_w`-week low (support, excluding that week
    via shift(1)), the week closed UP (close>open, the bounce), and the close is
    above the `trend_sma_w` SMA (uptrend). Evaluated on the last completed week so
    the signal does not flicker intraday; the buy fills at that close, so the close
    can sit above support after the bounce — that is expected.
    """
    wc, wh, wl, wo = _weeklies(ohlc)
    if len(wc) < trend_sma_w + 2:
        return []
    pos = _signal_week_pos(wc.index)
    support = wl.rolling(lookback_w).min().shift(1).iloc[pos]   # prior weeks only
    sma = wc.rolling(trend_sma_w).mean().iloc[pos]
    c, l, o = wc.iloc[pos], wl.iloc[pos], wo.iloc[pos]
    live = ohlc["close"].ffill().iloc[-1]                       # latest price, for drift check
    out = []
    for t in wc.columns:
        if t in held:
            continue
        sv, lo, cl, op, smv = support.get(t), l.get(t), c.get(t), o.get(t), sma.get(t)
        if any(pd.isna(v) for v in (sv, lo, cl, op, smv)) or sv <= 0:
            continue
        if lo <= sv * (1 + support_touch) and cl > op and cl > smv:
            now = float(live.get(t)) if pd.notna(live.get(t)) else float(cl)
            moved = (now / cl - 1) * 100 if cl else 0.0        # drift since the signal close
            out.append({"ticker": t, "name": NAMES.get(t, t),
                        "price": round(float(cl), 2),          # signal = the Friday close
                        "now": round(now, 2),                  # live price today
                        "moved_pct": round(moved, 1),          # how far it has drifted since
                        "stale": abs(moved) > 4,               # ran away from the signal
                        "support": round(float(sv), 2),
                        "low": round(float(lo), 2),            # the low that touched support
                        "low_vs_sup": round((lo / sv - 1) * 100, 1),
                        "bounce_pct": round((cl - lo) / lo * 100, 1),  # how far it bounced
                        "pct_above": round((cl - sv) / sv * 100, 1)})  # close vs support
    out.sort(key=lambda x: x["low_vs_sup"])   # deepest touch of support first
    return out


def _monitor(ohlc, held, exclude, lookback_w, support_touch, trend_sma_w, max_above=0.15):
    """Pre-signal watchlist: names in an uptrend (above the 40-week SMA) hovering
    within `max_above` of their support — the ones a dip this week could turn into
    a Friday buy signal. Excludes held names and names already firing a signal.
    Reported so you know what to monitor through the week before the Friday close."""
    wc, wh, wl, wo = _weeklies(ohlc)
    if len(wc) < trend_sma_w + 2:
        return []
    pos = _signal_week_pos(wc.index)
    support = wl.rolling(lookback_w).min().shift(1).iloc[pos]
    sma = wc.rolling(trend_sma_w).mean().iloc[pos]
    live = ohlc["close"].ffill().iloc[-1]
    out = []
    for t in wc.columns:
        if t in held or t in exclude:
            continue
        sv, smv, price = support.get(t), sma.get(t), live.get(t)
        if any(pd.isna(v) for v in (sv, smv, price)) or sv <= 0:
            continue
        if price <= smv:                       # uptrend only (above 40-week SMA)
            continue
        above = price / sv - 1
        if above > max_above:                  # too far above support to trigger this week
            continue
        out.append({"ticker": t, "name": NAMES.get(t, t), "price": round(float(price), 2),
                    "support": round(float(sv), 2), "pct_above": round(above * 100, 1),
                    "dip_to_buy": round(float(sv * (1 + support_touch)), 2),
                    "at_support": above <= support_touch})
    out.sort(key=lambda x: x["pct_above"])     # closest to support first
    return out


def _support_at(ohlc, ticker, since, lookback_w):
    """The support level (rolling low) as of the entry date, from history."""
    try:
        wl = ohlc["low"][ticker].resample("W-FRI").min()
        upto = wl.loc[:since].dropna()
        if len(upto) == 0:
            return None
        return float(upto.tail(lookback_w).min())
    except Exception:
        return None


def live_book(ohlc, state,
              lookback_w=None, support_touch=None, trend_sma_w=None,
              stop_below=None, trail=None, max_pos=None, capital=None):
    """Evaluate the user's recorded S/R positions against the live signal.
    `capital` sets the default cash/P-L baseline (SEK book: config.CAPITAL;
    Nifty book: config.NIFTY_CAPITAL)."""
    capital = config.CAPITAL if capital is None else capital
    lookback_w = config.SR_LOOKBACK_W if lookback_w is None else lookback_w
    support_touch = config.SR_SUPPORT_TOUCH if support_touch is None else support_touch
    trend_sma_w = config.SR_TREND_SMA_W if trend_sma_w is None else trend_sma_w
    stop_below = config.SR_STOP_BELOW if stop_below is None else stop_below
    trail = config.SR_TRAIL if trail is None else trail
    max_pos = config.SR_MAX_POS if max_pos is None else max_pos

    close = ohlc["close"] if ohlc else None
    if close is None or close.empty:
        return None
    last = close.ffill().iloc[-1]
    as_of = close.index[-1].date().isoformat()

    positions = state.get("positions", {})
    held = set(positions)

    rows = []
    sell_signals = []
    invested = 0.0
    for tkr, pos in positions.items():
        entry = float(pos["entry"])
        shares = int(pos["shares"])
        since = pos.get("since")
        now = float(last.get(tkr)) if pd.notna(last.get(tkr)) else entry

        # peak = highest close since entry (fallback to recorded peak / entry)
        peak = float(pos.get("peak", entry))
        try:
            if since and tkr in close.columns:
                hist_peak = close[tkr].loc[since:].max()
                if pd.notna(hist_peak):
                    peak = max(peak, float(hist_peak))
        except Exception:
            pass
        peak = max(peak, now, entry)

        sup_entry = pos.get("sup_entry")
        if sup_entry is None:
            sup_entry = _support_at(ohlc, tkr, since, lookback_w)
        trail_stop = peak * (1 - trail)
        sup_stop = (sup_entry * (1 - stop_below)) if sup_entry else 0.0
        stop_price = max(trail_stop, sup_stop)
        breaching = now <= stop_price
        reason = ("trailing stop" if now <= trail_stop else "support broke") if breaching else None

        value = shares * now
        invested += value
        row = {
            "ticker": tkr, "name": NAMES.get(tkr, tkr), "shares": shares,
            "entry": round(entry, 2), "since": since or "",
            "now": round(now, 2), "peak": round(peak, 2), "value": round(value, 0),
            "pl_pct": round((now / entry - 1) * 100, 1) if entry else 0.0,
            "pl_sek": round(shares * (now - entry), 0),
            "stop_price": round(stop_price, 2),
            "stop_dist_pct": round((now / stop_price - 1) * 100, 1) if stop_price else None,
            "action": "SELL" if breaching else "HOLD",
            "reason": reason,
        }
        rows.append(row)
        if breaching:
            sell_signals.append({"name": row["name"], "ticker": tkr, "shares": shares,
                                 "price": row["now"], "reason": reason})
    rows.sort(key=lambda r: r["value"], reverse=True)

    cash = float(state.get("cash", capital))
    account_value = cash + invested
    for r in rows:
        r["weight"] = round(r["value"] / invested * 100, 1) if invested else 0.0

    # buy signals = watchlist at the last COMPLETED Friday close (stable all week)
    watch = _watchlist(ohlc, held, lookback_w, support_touch, trend_sma_w)
    free_slots = max(max_pos - len(held), 0)
    buy_signals = watch[:free_slots]

    # suggested size = 1/6 of CURRENT equity (matches the backtest's compounding).
    # Capped at available cash so the suggestion is actually affordable.
    target_sek = account_value / max_pos if max_pos else 0
    for s in watch:                       # buy_signals references these same dicts
        affordable = min(target_sek, cash)
        s["alloc_sek"] = round(target_sek)
        s["sugg_shares"] = int(affordable // s["now"]) if s.get("now") else 0

    # pre-signal watchlist to monitor through the week (uptrend, approaching support)
    monitor = _monitor(ohlc, held, {w["ticker"] for w in watch},
                       lookback_w, support_touch, trend_sma_w)

    wc = ohlc["close"].resample("W-FRI").last()
    signal_week = wc.index[_signal_week_pos(wc.index)].date().isoformat()

    # P/L is measured against NET CONTRIBUTIONS (what you actually put in), not a
    # fixed 100k — so SIP top-ups (recorded via "Set cash") don't count as profit.
    deposited = float(state.get("deposited", capital))
    total_pl = account_value - deposited
    return {
        "as_of": as_of,
        "signal_week": signal_week,
        "account_value": round(account_value),
        "cash": round(cash),
        "deposited": round(deposited),
        "invested": round(invested),
        "exposure_pct": round(invested / account_value * 100, 1) if account_value else 0.0,
        "total_pl": round(total_pl),
        "total_pl_pct": round(total_pl / deposited * 100, 1) if deposited else 0.0,
        "n_positions": len(rows), "max_pos": max_pos, "free_slots": free_slots,
        "trail_pct": int(trail * 100),
        "positions": rows,
        "sell_signals": sell_signals,
        "buy_signals": buy_signals,
        "watch": watch,
        "monitor": monitor,
    }
