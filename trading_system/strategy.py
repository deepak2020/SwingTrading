"""
Signal generation for the OMXS30 momentum strategy.

Pure, broker-agnostic. Two responsibilities:
  - weekly_signal(): the Friday ranking -> which names to own (top-4) and which
    to keep holding (top-8 buffer).
  - trailing_stops(): the daily check -> which held names have breached their
    20% trailing stop.
"""

import io
import time
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

import config

_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def _stooq_ohlc(tkr, years):
    """Fallback OHLC source for when Yahoo is down (e.g. a feed-wide outage):
    Stooq's free daily CSV, no API key, no rate limit that matters at this
    scale. Only used per-ticker, after Yahoo has already failed for that
    ticker. Returns a DataFrame (Open/High/Low/Close, indexed by date) or
    None if Stooq doesn't have the symbol either."""
    try:
        r = requests.get("https://stooq.com/q/d/l/",
                          params={"s": tkr.lower(), "i": "d"}, timeout=20)
        if r.status_code != 200 or not r.text.strip().startswith("Date"):
            return None
        df = pd.read_csv(io.StringIO(r.text))
        if df.empty or "Close" not in df.columns:
            return None
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        cutoff = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=int(years * 365.25))
        df = df[df.index >= cutoff]
        return df[["Open", "High", "Low", "Close"]] if not df.empty else None
    except Exception:
        return None

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
    # Nifty 50 (the .NS suffix keeps the two universes distinct in one map)
    "RELIANCE.NS": "Reliance", "HDFCBANK.NS": "HDFC Bank", "ICICIBANK.NS": "ICICI Bank",
    "INFY.NS": "Infosys", "TCS.NS": "TCS", "ITC.NS": "ITC", "LT.NS": "L&T",
    "KOTAKBANK.NS": "Kotak Bank", "AXISBANK.NS": "Axis Bank", "SBIN.NS": "SBI",
    "BHARTIARTL.NS": "Bharti Airtel", "BAJFINANCE.NS": "Bajaj Finance",
    "HINDUNILVR.NS": "Hind. Unilever", "ASIANPAINT.NS": "Asian Paints",
    "MARUTI.NS": "Maruti Suzuki", "HCLTECH.NS": "HCL Tech", "SUNPHARMA.NS": "Sun Pharma",
    "TITAN.NS": "Titan", "ULTRACEMCO.NS": "UltraTech", "WIPRO.NS": "Wipro",
    "NESTLEIND.NS": "Nestle India", "ONGC.NS": "ONGC", "POWERGRID.NS": "Power Grid",
    "NTPC.NS": "NTPC", "TATAMOTORS.NS": "Tata Motors", "TATASTEEL.NS": "Tata Steel",
    "JSWSTEEL.NS": "JSW Steel", "ADANIENT.NS": "Adani Ent.", "ADANIPORTS.NS": "Adani Ports",
    "GRASIM.NS": "Grasim", "HDFCLIFE.NS": "HDFC Life", "SBILIFE.NS": "SBI Life",
    "BAJAJFINSV.NS": "Bajaj Finserv", "BAJAJ-AUTO.NS": "Bajaj Auto",
    "BRITANNIA.NS": "Britannia", "CIPLA.NS": "Cipla", "COALINDIA.NS": "Coal India",
    "DRREDDY.NS": "Dr Reddy's", "EICHERMOT.NS": "Eicher Motors",
    "HEROMOTOCO.NS": "Hero MotoCorp", "HINDALCO.NS": "Hindalco",
    "INDUSINDBK.NS": "IndusInd Bank", "M&M.NS": "M&M", "APOLLOHOSP.NS": "Apollo Hosp.",
    "BPCL.NS": "BPCL", "TECHM.NS": "Tech Mahindra", "TATACONSUM.NS": "Tata Consumer",
    "LTIM.NS": "LTIMindtree", "SHRIRAMFIN.NS": "Shriram Finance", "DIVISLAB.NS": "Divi's Labs",
}


def fetch_prices(tickers, years=2, provider="auto"):
    """Daily adjusted closes for the universe, as a DataFrame (columns=tickers).

    provider: "auto" (Yahoo, falling back to Stooq per-ticker on failure) or
    "stooq" (skip Yahoo entirely -- useful when Yahoo is known to be serving
    stale data, since that doesn't look like a "failure" to the auto path)."""
    now = datetime.now(timezone.utc)
    p1 = int((now.timestamp())) - int(years * 365.25 * 86400)
    p2 = int(now.timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    series = {}
    for tkr in tickers:
        got = False
        for attempt in range(4 if provider != "stooq" else 0):
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
                    got = True
                    break
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        if not got:
            stq = _stooq_ohlc(tkr, years)
            if stq is not None:
                series[tkr] = stq["Close"]
        time.sleep(0.1)
    df = pd.DataFrame(series).sort_index()
    return df.dropna(axis=1, thresh=int(len(df) * 0.8))


def fetch_ohlc(tickers, years=3, provider="auto"):
    """Daily split/dividend-adjusted OHLC for the universe.

    provider: "auto" (Yahoo, falling back to Stooq per-ticker) or "stooq"
    (skip Yahoo entirely -- see fetch_prices() for why that matters).

    Returns {'close','high','low','open'} as DataFrames (columns=tickers),
    restricted to names with >=80% close coverage. The close frame is identical
    in meaning to fetch_prices()'s output, so the momentum book can reuse it and
    the whole page needs only one network fetch.
    """
    now = datetime.now(timezone.utc)
    p1 = int(now.timestamp()) - int(years * 365.25 * 86400)
    p2 = int(now.timestamp())
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    frames = {"close": {}, "high": {}, "low": {}, "open": {}}
    for tkr in tickers:
        got = False
        for attempt in range(4 if provider != "stooq" else 0):
            host = _YAHOO_HOSTS[attempt % 2]
            try:
                r = sess.get(f"https://{host}/v8/finance/chart/{tkr}",
                             params={"period1": p1, "period2": p2, "interval": "1d",
                                     "events": "div,splits"}, timeout=30)
                if r.status_code == 200:
                    res = r.json()["chart"]["result"][0]
                    if "timestamp" not in res:
                        break
                    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                    q = res["indicators"]["quote"][0]
                    close = pd.Series(q["close"], index=idx, dtype=float)
                    adj_list = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
                    adj = pd.Series(adj_list, index=idx, dtype=float) if adj_list is not None else close
                    factor = (adj / close).fillna(1.0)     # back-adjust OHL by the close ratio
                    frames["close"][tkr] = adj
                    frames["high"][tkr] = pd.Series(q["high"], index=idx, dtype=float) * factor
                    frames["low"][tkr] = pd.Series(q["low"], index=idx, dtype=float) * factor
                    frames["open"][tkr] = pd.Series(q["open"], index=idx, dtype=float) * factor
                    got = True
                    break
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        if not got:
            stq = _stooq_ohlc(tkr, years)
            if stq is not None:
                frames["close"][tkr] = stq["Close"]
                frames["high"][tkr] = stq["High"]
                frames["low"][tkr] = stq["Low"]
                frames["open"][tkr] = stq["Open"]
        time.sleep(0.1)
    close = pd.DataFrame(frames["close"]).sort_index()
    keep = close.dropna(axis=1, thresh=int(len(close) * 0.8)).columns
    return {k: pd.DataFrame(v).sort_index()[keep] for k, v in frames.items()}


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


def sr_signal(prices, lookback_w=12, support_touch=0.03, trend_sma_w=40):
    """Experimental trailing support/resistance watch (see ../backtest_sr_trailing.py).

    Close-only approximation of the backtest signal (the live feed has no OHLC):
      * support   = rolling `lookback_w`-week low of the WEEKLY CLOSE
      * near      = latest weekly close within `support_touch` of that support
      * uptrend   = weekly close above its `trend_sma_w`-week SMA
      * bullish   = this week's close above last week's (an "up week" proxy for a
                    bullish candle, since we have no open/high/low)

    Returns a list of candidate dicts sorted by proximity to support (closest
    first) — i.e. the names this strategy would buy the dip on right now.
    """
    weekly = prices.resample("W-FRI").last().ffill(limit=2)
    if len(weekly) < trend_sma_w + 2:
        return []
    support = weekly.rolling(lookback_w).min().iloc[-1]
    sma = weekly.rolling(trend_sma_w).mean().iloc[-1]
    last = weekly.iloc[-1]
    prev = weekly.iloc[-2]

    out = []
    for tkr in weekly.columns:
        s, c, m, p = support.get(tkr), last.get(tkr), sma.get(tkr), prev.get(tkr)
        if any(pd.isna(v) for v in (s, c, m, p)) or s <= 0:
            continue
        dist = (c - s) / s                       # how far above support (fraction)
        near = c <= s * (1 + support_touch)
        uptrend = c > m
        bullish = c > p
        if near and uptrend and bullish:
            out.append({
                "ticker": tkr,
                "name": NAMES.get(tkr, tkr),
                "price": float(c),
                "support": float(s),
                "pct_above": float(dist * 100),
                "sma40": float(m),
            })
    out.sort(key=lambda x: x["pct_above"])
    return out


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
