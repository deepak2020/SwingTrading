"""
Signal generation for the OMXS30 momentum strategy.

Pure, broker-agnostic. Two responsibilities:
  - weekly_signal(): the Friday ranking -> which names to own (top-4) and which
    to keep holding (top-8 buffer).
  - trailing_stops(): the daily check -> which held names have breached their
    20% trailing stop.
"""

import time
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

import config

_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

# Friendly names for readable output
NAMES = {
    "ABB.ST": "ABB", "ADDT-B.ST": "Addtech", "ALFA.ST": "Alfa Laval",
    "ASSA-B.ST": "Assa Abloy", "AZN.ST": "AstraZeneca", "ATCO-A.ST": "Atlas Copco",
    "BOL.ST": "Boliden", "EPI-A.ST": "Epiroc", "EQT.ST": "EQT", "ERIC-B.ST": "Ericsson",
    "ESSITY-B.ST": "Essity", "EVO.ST": "Evolution", "HM-B.ST": "H&M", "HEXA-B.ST": "Hexagon",
    "INVE-B.ST": "Investor", "LIFCO-B.ST": "Lifco", "NIBE-B.ST": "NIBE", "NDA-SE.ST": "Nordea",
    "SAAB-B.ST": "SAAB", "SAND.ST": "Sandvik", "SEB-A.ST": "SEB", "SKA-B.ST": "Skanska",
    "SKF-B.ST": "SKF", "SCA-B.ST": "SCA", "SHB-A.ST": "Handelsbanken", "SWED-A.ST": "Swedbank",
    "TEL2-B.ST": "Tele2", "TELIA.ST": "Telia", "VOLV-B.ST": "Volvo",
}


def fetch_prices(tickers, years=2):
    """Daily adjusted closes for the universe, as a DataFrame (columns=tickers)."""
    now = datetime.now(timezone.utc)
    p1 = int((now.timestamp())) - int(years * 365.25 * 86400)
    p2 = int(now.timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    series = {}
    for tkr in tickers:
        for attempt in range(4):
            host = _YAHOO_HOSTS[attempt % 2]
            try:
                r = sess.get(f"https://{host}/v8/finance/chart/{tkr}",
                             params={"period1": p1, "period2": p2, "interval": "1d",
                                     "events": "div,splits"}, timeout=30)
                if r.status_code == 200:
                    res = r.json()["chart"]["result"][0]
                    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                    ind = res["indicators"]
                    adj = ind.get("adjclose", [{}])[0].get("adjclose") if "adjclose" in ind else None
                    close = adj if adj is not None else ind["quote"][0]["close"]
                    series[tkr] = pd.Series(close, index=idx, dtype=float)
                    break
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        time.sleep(0.1)
    df = pd.DataFrame(series).sort_index()
    return df.dropna(axis=1, thresh=int(len(df) * 0.8))


def weekly_signal(prices):
    """Return the current Friday-close ranking and target sets.

    Returns dict: asof, ranking (list of (tkr, momentum, above_trend)),
    target (top-N tickers to own), hold_ok (top-N+buffer tickers to keep),
    last_price (Series).
    """
    weekly = prices.resample("W-FRI").last().ffill(limit=2)
    mom = weekly.pct_change(config.MOMENTUM_WEEKS).iloc[-1]
    sma = weekly.rolling(config.TREND_SMA_WEEKS).mean().iloc[-1]
    price = weekly.iloc[-1]
    trend_ok = price > sma

    ranked = []
    for tkr in weekly.columns:
        if pd.notna(mom[tkr]) and bool(trend_ok[tkr]):
            ranked.append((tkr, float(mom[tkr])))
    ranked.sort(key=lambda x: x[1], reverse=True)
    order = [t for t, _ in ranked]

    return {
        "asof": weekly.index[-1].date().isoformat(),
        "ranking": [(t, m, True) for t, m in ranked],
        "target": order[:config.TOP_N],
        "hold_ok": set(order[:config.TOP_N + config.RANK_BUFFER]),
        "last_price": price,
    }


def latest_prices(prices):
    """Most recent close per ticker (Series)."""
    return prices.ffill().iloc[-1]


def trailing_stops(state, prices):
    """Update peaks and return tickers whose trailing stop is breached.

    Mutates state['positions'][t]['peak'] with the latest close.
    """
    px = latest_prices(prices)
    breached = []
    for tkr, pos in state["positions"].items():
        p = px.get(tkr)
        if p is None or pd.isna(p):
            continue
        pos["peak"] = max(pos.get("peak", pos["entry"]), float(p))
        if float(p) <= pos["peak"] * (1 - config.TRAIL_STOP_PCT):
            breached.append(tkr)
    return breached, px
