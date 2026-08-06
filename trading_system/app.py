"""
Portfolio dashboard — a small local web app for the OMXS30 momentum system.

Reads the paper book (state.json), pulls live prices, and renders:
  - account value, cash, invested, exposure, total P/L vs starting capital
  - a positions table: shares, entry, now, peak, P/L, trailing-stop level + distance
  - this week's signal (top-N buy / buffer hold) with your holdings marked
  - an equity curve that accumulates one point per day the dashboard is opened

Run:  cd trading_system && python app.py     then open http://127.0.0.1:5000
It is read-only: it never places orders. Use run.py for rebalance/stops.
"""

import json
import os
import time
from datetime import date

import pandas as pd
import requests
from flask import Flask, Response, jsonify, redirect, render_template_string, request

import config
import engine
import sr_book
import strategy

app = Flask(__name__)

EQUITY_HISTORY_FILE = os.path.join(config.DATA_DIR, "equity_history.json")

# ---- optional password gate (required when hosting online) ----
# Set DASHBOARD_PASSWORD in the host's env to require a login. If unset (local
# use) the dashboard is open. Served over HTTPS by the host, so basic-auth
# credentials are encrypted in transit.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Log the storage mode at import (shows in Render logs under gunicorn too).
print(f"[dashboard] storage mode: {engine.storage_mode()}"
      f"{'  (edits persist)' if engine.storage_mode() == 'postgres' else '  (EPHEMERAL — set DATABASE_URL to persist edits)'}",
      flush=True)


@app.before_request
def _require_auth():
    if request.path == "/healthz":
        return  # keep-awake ping: no auth, no heavy work
    if not DASHBOARD_PASSWORD:
        return  # no password configured -> open (local dev)
    auth = request.authorization
    if not auth or auth.password != DASHBOARD_PASSWORD:
        return Response("Login required", 401,
                        {"WWW-Authenticate": 'Basic realm="Portfolio"'})


@app.route("/healthz")
def healthz():
    # Cheap liveness endpoint for an external uptime pinger to hit every ~10 min,
    # keeping a free-tier host from sleeping. Deliberately does NOT fetch prices.
    # 'storage' tells you whether edits persist: "postgres" = safe across
    # restarts; "file" = ephemeral on free hosts (set DATABASE_URL to fix).
    return {"status": "ok", "storage": engine.storage_mode()}, 200

# In-memory price cache so a page refresh doesn't re-fetch 29 tickers every time.
_CACHE = {"prices": None, "ohlc": None, "index": None, "ts": 0.0}
_CACHE_TTL = 300  # 5 minutes — matches the page's 5-min auto-refresh

# Separate cache for the Nifty 50 book (fetched lazily when /nifty is opened,
# so the main dashboard never waits on 50 extra tickers).
_NIFTY_CACHE = {"ohlc": None, "ts": 0.0}

_INDEX_TICKER = "^OMX"   # OMXS30 index on Yahoo


def _fetch_index():
    """Daily closes of the OMXS30 index (~1y) for the market-context card."""
    from urllib.parse import quote
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            r = sess.get(f"https://{hosts[attempt % 2]}/v8/finance/chart/{quote(_INDEX_TICKER)}",
                         params={"range": "1y", "interval": "1d"}, timeout=30)
            if r.status_code == 200:
                res = r.json()["chart"]["result"][0]
                idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
                close = res["indicators"]["quote"][0]["close"]
                return pd.Series(close, index=idx, dtype=float).dropna()
        except Exception:
            time.sleep(0.3 * (attempt + 1))
    return None


def get_prices(force=False):
    """Refresh the shared cache once per TTL. Pulls daily OHLC (used by both the
    momentum book's closes and the S/R paper book); falls back to close-only if
    the OHLC fetch fails so the momentum book always works."""
    if force or _CACHE["prices"] is None or (time.time() - _CACHE["ts"]) > _CACHE_TTL:
        ohlc = None
        try:
            ohlc = strategy.fetch_ohlc(config.TICKERS, years=config.SR_HISTORY_YEARS)
        except Exception:
            ohlc = None
        if ohlc is not None and not ohlc["close"].empty:
            _CACHE["ohlc"] = ohlc
            _CACHE["prices"] = ohlc["close"]
        else:
            _CACHE["ohlc"] = None
            _CACHE["prices"] = strategy.fetch_prices(config.TICKERS)
        _CACHE["index"] = _fetch_index()
        _CACHE["ts"] = time.time()
    return _CACHE["prices"]


def index_snapshot():
    """Level, day change, and 20-week-SMA regime for the OMXS30 index (or None)."""
    ix = _CACHE.get("index")
    if ix is None or len(ix) < 2:
        return None
    level = float(ix.iloc[-1])
    chg = (level / float(ix.iloc[-2]) - 1) * 100
    weekly = ix.resample("W-FRI").last().dropna()
    sma = weekly.rolling(config.TREND_SMA_WEEKS).mean().iloc[-1] if len(weekly) >= config.TREND_SMA_WEEKS else None
    vs_sma = (level / float(sma) - 1) * 100 if sma is not None and pd.notna(sma) else None
    return {
        "level": round(level, 1),
        "chg": round(chg, 2),
        "vs_sma": round(vs_sma, 1) if vs_sma is not None else None,
        "regime": ("uptrend" if vs_sma >= 0 else "downtrend") if vs_sma is not None else "n/a",
    }


def _load_equity_history():
    if os.path.exists(EQUITY_HISTORY_FILE):
        try:
            with open(EQUITY_HISTORY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _log_equity(value):
    """Record one equity point per calendar day (last write of the day wins)."""
    hist = _load_equity_history()
    today = date.today().isoformat()
    if hist and hist[-1]["date"] == today:
        hist[-1]["value"] = round(value, 2)
    else:
        hist.append({"date": today, "value": round(value, 2)})
    with open(EQUITY_HISTORY_FILE, "w") as f:
        json.dump(hist, f, indent=2)
    return hist


def build_snapshot(force=False):
    prices = get_prices(force=force)
    px = strategy.latest_prices(prices)
    prev_px = prices.ffill().iloc[-2] if len(prices) > 1 else px   # previous close, for today's change
    state = engine.load_state()
    sig = strategy.weekly_signal(prices)
    held = set(state["positions"])

    # --- positions ---
    positions = []
    invested = 0.0
    for tkr, pos in state["positions"].items():
        now = float(px.get(tkr, pos["entry"]))
        # Peak = highest close since entry (derived live), so the trailing stop
        # ratchets up as the stock rises. The stored peak alone goes stale because
        # nothing bumps it between CLI runs.
        peak = float(pos.get("peak", pos["entry"]))
        since = pos.get("since")
        try:
            if since and tkr in prices.columns:
                hist_peak = prices[tkr].loc[since:].max()
                if pd.notna(hist_peak):
                    peak = max(peak, float(hist_peak))
        except Exception:
            pass
        peak = max(peak, now, float(pos["entry"]))
        prev = float(prev_px.get(tkr, now))
        value = pos["shares"] * now
        invested += value
        stop_price = peak * (1 - config.TRAIL_STOP_PCT)
        positions.append({
            "ticker": tkr,
            "name": strategy.NAMES.get(tkr, tkr),
            "shares": pos["shares"],
            "entry": round(pos["entry"], 2),
            "now": round(now, 2),
            "peak": round(peak, 2),
            "day_chg": round((now / prev - 1) * 100, 1) if prev else None,
            "day_sek": round(pos["shares"] * (now - prev), 0) if prev else None,
            "value": round(value, 2),
            "pl_pct": round((now / pos["entry"] - 1) * 100, 1),
            "pl_sek": round(pos["shares"] * (now - pos["entry"]), 0),
            "stop_price": round(stop_price, 2),
            "stop_dist_pct": round((now / stop_price - 1) * 100, 1) if stop_price else None,
            "since": pos.get("since", ""),
        })
    positions.sort(key=lambda p: p["value"], reverse=True)
    for p in positions:
        p["weight"] = round(p["value"] / invested * 100, 1) if invested else 0.0

    # --- recommended actions: cross-reference holdings with this week's signal ---
    target = sig["target"]                      # top-N names to own
    hold_ok = sig["hold_ok"]                    # top-(N+buffer) names ok to keep
    rank_of = {t: i + 1 for i, (t, _, _) in enumerate(sig["ranking"])}
    buf = config.TOP_N + config.RANK_BUFFER

    def _fee(notional):
        return round(max(notional * config.COMMISSION_PCT, config.COMMISSION_MIN))

    sell, hold = [], []
    for p in positions:
        t, now, stop = p["ticker"], p["now"], p["stop_price"]
        if stop and now <= stop:
            p["action"] = "SELL"
            p["action_reason"] = f"{config.TRAIL_STOP_PCT:.0%} trailing stop hit"
        elif t not in hold_ok:
            p["action"] = "SELL"
            p["action_reason"] = (f"fell to rank {rank_of[t]} (outside top {buf})"
                                  if t in rank_of else "lost its uptrend (below 20-week SMA)")
        else:
            p["action"] = "HOLD"
            p["action_reason"] = f"rank {rank_of.get(t, '-')} — inside top {buf}"
        (sell if p["action"] == "SELL" else hold).append(
            {"ticker": t, "name": p["name"], "reason": p["action_reason"],
             "fee": _fee(p["value"])})

    slots = max(0, config.TOP_N - len(hold))    # open slots after keeping the holds
    alloc = (state["cash"] + invested) * config.MAX_EXPOSURE / config.TOP_N
    buy = []
    for t, m, _ in sig["ranking"]:
        if len(buy) >= slots:
            break
        if t in target and t not in held:
            buy.append({"ticker": t, "name": strategy.NAMES.get(t, t),
                        "mom": round(m * 100, 1), "rank": rank_of[t], "fee": _fee(alloc)})
    actions = {"sell": sell, "buy": buy, "hold": hold,
               "fee_total": sum(a["fee"] for a in sell) + sum(a["fee"] for a in buy)}

    signal_list = []
    for i, (t, m, _) in enumerate(sig["ranking"][:buf]):
        core = i < config.TOP_N
        act = ("OWN — core" if t in held else "BUY now") if core else \
              ("keep (buffer)" if t in held else "watch")
        signal_list.append({"rank": i + 1, "ticker": t, "name": strategy.NAMES.get(t, t),
                            "mom": round(m * 100, 1), "action": act, "held": t in held})

    account_value = engine.portfolio_value(state, px)
    hist = _log_equity(account_value)

    # P/L is measured against NET CONTRIBUTIONS (what you actually put in), not a
    # fixed 100k — so SIP top-ups (recorded via "Set cash") don't count as profit.
    deposited = float(state.get("deposited", config.CAPITAL))
    total_pl = account_value - deposited
    # Brokerage paid = the larger of what we've tracked on app trades and the
    # brokerage implied by the positions currently held (each was bought once).
    # This keeps the figure honest for holdings entered before fee-tracking, via
    # "Save" corrections, or on a fresh database — it never shows 0 while you hold.
    implied_entry_fees = sum(
        max(p["shares"] * p["entry"] * config.COMMISSION_PCT, config.COMMISSION_MIN)
        for p in positions)
    fees_paid = max(state.get("fees_paid", 0.0), implied_entry_fees)
    gross_pl = total_pl + fees_paid                    # profit before brokerage
    fee_drag_pct = round(fees_paid / gross_pl * 100, 1) if gross_pl > 0 else None
    return {
        "as_of": sig["asof"],
        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(_CACHE["ts"])),
        "account_value": round(account_value, 0),
        "cash": round(state["cash"], 0),
        "deposited": round(deposited, 0),
        "invested": round(invested, 0),
        "exposure_pct": round(invested / account_value * 100, 1) if account_value else 0.0,
        "start_capital": config.CAPITAL,
        "total_pl": round(total_pl, 0),
        "total_pl_pct": round(total_pl / deposited * 100, 1) if deposited else 0.0,
        "n_positions": len(positions),
        "top_n": config.TOP_N,
        "trail_pct": int(config.TRAIL_STOP_PCT * 100),
        "fees_paid": round(fees_paid),
        "fee_drag_pct": fee_drag_pct,
        "brokerage_min": config.COMMISSION_MIN,
        "brokerage_pct": config.COMMISSION_PCT * 100,
        "positions": positions,
        "actions": actions,
        "signal": signal_list,
        "index": index_snapshot(),
        "equity_history": hist,
        "sr": _sr_snapshot(),
    }


def _sr_snapshot():
    """The editable Trailing-S/R paper book: your recorded S/R positions evaluated
    against the live signal (buy/sell). None if the OHLC feed was unavailable."""
    ohlc = _CACHE.get("ohlc")
    if not ohlc:
        return None
    try:
        return sr_book.live_book(ohlc, engine.load_state("sr"))
    except Exception:
        return None


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio — OMXS30 Momentum</title>
<style>
  :root {
    --bg:#0b0f17; --card:#141b2b; --card2:#1b2436; --line:#26324a;
    --text:#e6ebf5; --muted:#8b98b0; --accent:#3b82f6; --pos:#22c55e; --neg:#ef4444;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6fb; --card:#ffffff; --card2:#f0f3f9; --line:#e2e8f0;
            --text:#111827; --muted:#6b7280; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:1000px; margin:0 auto; padding:20px 16px 60px; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:6px; }
  h1 { font-size:20px; margin:0; font-weight:650; }
  .sub { color:var(--muted); font-size:13px; }
  .refresh { margin-left:auto; }
  a.btn { text-decoration:none; background:var(--accent); color:#fff; padding:7px 14px;
    border-radius:8px; font-size:13px; font-weight:600; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px; margin:16px 0 22px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .val { font-size:24px; font-weight:680; margin-top:4px; }
  .pos { color:var(--pos); } .neg { color:var(--neg); }
  h2 { font-size:15px; margin:26px 0 10px; font-weight:640; }
  table { width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  th,td { padding:9px 12px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.03em; font-weight:600; }
  tr:last-child td { border-bottom:none; }
  tbody tr:hover { background:var(--card2); }
  .tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:99px;
    background:var(--card2); color:var(--muted); border:1px solid var(--line); }
  .tag.held { background:rgba(34,197,94,.15); color:var(--pos); border-color:transparent; }
  .tag.buy { background:rgba(59,130,246,.15); color:var(--accent); border-color:transparent; }
  .bar { height:6px; background:var(--card2); border-radius:99px; overflow:hidden; min-width:60px; }
  .bar > span { display:block; height:100%; background:var(--accent); }
  .empty { background:var(--card); border:1px dashed var(--line); border-radius:12px;
    padding:26px; text-align:center; color:var(--muted); }
  .tablescroll { overflow-x:auto; }
  svg { width:100%; height:auto; display:block; }
  .foot { color:var(--muted); font-size:12px; margin-top:26px; }
  code { background:var(--card2); padding:1px 6px; border-radius:5px; font-size:13px; }
  a.btn.ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
  input, select { background:var(--card2); border:1px solid var(--line); color:var(--text);
    padding:8px 10px; border-radius:8px; font-size:15px; min-width:0; }
  input { width:100px; }
  label { display:inline-flex; flex-direction:column; gap:4px; font-size:11px;
    text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }
  button { background:var(--accent); color:#fff; border:none; padding:9px 16px; border-radius:8px;
    font-weight:600; cursor:pointer; font-size:14px; }
  button.danger { background:transparent; color:var(--neg); border:1px solid var(--neg); }
  .editcard { background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:14px 16px; margin-bottom:10px; }
  .editcard .ename { font-weight:640; margin-bottom:10px; }
  .frow { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:flex-end; }
  .frow form { display:flex; flex-wrap:wrap; gap:10px 12px; align-items:flex-end; }
  .hint { color:var(--muted); font-size:12px; margin:2px 0 14px; }
  .actions { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .act { display:flex; align-items:center; gap:10px; padding:11px 14px; border-bottom:1px solid var(--line); }
  .act:last-child { border-bottom:none; }
  .pill { font-size:11px; font-weight:700; padding:2px 9px; border-radius:99px;
    letter-spacing:.03em; white-space:nowrap; }
  .pill.sell { background:rgba(239,68,68,.16); color:var(--neg); }
  .pill.buy { background:rgba(59,130,246,.16); color:var(--accent); }
  .pill.hold { background:var(--card2); color:var(--muted); }
  .summary { color:var(--muted); font-size:13px; margin:2px 0 12px; }
</style>
</head>
<body><div class="wrap">
  <header>
    <h1>Portfolio</h1>
    <span class="sub">OMXS30 momentum · top-{{d.top_n}} · {{d.trail_pct}}% trailing stop · {{d.brokerage_min}} SEK brokerage · week ending {{d.as_of}}</span>
    <span class="refresh">
      <a class="btn ghost" href="/nifty">🇮🇳 Nifty</a>
      <a class="btn ghost" href="/backtest">📊 Backtest</a>
      {% if edit %}<a class="btn" href="/">✓ Done</a>
      {% else %}<a class="btn ghost" href="?edit=1">✎ Edit</a>
      <a class="btn" href="?refresh=1">↻ Refresh</a>{% endif %}
    </span>
  </header>

  <div class="cards">
    <div class="card"><div class="label">Account value</div>
      <div class="val">{{ "{:,.0f}".format(d.account_value) }} <span class="sub">SEK</span></div></div>
    <div class="card"><div class="label">Total P/L <span class="sub">net</span></div>
      <div class="val {{ 'pos' if d.total_pl>=0 else 'neg' }}">
        {{ '+' if d.total_pl>=0 else '' }}{{ "{:,.0f}".format(d.total_pl) }}
        <span class="sub">({{ '+' if d.total_pl_pct>=0 else '' }}{{d.total_pl_pct}}%)</span></div>
      <div class="sub">on {{ "{:,.0f}".format(d.deposited) }} put in</div></div>
    <div class="card"><div class="label">Cash</div>
      <div class="val">{{ "{:,.0f}".format(d.cash) }}</div></div>
    <div class="card"><div class="label">Exposure</div>
      <div class="val">{{d.exposure_pct}}% <span class="sub">{{d.n_positions}}/{{d.top_n}}</span></div></div>
    <div class="card"><div class="label">Brokerage paid</div>
      <div class="val">{{ "{:,.0f}".format(d.fees_paid) }}
        <span class="sub">SEK · {{d.brokerage_min}}/trade</span></div>
      <div class="sub">{% if d.fee_drag_pct is not none %}{{d.fee_drag_pct}}% of gross P/L{% else %}— of gross P/L{% endif %}</div></div>
    {% if d.index %}
    <div class="card"><div class="label">OMXS30 index</div>
      <div class="val">{{ "{:,.0f}".format(d.index.level) }}
        <span class="sub {{ 'pos' if d.index.chg>=0 else 'neg' }}">{{ '+' if d.index.chg>=0 else '' }}{{d.index.chg}}%</span></div>
      <div class="sub">{% if d.index.vs_sma is not none %}<span class="{{ 'pos' if d.index.regime=='uptrend' else 'neg' }}">{{d.index.regime}}</span> · {{ '+' if d.index.vs_sma>=0 else '' }}{{d.index.vs_sma}}% vs 20wk SMA{% else %}{{d.index.regime}}{% endif %}</div></div>
    {% endif %}
  </div>

  <h2>Recommended actions <span class="sub">week ending {{d.as_of}}</span></h2>
  {% if d.actions.sell or d.actions.buy or d.actions.hold %}
  <div class="summary">
    Sell {{d.actions.sell|length}} · Buy {{d.actions.buy|length}} · Hold {{d.actions.hold|length}}
    {% if d.actions.sell or d.actions.buy %}· est. brokerage {{d.actions.fee_total}} SEK{% endif %}
    {% if not d.actions.sell and not d.actions.buy %}— nothing to do, hold everything.{% endif %}
  </div>
  <div class="actions">
    {% for a in d.actions.sell %}
    <div class="act"><span class="pill sell">SELL</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · {{a.reason}} · ~{{a.fee}} SEK fee</span></div>
    {% endfor %}
    {% for a in d.actions.buy %}
    <div class="act"><span class="pill buy">BUY</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · +{{a.mom}}% 12w · ~{{a.fee}} SEK fee</span></div>
    {% endfor %}
    {% for a in d.actions.hold %}
    <div class="act"><span class="pill hold">HOLD</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · {{a.reason}}</span></div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">All cash — buy the <strong>BUY now</strong> names in this week's signal below,
    or tap <strong>✎ Edit</strong> to record holdings you already own.</div>
  {% endif %}

  {% if d.equity_history|length > 1 %}
  <h2>Equity</h2>
  <div class="card">{{ chart|safe }}</div>
  {% endif %}

  <h2>Positions</h2>
  {% if edit %}
  <p class="hint">Enter your actual fills. Adding a position spends cash (shares × price + fee);
    “Save” corrects the recorded shares/price without moving cash; “Sell” books proceeds back to cash.
    Use “Set cash” to reconcile to your broker’s real balance.</p>

  {% for p in d.positions %}
  <div class="editcard">
    <div class="ename"><span class="pill {{p.action|lower}}">{{p.action}}</span>
      {{p.name}} <span class="sub">{{p.ticker}}</span>
      · now {{p.now}}{% if p.day_chg is not none %} · today <span class="{{ 'pos' if p.day_chg>=0 else 'neg' }}">{{ '+' if p.day_chg>=0 else '' }}{{p.day_chg}}%</span>{% endif %}
      · P/L <span class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%</span></div>
    <div class="frow">
      <form method="post" action="/position/update">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Shares<input type="number" name="shares" value="{{p.shares}}" min="1" step="1" inputmode="numeric"></label>
        <label>Entry price<input type="number" name="price" value="{{p.entry}}" min="0" step="0.01" inputmode="decimal"></label>
        <button>Save</button>
      </form>
      <form method="post" action="/position/sell" onsubmit="return confirm('Sell all {{p.shares}} {{p.ticker}}?');">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Sell @<input type="number" name="price" value="{{p.now}}" min="0" step="0.01" inputmode="decimal"></label>
        <button class="danger">Sell</button>
      </form>
    </div>
  </div>
  {% endfor %}

  <div class="editcard">
    <div class="ename">＋ Add position</div>
    <form class="frow" method="post" action="/position/add">
      <label>Stock<select name="ticker">
        {% for t, n in universe %}<option value="{{t}}">{{n}} ({{t}})</option>{% endfor %}
      </select></label>
      <label>Shares<input type="number" name="shares" min="1" step="1" inputmode="numeric" required></label>
      <label>Price<input type="number" name="price" min="0" step="0.01" inputmode="decimal" required></label>
      <button>Add</button>
    </form>
  </div>

  <div class="editcard">
    <div class="ename">Cash <span class="sub">deposit / withdraw</span></div>
    <form class="frow" method="post" action="/cash">
      <label>Balance (SEK)<input type="number" name="cash" value="{{ "%.2f"|format(d.cash) }}" step="0.01" inputmode="decimal"></label>
      <button>Set cash</button>
    </form>
    <p class="hint" style="margin:8px 0 0">Changing cash is treated as a deposit or withdrawal (e.g. your monthly SIP) —
      the P/L baseline moves with it, so contributions don't show up as profit. Currently measured against
      {{ "{:,.0f}".format(d.deposited) }} SEK put in.</p>
  </div>

  {% elif d.positions %}
  <div class="tablescroll"><table>
    <thead><tr>
      <th>Stock</th><th>Shares</th><th>Entry</th><th>Now</th><th>Today</th><th>Peak</th>
      <th>P/L</th><th>Value</th><th>Weight</th><th>Stop @</th><th>To stop</th>
    </tr></thead><tbody>
    {% for p in d.positions %}
    <tr>
      <td><strong>{{p.name}}</strong> <span class="sub">{{p.ticker}}</span><br>
        <span class="pill {{p.action|lower}}">{{p.action}}</span></td>
      <td>{{p.shares}}</td><td>{{p.entry}}</td><td>{{p.now}}</td>
      <td class="{{ 'pos' if p.day_chg and p.day_chg>=0 else 'neg' }}">
        {% if p.day_chg is not none %}{{ '+' if p.day_chg>=0 else '' }}{{p.day_chg}}%<br>
        <span class="sub">{{ '+' if p.day_sek>=0 else '' }}{{ "{:,.0f}".format(p.day_sek) }}</span>{% else %}—{% endif %}</td>
      <td>{{p.peak}}</td>
      <td class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%<br>
        <span class="sub">{{ '+' if p.pl_sek>=0 else '' }}{{ "{:,.0f}".format(p.pl_sek) }}</span></td>
      <td>{{ "{:,.0f}".format(p.value) }}</td>
      <td><div class="bar"><span style="width:{{p.weight}}%"></span></div><span class="sub">{{p.weight}}%</span></td>
      <td>{{p.stop_price}}</td>
      <td class="{{ 'neg' if p.stop_dist_pct is not none and p.stop_dist_pct < 5 else '' }}">
        {{p.stop_dist_pct}}%</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">No open positions — the book is all cash.<br>
    Tap <strong>✎ Edit</strong> to add your actual holdings, or run
    <code>python run.py rebalance</code> to open the top-{{d.top_n}} from this week's signal.</div>
  {% endif %}

  <h2>This week's signal</h2>
  <div class="tablescroll"><table>
    <thead><tr><th>#</th><th>Stock</th><th>12w momentum</th><th>Action</th><th></th></tr></thead><tbody>
    {% for s in d.signal %}
    <tr>
      <td>{{s.rank}}</td>
      <td><strong>{{s.name}}</strong> <span class="sub">{{s.ticker}}</span></td>
      <td class="{{ 'pos' if s.mom>=0 else 'neg' }}">{{ '+' if s.mom>=0 else '' }}{{s.mom}}%</td>
      <td><span class="tag {{ 'buy' if s.rank<=d.top_n else '' }}">{{s.action}}</span></td>
      <td>{% if s.held %}<span class="tag held">● held</span>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>

  {% if d.sr %}
  <h2 style="margin-top:34px;border-top:1px solid var(--line);padding-top:22px">
    Trailing S/R book <span class="sub">your S/R portfolio · buy the dip, 20% trailing stop</span></h2>
  <p class="sub" style="margin:-4px 0 12px">
    A separate book for the trailing support/resistance strategy
    (<code>backtest_sr_trailing.py</code>). Record your actual S/R fills below (tap
    <strong>✎ Edit</strong>); the dashboard flags <strong>SELL</strong> when a holding hits its
    {{d.sr.trail_pct}}% trailing stop and <strong>BUY</strong> when a name is at support in an uptrend.</p>

  <div class="cards">
    <div class="card"><div class="label">S/R value</div>
      <div class="val">{{ "{:,.0f}".format(d.sr.account_value) }} <span class="sub">SEK</span></div></div>
    <div class="card"><div class="label">S/R P/L <span class="sub">net</span></div>
      <div class="val {{ 'pos' if d.sr.total_pl>=0 else 'neg' }}">
        {{ '+' if d.sr.total_pl>=0 else '' }}{{ "{:,.0f}".format(d.sr.total_pl) }}
        <span class="sub">({{ '+' if d.sr.total_pl_pct>=0 else '' }}{{d.sr.total_pl_pct}}%)</span></div>
      <div class="sub">on {{ "{:,.0f}".format(d.sr.deposited) }} put in · after fees</div></div>
    <div class="card"><div class="label">S/R cash</div>
      <div class="val">{{ "{:,.0f}".format(d.sr.cash) }}</div></div>
    <div class="card"><div class="label">S/R exposure</div>
      <div class="val">{{d.sr.exposure_pct}}% <span class="sub">{{d.sr.n_positions}}/{{d.sr.max_pos}}</span></div></div>
  </div>

  <div class="summary">Sell {{d.sr.sell_signals|length}} · Buy {{d.sr.buy_signals|length}}
    {% if not d.sr.sell_signals and not d.sr.buy_signals %}— nothing to do.{% endif %}
    <br><span class="sub">BUY confirmed at the Friday close of {{d.sr.signal_week}} (stable all week — buy Monday).
    SELL checked live ({{d.sr.as_of}}).</span></div>
  {% if d.sr.sell_signals or d.sr.buy_signals %}
  <div class="actions">
    {% for a in d.sr.sell_signals %}
    <div class="act"><span class="pill sell">SELL</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · {{a.reason}} · {{a.shares}} sh @ ~{{a.price}}</span></div>
    {% endfor %}
    {% for a in d.sr.buy_signals %}
    <div class="act"><span class="pill buy">BUY</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · <strong>~{{ "{:,.0f}".format(a.alloc_sek) }} SEK (~{{a.sugg_shares}} sh)</strong>
      · now {{a.now}} (<span class="{{ 'neg' if a.stale else '' }}">{{ '+' if a.moved_pct>=0 else '' }}{{a.moved_pct}}% since signal</span>){% if a.stale %} ⚠ ran away{% endif %}</span></div>
    {% endfor %}
  </div>
  {% endif %}

  <h2>S/R holdings</h2>
  {% if edit %}
  <p class="hint">Record your real S/R fills. The stop level and peak are computed from
    live prices — you only enter shares, price, and (optionally) the buy date.</p>
  {% for p in d.sr.positions %}
  <div class="editcard">
    <div class="ename"><span class="pill {{p.action|lower}}">{{p.action}}</span>
      {{p.name}} <span class="sub">{{p.ticker}} · now {{p.now}} · stop {{p.stop_price}}
      · P/L</span> <span class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%</span></div>
    <div class="frow">
      <form method="post" action="/sr/position/update">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Shares<input type="number" name="shares" value="{{p.shares}}" min="1" step="1" inputmode="numeric"></label>
        <label>Entry price<input type="number" name="price" value="{{p.entry}}" min="0" step="0.01" inputmode="decimal"></label>
        <label>Buy date<input type="date" name="since" value="{{p.since}}"></label>
        <button>Save</button>
      </form>
      <form method="post" action="/sr/position/sell" onsubmit="return confirm('Sell all {{p.shares}} {{p.ticker}} from the S/R book?');">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Sell @<input type="number" name="price" value="{{p.now}}" min="0" step="0.01" inputmode="decimal"></label>
        <button class="danger">Sell</button>
      </form>
    </div>
  </div>
  {% endfor %}
  <div class="editcard">
    <div class="ename">＋ Add S/R position</div>
    <form class="frow" method="post" action="/sr/position/add">
      <label>Stock<select name="ticker">
        {% for t, n in universe %}<option value="{{t}}">{{n}} ({{t}})</option>{% endfor %}
      </select></label>
      <label>Shares<input type="number" name="shares" min="1" step="1" inputmode="numeric" required></label>
      <label>Price<input type="number" name="price" min="0" step="0.01" inputmode="decimal" required></label>
      <label>Buy date<input type="date" name="since"></label>
      <button>Add</button>
    </form>
  </div>
  <div class="editcard">
    <div class="ename">S/R cash <span class="sub">deposit / withdraw</span></div>
    <form class="frow" method="post" action="/sr/cash">
      <label>Balance (SEK)<input type="number" name="cash" value="{{ "%.2f"|format(d.sr.cash) }}" step="0.01" inputmode="decimal"></label>
      <button>Set cash</button>
    </form>
    <p class="hint" style="margin:8px 0 0">Changing cash is treated as a deposit or withdrawal (e.g. your monthly SIP) —
      the P/L baseline moves with it, so contributions don't show up as profit. Currently measured against
      {{ "{:,.0f}".format(d.sr.deposited) }} SEK put in.</p>
  </div>
  {% elif d.sr.positions %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Shares</th><th>Entry</th><th>Now</th><th>P/L</th><th>Peak</th><th>Stop @</th><th>To stop</th><th>Value</th></tr></thead><tbody>
    {% for p in d.sr.positions %}
    <tr>
      <td><strong>{{p.name}}</strong> <span class="sub">{{p.ticker}}</span><br>
        <span class="pill {{p.action|lower}}">{{p.action}}</span></td>
      <td>{{p.shares}}</td><td>{{p.entry}}</td><td>{{p.now}}</td>
      <td class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%<br>
        <span class="sub">{{ '+' if p.pl_sek>=0 else '' }}{{ "{:,.0f}".format(p.pl_sek) }}</span></td>
      <td>{{p.peak}}</td>
      <td>{{p.stop_price}}</td>
      <td class="{{ 'neg' if p.stop_dist_pct is not none and p.stop_dist_pct < 5 else '' }}">
        {% if p.stop_dist_pct is not none %}{{p.stop_dist_pct}}%{% else %}—{% endif %}</td>
      <td>{{ "{:,.0f}".format(p.value) }}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">No S/R positions yet.<br>
    Tap <strong>✎ Edit</strong> to record what you've bought for the S/R strategy —
    the <strong>BUY</strong> signals above are where it would start.</div>
  {% endif %}

  <h2>S/R watchlist <span class="sub">confirmed at Friday close {{d.sr.signal_week}} · buy Monday</span></h2>
  {% if d.sr.watch %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Support</th><th>Low vs sup</th><th>Signal close</th><th>Now</th><th>Since signal</th><th>Buy ~</th><th></th></tr></thead><tbody>
    {% for s in d.sr.watch %}
    <tr>
      <td><strong>{{s.name}}</strong> <span class="sub">{{s.ticker}}</span></td>
      <td>{{s.support}}</td>
      <td class="{{ 'pos' if s.low_vs_sup>=0 else 'neg' }}">{{ '+' if s.low_vs_sup>=0 else '' }}{{s.low_vs_sup}}%</td>
      <td>{{s.price}}</td>
      <td>{{s.now}}</td>
      <td class="{{ 'neg' if s.stale else '' }}">{{ '+' if s.moved_pct>=0 else '' }}{{s.moved_pct}}%{% if s.stale %} ⚠{% endif %}</td>
      <td>{{s.sugg_shares}} sh<br><span class="sub">{{ "{:,.0f}".format(s.alloc_sek) }} SEK</span></td>
      <td>{% if loop.index0 < d.sr.free_slots %}<span class="tag buy">buy now</span>{% else %}<span class="tag">full</span>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  <p class="sub" style="margin-top:8px">“Buy ~” is 1/6 of your current S/R equity ({{ "{:,.0f}".format(d.sr.account_value) }} SEK ÷ {{d.sr.max_pos}}),
    at the live price — this reinvests profit (compounding), matching the backtest. Signal is the confirmed Friday close;
    a big “Since signal” move (⚠ over 4%) means the price ran away, so skip it. {{d.sr.free_slots}} free slot(s) of {{d.sr.max_pos}}.</p>
  {% else %}
  <p class="sub">Nothing dipped to support and bounced this week — no buys.</p>
  {% endif %}

  <h2>Watch this week <span class="sub">approaching support · a dip could trigger a Friday buy</span></h2>
  {% if d.sr.monitor %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Price</th><th>Support</th><th>Above support</th><th>Dip-to-buy</th><th></th></tr></thead><tbody>
    {% for s in d.sr.monitor %}
    <tr>
      <td><strong>{{s.name}}</strong> <span class="sub">{{s.ticker}}</span></td>
      <td>{{s.price}}</td>
      <td>{{s.support}}</td>
      <td class="{{ 'pos' if s.pct_above<=8 else '' }}">+{{s.pct_above}}%</td>
      <td>{{s.dip_to_buy}}</td>
      <td>{% if s.at_support %}<span class="tag buy">at support ⚠ watch Fri close</span>{% elif s.pct_above<=8 %}<span class="tag">near</span>{% else %}<span class="tag">far</span>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  <p class="sub" style="margin-top:8px">Names in an uptrend hovering near support. A stock <strong>buys</strong> only if its
    low dips to the “Dip-to-buy” level <em>and</em> it closes the week up (bullish) — confirmed at Friday's close.
    “At support” names are at the decision point now; watch how they close Friday. Not yet buys.</p>
  {% else %}
  <p class="sub">Nothing near support in an uptrend right now — nothing to watch.</p>
  {% endif %}
  {% endif %}

  <div class="foot">Prices updated {{d.updated}} · auto-refreshes every 5 min during
    market hours (Mon–Fri 09:00–17:30 CET) · read-only dashboard, places no orders ·
    start capital {{ "{:,.0f}".format(d.start_capital) }} SEK</div>
</div>
<script>
  // Auto-refresh every 5 min during Stockholm market hours (Mon-Fri 09:00-17:30),
  // skipped while editing. Outside hours it re-checks client-side but does NOT
  // reload, so it never churns the server overnight or on weekends. Stockholm
  // time is computed explicitly, so it's correct wherever the page is opened.
  (function () {
    if (location.search.includes("edit=1")) return;
    function marketOpen() {
      var p = new Intl.DateTimeFormat("en-US", {
        timeZone: "Europe/Stockholm", weekday: "short",
        hour: "2-digit", minute: "2-digit", hour12: false
      }).formatToParts(new Date());
      var g = function (t) { return p.find(function (x) { return x.type === t; }).value; };
      var wd = g("weekday");
      var hh = parseInt(g("hour"), 10); if (hh === 24) hh = 0;
      var mins = hh * 60 + parseInt(g("minute"), 10);
      var weekday = wd !== "Sat" && wd !== "Sun";
      return weekday && mins >= 540 && mins < 1050;   // 09:00 .. 17:30
    }
    function tick() {
      if (marketOpen()) { location.reload(); }        // in-hours: pull fresh data
      else { setTimeout(tick, 300000); }              // off-hours: just re-check, no reload
    }
    setTimeout(tick, 300000);
  })();
</script>
</body></html>
"""


BACKTEST_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest — S/R + SIP</title>
<style>
  :root { --bg:#0b0f17; --card:#141b2b; --card2:#1b2436; --line:#26324a;
    --text:#e6ebf5; --muted:#8b98b0; --accent:#3b82f6; --pos:#22c55e; --neg:#ef4444; --warn:#f59e0b; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6fb; --card:#fff; --card2:#f0f3f9; --line:#e2e8f0; --text:#111827; --muted:#6b7280; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:1000px; margin:0 auto; padding:20px 16px 60px; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:6px; }
  h1 { font-size:20px; margin:0; font-weight:650; }
  h2 { font-size:15px; margin:26px 0 10px; font-weight:640; }
  .sub { color:var(--muted); font-size:13px; }
  .back { margin-left:auto; text-decoration:none; background:transparent; color:var(--accent);
    border:1px solid var(--accent); padding:7px 14px; border-radius:8px; font-size:13px; font-weight:600; }
  .warn { background:rgba(245,158,11,.12); border:1px solid var(--warn); color:var(--text);
    border-radius:12px; padding:12px 16px; margin:14px 0; font-size:13.5px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:16px 0 4px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .val { font-size:22px; font-weight:680; margin-top:4px; }
  .pos { color:var(--pos); } .neg { color:var(--neg); }
  table { width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  th,td { padding:8px 12px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.03em; font-weight:600; }
  tr:last-child td { border-bottom:none; }
  tbody tr:hover { background:var(--card2); }
  .tablescroll { overflow-x:auto; }
  .scrollbox { max-height:520px; overflow-y:auto; border:1px solid var(--line); border-radius:12px; }
  .scrollbox table { border:none; }
  .pill { font-size:11px; font-weight:700; padding:1px 7px; border-radius:99px; }
  .pill.buy { background:rgba(59,130,246,.16); color:var(--accent); }
  .pill.sell { background:rgba(239,68,68,.16); color:var(--neg); }
  .foot { color:var(--muted); font-size:12px; margin-top:26px; }
  code { background:var(--card2); padding:1px 6px; border-radius:5px; font-size:13px; }
</style>
</head>
<body><div class="wrap">
  <header>
    <h1>Strategy backtest</h1>
    <span class="sub">Trailing S/R · OMXS30 · 100,000 base + 25,000/mo SIP · {{b.start}} → {{b.end}}</span>
    <a class="back" href="/">← Dashboard</a>
  </header>

  <div class="warn">
    ⚠ <strong>This is a backtest on today's OMXS30 survivors — it is optimistic (survivorship bias).</strong>
    The realistic, survivorship-corrected return is about <strong>~10% CAGR</strong>. At ~10%, the same
    {{ "{:,.0f}".format(b.deposited) }} SEK of contributions would grow to roughly <strong>~11–12M</strong>,
    not the {{ "{:,.0f}".format(b.final) }} shown below. Use ~10% for planning; treat the figures here as the
    upside end, and the trade sequence as representative of how the strategy behaves.
  </div>

  <div class="cards">
    <div class="card"><div class="label">Total contributed</div>
      <div class="val">{{ "{:,.0f}".format(b.deposited) }} <span class="sub">SEK</span></div>
      <div class="sub">100k + 25k × {{b.n_sip}} months</div></div>
    <div class="card"><div class="label">Final value <span class="sub">backtest</span></div>
      <div class="val">{{ "{:,.0f}".format(b.final) }}</div></div>
    <div class="card"><div class="label">Profit <span class="sub">backtest</span></div>
      <div class="val {{ 'pos' if b.profit>=0 else 'neg' }}">{{ '+' if b.profit>=0 else '' }}{{ "{:,.0f}".format(b.profit) }}</div></div>
    <div class="card"><div class="label">Trades</div>
      <div class="val">{{b.n_buys + b.n_sells}}</div>
      <div class="sub">{{b.n_buys}} buys · {{b.n_sells}} sells</div></div>
  </div>

  <h2>Profit by year <span class="sub">investment gain, contributions netted out</span></h2>
  <div class="tablescroll"><table>
    <thead><tr><th>Year</th><th>Paid in</th><th>Year-end value</th><th>Profit that year</th></tr></thead><tbody>
    {% for y in b.yearly %}
    <tr><td>{{y.year}}</td><td>{{ "{:,.0f}".format(y.paid) }}</td><td>{{ "{:,.0f}".format(y.value) }}</td>
      <td class="{{ 'pos' if y.profit>=0 else 'neg' }}">{{ '+' if y.profit>=0 else '' }}{{ "{:,.0f}".format(y.profit) }}</td></tr>
    {% endfor %}
    </tbody></table></div>

  <h2>Every trade <span class="sub">{{b.trades|length}} buys &amp; sells</span></h2>
  <div class="scrollbox"><table>
    <thead><tr><th>Date</th><th>Action</th><th>Stock</th><th>Shares</th><th>Price</th><th>Value</th><th>Reason</th></tr></thead><tbody>
    {% for t in b.trades %}
    <tr>
      <td>{{t.date}}</td>
      <td><span class="pill {{t.action|lower}}">{{t.action}}</span></td>
      <td>{{t.name}}</td><td>{{t.shares}}</td><td>{{t.price}}</td>
      <td>{{ "{:,.0f}".format(t.value) }}</td>
      <td style="text-align:left">{{t.reason}}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>

  <div class="foot">Strategy: buy a weekly dip to 12-week support in an uptrend (above the 40-week SMA, up week);
    exit on a 20% trailing stop or a 5% break below entry support; up to 6 equal-weight positions; profit reinvested.
    Backtest on Yahoo adjusted prices, Nordnet fees (0.15%, min 39 SEK). Survivorship-flattered — see the note above.</div>
</div>
</body></html>
"""


NIFTY_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nifty 50 — Trailing S/R</title>
<style>
  :root { --bg:#0b0f17; --card:#141b2b; --card2:#1b2436; --line:#26324a;
    --text:#e6ebf5; --muted:#8b98b0; --accent:#f59e0b; --blue:#3b82f6; --pos:#22c55e; --neg:#ef4444; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6fb; --card:#fff; --card2:#f0f3f9; --line:#e2e8f0; --text:#111827; --muted:#6b7280; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:1000px; margin:0 auto; padding:20px 16px 60px; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:6px; }
  h1 { font-size:20px; margin:0; font-weight:650; }
  h2 { font-size:15px; margin:26px 0 10px; font-weight:640; }
  .sub { color:var(--muted); font-size:13px; }
  .refresh { margin-left:auto; display:flex; gap:8px; }
  a.btn { text-decoration:none; background:var(--accent); color:#111; padding:7px 14px;
    border-radius:8px; font-size:13px; font-weight:600; }
  a.btn.ghost { background:transparent; color:var(--accent); border:1px solid var(--accent); }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:16px 0 22px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .card .val { font-size:22px; font-weight:680; margin-top:4px; }
  .pos { color:var(--pos); } .neg { color:var(--neg); }
  table { width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  th,td { padding:9px 12px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.03em; font-weight:600; }
  tr:last-child td { border-bottom:none; }
  tbody tr:hover { background:var(--card2); }
  .tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:99px;
    background:var(--card2); color:var(--muted); border:1px solid var(--line); }
  .tag.buy { background:rgba(245,158,11,.16); color:var(--accent); border-color:transparent; }
  .tablescroll { overflow-x:auto; }
  .empty { background:var(--card); border:1px dashed var(--line); border-radius:12px;
    padding:26px; text-align:center; color:var(--muted); }
  input, select { background:var(--card2); border:1px solid var(--line); color:var(--text);
    padding:8px 10px; border-radius:8px; font-size:15px; min-width:0; }
  input { width:110px; }
  label { display:inline-flex; flex-direction:column; gap:4px; font-size:11px;
    text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }
  button { background:var(--accent); color:#111; border:none; padding:9px 16px; border-radius:8px;
    font-weight:600; cursor:pointer; font-size:14px; }
  button.danger { background:transparent; color:var(--neg); border:1px solid var(--neg); }
  .editcard { background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:14px 16px; margin-bottom:10px; }
  .editcard .ename { font-weight:640; margin-bottom:10px; }
  .frow { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:flex-end; }
  .frow form { display:flex; flex-wrap:wrap; gap:10px 12px; align-items:flex-end; }
  .hint { color:var(--muted); font-size:12px; margin:2px 0 14px; }
  .actions { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .act { display:flex; align-items:center; gap:10px; padding:11px 14px; border-bottom:1px solid var(--line); }
  .act:last-child { border-bottom:none; }
  .pill { font-size:11px; font-weight:700; padding:2px 9px; border-radius:99px; white-space:nowrap; }
  .pill.sell { background:rgba(239,68,68,.16); color:var(--neg); }
  .pill.buy { background:rgba(245,158,11,.16); color:var(--accent); }
  .pill.hold { background:var(--card2); color:var(--muted); }
  .summary { color:var(--muted); font-size:13px; margin:2px 0 12px; }
  .foot { color:var(--muted); font-size:12px; margin-top:26px; }
</style>
</head>
<body><div class="wrap">
  <header>
    <h1>Nifty 50 <span class="sub">Trailing S/R · India</span></h1>
    <span class="sub">buy the dip · {{n.trail_pct}}% trailing stop · ₹ INR</span>
    <span class="refresh">
      <a class="btn ghost" href="/">← Dashboard</a>
      {% if edit %}<a class="btn" href="/nifty">✓ Done</a>
      {% else %}<a class="btn ghost" href="/nifty?edit=1">✎ Edit</a>
      <a class="btn" href="/nifty?refresh=1">↻ Refresh</a>{% endif %}
    </span>
  </header>

  {% if not n %}
  <div class="empty">Could not load Nifty prices right now — try ↻ Refresh in a minute.</div>
  {% else %}
  <div class="cards">
    <div class="card"><div class="label">Book value</div>
      <div class="val">₹{{ "{:,.0f}".format(n.account_value) }}</div></div>
    <div class="card"><div class="label">P/L <span class="sub">net</span></div>
      <div class="val {{ 'pos' if n.total_pl>=0 else 'neg' }}">
        {{ '+' if n.total_pl>=0 else '' }}{{ "{:,.0f}".format(n.total_pl) }}
        <span class="sub">({{ '+' if n.total_pl_pct>=0 else '' }}{{n.total_pl_pct}}%)</span></div>
      <div class="sub">on ₹{{ "{:,.0f}".format(n.deposited) }} put in</div></div>
    <div class="card"><div class="label">Cash</div>
      <div class="val">₹{{ "{:,.0f}".format(n.cash) }}</div></div>
    <div class="card"><div class="label">Exposure</div>
      <div class="val">{{n.exposure_pct}}% <span class="sub">{{n.n_positions}}/{{n.max_pos}}</span></div></div>
  </div>

  <div class="summary">Sell {{n.sell_signals|length}} · Buy {{n.buy_signals|length}}
    {% if not n.sell_signals and not n.buy_signals %}— nothing to do.{% endif %}
    <br><span class="sub">BUY confirmed at the Friday close of {{n.signal_week}} (stable all week — buy Monday).
    SELL checked live ({{n.as_of}}). NSE trades via your Indian brokerage.</span></div>
  {% if n.sell_signals or n.buy_signals %}
  <div class="actions">
    {% for a in n.sell_signals %}
    <div class="act"><span class="pill sell">SELL</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · {{a.reason}} · {{a.shares}} sh @ ~{{a.price}}</span></div>
    {% endfor %}
    {% for a in n.buy_signals %}
    <div class="act"><span class="pill buy">BUY</span>
      <strong>{{a.name}}</strong> <span class="sub">{{a.ticker}} · <strong>~₹{{ "{:,.0f}".format(a.alloc_sek) }} (~{{a.sugg_shares}} sh)</strong>
      · now {{a.now}} ({{ '+' if a.moved_pct>=0 else '' }}{{a.moved_pct}}% since signal){% if a.stale %} ⚠ ran away{% endif %}</span></div>
    {% endfor %}
  </div>
  {% endif %}

  <h2>Holdings</h2>
  {% if edit %}
  <p class="hint">Record your real NSE fills (₹). Stops and peaks are computed from live prices.</p>
  {% for p in n.positions %}
  <div class="editcard">
    <div class="ename"><span class="pill {{p.action|lower}}">{{p.action}}</span>
      {{p.name}} <span class="sub">{{p.ticker}} · now {{p.now}} · stop {{p.stop_price}} · P/L</span>
      <span class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%</span></div>
    <div class="frow">
      <form method="post" action="/nifty/position/update">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Shares<input type="number" name="shares" value="{{p.shares}}" min="1" step="1" inputmode="numeric"></label>
        <label>Entry price<input type="number" name="price" value="{{p.entry}}" min="0" step="0.01" inputmode="decimal"></label>
        <label>Buy date<input type="date" name="since" value="{{p.since}}"></label>
        <button>Save</button>
      </form>
      <form method="post" action="/nifty/position/sell" onsubmit="return confirm('Sell all {{p.shares}} {{p.ticker}}?');">
        <input type="hidden" name="ticker" value="{{p.ticker}}">
        <label>Sell @<input type="number" name="price" value="{{p.now}}" min="0" step="0.01" inputmode="decimal"></label>
        <button class="danger">Sell</button>
      </form>
    </div>
  </div>
  {% endfor %}
  <div class="editcard">
    <div class="ename">＋ Add position</div>
    <form class="frow" method="post" action="/nifty/position/add">
      <label>Stock<select name="ticker">
        {% for t, nm in universe %}<option value="{{t}}">{{nm}} ({{t}})</option>{% endfor %}
      </select></label>
      <label>Shares<input type="number" name="shares" min="1" step="1" inputmode="numeric" required></label>
      <label>Price<input type="number" name="price" min="0" step="0.01" inputmode="decimal" required></label>
      <label>Buy date<input type="date" name="since"></label>
      <button>Add</button>
    </form>
  </div>
  <div class="editcard">
    <div class="ename">Cash <span class="sub">deposit / withdraw</span></div>
    <form class="frow" method="post" action="/nifty/cash">
      <label>Balance (₹)<input type="number" name="cash" value="{{ "%.2f"|format(n.cash) }}" step="0.01" inputmode="decimal"></label>
      <button>Set cash</button>
    </form>
    <p class="hint" style="margin:8px 0 0">Cash changes are treated as deposits/withdrawals — the P/L baseline moves with
      them. Currently measured against ₹{{ "{:,.0f}".format(n.deposited) }} put in.</p>
  </div>
  {% elif n.positions %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Shares</th><th>Entry</th><th>Now</th><th>P/L</th><th>Peak</th><th>Stop @</th><th>To stop</th><th>Value ₹</th></tr></thead><tbody>
    {% for p in n.positions %}
    <tr>
      <td><strong>{{p.name}}</strong> <span class="sub">{{p.ticker}}</span><br>
        <span class="pill {{p.action|lower}}">{{p.action}}</span></td>
      <td>{{p.shares}}</td><td>{{p.entry}}</td><td>{{p.now}}</td>
      <td class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%<br>
        <span class="sub">{{ '+' if p.pl_sek>=0 else '' }}{{ "{:,.0f}".format(p.pl_sek) }}</span></td>
      <td>{{p.peak}}</td>
      <td>{{p.stop_price}}</td>
      <td class="{{ 'neg' if p.stop_dist_pct is not none and p.stop_dist_pct < 5 else '' }}">
        {% if p.stop_dist_pct is not none %}{{p.stop_dist_pct}}%{% else %}—{% endif %}</td>
      <td>{{ "{:,.0f}".format(p.value) }}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <div class="empty">No Nifty positions yet.<br>
    Tap <strong>✎ Edit</strong> to record fills, or start from the BUY signals above.
    Paper-trade first — same rules as the Swedish book.</div>
  {% endif %}

  <h2>Watchlist <span class="sub">confirmed at Friday close {{n.signal_week}} · buy Monday</span></h2>
  {% if n.watch %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Support</th><th>Low vs sup</th><th>Signal close</th><th>Now</th><th>Since signal</th><th>Buy ~</th><th></th></tr></thead><tbody>
    {% for s in n.watch %}
    <tr>
      <td><strong>{{s.name}}</strong> <span class="sub">{{s.ticker}}</span></td>
      <td>{{s.support}}</td>
      <td class="{{ 'pos' if s.low_vs_sup>=0 else 'neg' }}">{{ '+' if s.low_vs_sup>=0 else '' }}{{s.low_vs_sup}}%</td>
      <td>{{s.price}}</td>
      <td>{{s.now}}</td>
      <td class="{{ 'neg' if s.stale else '' }}">{{ '+' if s.moved_pct>=0 else '' }}{{s.moved_pct}}%{% if s.stale %} ⚠{% endif %}</td>
      <td>{{s.sugg_shares}} sh<br><span class="sub">₹{{ "{:,.0f}".format(s.alloc_sek) }}</span></td>
      <td>{% if loop.index0 < n.free_slots %}<span class="tag buy">buy now</span>{% else %}<span class="tag">full</span>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <p class="sub">Nothing dipped to support and bounced this week — no buys.</p>
  {% endif %}

  <h2>Watch this week <span class="sub">approaching support</span></h2>
  {% if n.monitor %}
  <div class="tablescroll"><table>
    <thead><tr><th>Stock</th><th>Price</th><th>Support</th><th>Above support</th><th>Dip-to-buy</th><th></th></tr></thead><tbody>
    {% for s in n.monitor %}
    <tr>
      <td><strong>{{s.name}}</strong> <span class="sub">{{s.ticker}}</span></td>
      <td>{{s.price}}</td>
      <td>{{s.support}}</td>
      <td class="{{ 'pos' if s.pct_above<=8 else '' }}">+{{s.pct_above}}%</td>
      <td>{{s.dip_to_buy}}</td>
      <td>{% if s.at_support %}<span class="tag buy">at support ⚠ watch Fri close</span>{% elif s.pct_above<=8 %}<span class="tag">near</span>{% else %}<span class="tag">far</span>{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody></table></div>
  {% else %}
  <p class="sub">Nothing near support in an uptrend right now.</p>
  {% endif %}
  {% endif %}

  <div class="foot">Same strategy as the Swedish S/R book, applied to Nifty 50 (NSE, ₹).
    Backtested 2010–2026: strong drawdown control (−22% vs index −38%) and low correlation with the
    Swedish book — but survivorship-flattered like all backtests; plan on ~10%/yr. Paper book —
    places no orders; fees modelled at 0.12%/side delivery.</div>
</div>
</body></html>
"""


def _sparkline(hist, w=920, h=120, pad=8, color=None):
    """Minimal inline-SVG equity line (no external deps). `color` overrides the
    default up/down green/red (used to tint the S/R book's curve differently)."""
    vals = [p["value"] for p in hist]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    def x(i): return pad + i * (w - 2 * pad) / (n - 1)
    def y(v): return pad + (h - 2 * pad) * (1 - (v - lo) / rng)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{pad},{h-pad} " + pts + f" {w-pad},{h-pad}"
    up = vals[-1] >= vals[0]
    col = color or ("#22c55e" if up else "#ef4444")
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        f'aria-label="equity curve">'
        f'<polygon points="{area}" fill="{col}" opacity="0.10"/>'
        f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" '
        f'stroke-linejoin="round"/></svg>'
    )


@app.route("/")
def index():
    d = build_snapshot(force=request.args.get("refresh") == "1")
    chart = _sparkline(d["equity_history"]) if len(d["equity_history"]) > 1 else ""
    universe = sorted(((t, strategy.NAMES.get(t, t)) for t in config.TICKERS),
                      key=lambda x: x[1])
    return render_template_string(PAGE, d=d, chart=chart,
                                  edit=request.args.get("edit") == "1", universe=universe)


def _nifty_snapshot(force=False):
    """The Nifty 50 S/R paper book, on its own lazily-fetched OHLC cache."""
    if force or _NIFTY_CACHE["ohlc"] is None or (time.time() - _NIFTY_CACHE["ts"]) > _CACHE_TTL:
        try:
            ohlc = strategy.fetch_ohlc(config.NIFTY_TICKERS, years=config.SR_HISTORY_YEARS)
            if ohlc is not None and not ohlc["close"].empty:
                _NIFTY_CACHE["ohlc"] = ohlc
                _NIFTY_CACHE["ts"] = time.time()
        except Exception:
            pass
    if not _NIFTY_CACHE["ohlc"]:
        return None
    try:
        return sr_book.live_book(_NIFTY_CACHE["ohlc"], engine.load_state("nifty"),
                                 capital=config.NIFTY_CAPITAL)
    except Exception:
        return None


BACKTEST_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_trades_sip.csv")


def _backtest_summary():
    """Read the committed SIP trade log and derive summary + yearly profit.
    Static snapshot (a backtest doesn't change day to day) so the page is fast."""
    df = pd.read_csv(BACKTEST_CSV)
    df["date"] = pd.to_datetime(df["date"])
    base = 100_000
    n_sip = int((df["action"] == "SIP").sum())
    deposited = base + n_sip * 25_000
    final = float(df["portfolio_value"].iloc[-1])
    # running contributions -> yearly investment profit (value minus paid-in)
    df["paid"] = base + (df["action"] == "SIP").cumsum() * 25_000
    df["ahead"] = df["portfolio_value"] - df["paid"]
    ye = df.set_index("date").resample("YE").last()
    yearly, prev = [], 0.0
    for d, row in ye.iterrows():
        yearly.append({"year": d.year, "value": round(row["portfolio_value"]),
                       "paid": round(row["paid"]), "profit": round(row["ahead"] - prev)})
        prev = row["ahead"]
    trades = df[df["action"].isin(["BUY", "SELL"])].copy()
    trade_rows = [{"date": r["date"].date().isoformat(), "action": r["action"], "name": r["name"],
                   "shares": int(r["shares"]) if pd.notna(r["shares"]) else "",
                   "price": r["price"], "value": r["value"], "reason": r["reason"]}
                  for _, r in trades.iterrows()]
    return {
        "start": df["date"].iloc[0].date().isoformat(), "end": df["date"].iloc[-1].date().isoformat(),
        "deposited": deposited, "final": round(final), "profit": round(final - deposited),
        "n_buys": int((df["action"] == "BUY").sum()), "n_sells": int((df["action"] == "SELL").sum()),
        "n_sip": n_sip, "yearly": yearly, "trades": trade_rows,
    }


@app.route("/backtest")
def backtest_page():
    try:
        b = _backtest_summary()
    except Exception as e:
        return f"Backtest data unavailable: {e}", 500
    return render_template_string(BACKTEST_PAGE, b=b)


@app.route("/nifty")
def nifty_page():
    n = _nifty_snapshot(force=request.args.get("refresh") == "1")
    universe = sorted(((t, strategy.NAMES.get(t, t)) for t in config.NIFTY_TICKERS),
                      key=lambda x: x[1])
    return render_template_string(NIFTY_PAGE, n=n,
                                  edit=request.args.get("edit") == "1", universe=universe)


# ---- editing the Nifty book (book="nifty", Indian delivery fees) ----

@app.route("/nifty/position/add", methods=["POST"])
def nifty_position_add():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/nifty?edit=1")
    since = request.form.get("since") or date.today().isoformat()
    if tkr and shares > 0 and price > 0:
        state = engine.load_state("nifty")
        engine.apply_fill(state, engine.Order("BUY", tkr, shares, price, "MANUAL"),
                          commission_pct=config.NIFTY_COMMISSION_PCT,
                          commission_min=config.NIFTY_COMMISSION_MIN)
        state["positions"][tkr]["since"] = since
        engine.save_state(state, "nifty")
    return redirect("/nifty?edit=1")


@app.route("/nifty/position/update", methods=["POST"])
def nifty_position_update():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/nifty?edit=1")
    state = engine.load_state("nifty")
    pos = state["positions"].get(tkr)
    if pos and shares > 0 and price > 0:
        state["cash"] = float(state.get("cash", 0)) + pos["shares"] * pos["entry"] - shares * price
        pos["shares"] = shares
        pos["entry"] = price
        pos["peak"] = max(float(pos.get("peak", price)), price)
        if request.form.get("since"):
            pos["since"] = request.form["since"]
        pos.pop("sup_entry", None)
        engine.save_state(state, "nifty")
    return redirect("/nifty?edit=1")


@app.route("/nifty/position/sell", methods=["POST"])
def nifty_position_sell():
    tkr = request.form.get("ticker", "")
    try:
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/nifty?edit=1")
    state = engine.load_state("nifty")
    pos = state["positions"].get(tkr)
    if pos and price > 0:
        engine.apply_fill(state, engine.Order("SELL", tkr, pos["shares"], price, "MANUAL"),
                          commission_pct=config.NIFTY_COMMISSION_PCT,
                          commission_min=config.NIFTY_COMMISSION_MIN)
        engine.save_state(state, "nifty")
    return redirect("/nifty?edit=1")


@app.route("/nifty/cash", methods=["POST"])
def nifty_set_cash():
    try:
        state = engine.load_state("nifty")
        new_cash = float(request.form["cash"])
        old_cash = float(state.get("cash", config.NIFTY_CAPITAL))
        state["deposited"] = float(state.get("deposited", config.NIFTY_CAPITAL)) + (new_cash - old_cash)
        state["cash"] = new_cash
        engine.save_state(state, "nifty")
    except (KeyError, ValueError):
        pass
    return redirect("/nifty?edit=1")


@app.route("/api/data")
def api_data():
    return jsonify(build_snapshot(force=request.args.get("refresh") == "1"))


# ---- editing: record your actual fills into state.json ----

@app.route("/position/add", methods=["POST"])
def position_add():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    if tkr and shares > 0 and price > 0:
        state = engine.load_state()
        engine.apply_fill(state, engine.Order("BUY", tkr, shares, price, "MANUAL"))
        engine.save_state(state)
    return redirect("/?edit=1")


@app.route("/position/update", methods=["POST"])
def position_update():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    state = engine.load_state()
    pos = state["positions"].get(tkr)
    if pos and shares > 0 and price > 0:
        # Correcting a fill changes the true cost basis -- true up cash by the
        # difference so account value doesn't silently drift (the original
        # add() already deducted the old cost basis from cash).
        state["cash"] = float(state.get("cash", 0)) + pos["shares"] * pos["entry"] - shares * price
        pos["shares"] = shares
        pos["entry"] = price
        pos["peak"] = max(float(pos.get("peak", price)), price)  # never below entry
        engine.save_state(state)
    return redirect("/?edit=1")


@app.route("/position/sell", methods=["POST"])
def position_sell():
    tkr = request.form.get("ticker", "")
    try:
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    state = engine.load_state()
    pos = state["positions"].get(tkr)
    if pos and price > 0:
        engine.apply_fill(state, engine.Order("SELL", tkr, pos["shares"], price, "MANUAL"))
        engine.save_state(state)
    return redirect("/?edit=1")


@app.route("/cash", methods=["POST"])
def set_cash():
    try:
        state = engine.load_state()
        new_cash = float(request.form["cash"])
        old_cash = float(state.get("cash", config.CAPITAL))
        # A cash change is a deposit/withdrawal (e.g. your monthly SIP), not profit:
        # move the P/L baseline by the same delta so contributions don't show as gains.
        state["deposited"] = float(state.get("deposited", config.CAPITAL)) + (new_cash - old_cash)
        state["cash"] = new_cash
        engine.save_state(state)
    except (KeyError, ValueError):
        pass
    return redirect("/?edit=1")


# ---- editing the trailing-S/R book (a separate portfolio, book="sr") ----

@app.route("/sr/position/add", methods=["POST"])
def sr_position_add():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    since = request.form.get("since") or date.today().isoformat()
    if tkr and shares > 0 and price > 0:
        state = engine.load_state("sr")
        engine.apply_fill(state, engine.Order("BUY", tkr, shares, price, "MANUAL"))
        state["positions"][tkr]["since"] = since   # honour the recorded buy date
        engine.save_state(state, "sr")
    return redirect("/?edit=1")


@app.route("/sr/position/update", methods=["POST"])
def sr_position_update():
    try:
        tkr = request.form["ticker"]
        shares = int(request.form["shares"])
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    state = engine.load_state("sr")
    pos = state["positions"].get(tkr)
    if pos and shares > 0 and price > 0:
        state["cash"] = float(state.get("cash", 0)) + pos["shares"] * pos["entry"] - shares * price
        pos["shares"] = shares
        pos["entry"] = price
        pos["peak"] = max(float(pos.get("peak", price)), price)
        if request.form.get("since"):
            pos["since"] = request.form["since"]
        pos.pop("sup_entry", None)   # recompute support from history on next load
        engine.save_state(state, "sr")
    return redirect("/?edit=1")


@app.route("/sr/position/sell", methods=["POST"])
def sr_position_sell():
    tkr = request.form.get("ticker", "")
    try:
        price = float(request.form["price"])
    except (KeyError, ValueError):
        return redirect("/?edit=1")
    state = engine.load_state("sr")
    pos = state["positions"].get(tkr)
    if pos and price > 0:
        engine.apply_fill(state, engine.Order("SELL", tkr, pos["shares"], price, "MANUAL"))
        engine.save_state(state, "sr")
    return redirect("/?edit=1")


@app.route("/sr/cash", methods=["POST"])
def sr_set_cash():
    try:
        state = engine.load_state("sr")
        new_cash = float(request.form["cash"])
        old_cash = float(state.get("cash", config.CAPITAL))
        # A cash change is a deposit/withdrawal (e.g. your monthly SIP), not profit:
        # move the P/L baseline by the same delta so contributions don't show as gains.
        state["deposited"] = float(state.get("deposited", config.CAPITAL)) + (new_cash - old_cash)
        state["cash"] = new_cash
        engine.save_state(state, "sr")
    except (KeyError, ValueError):
        pass
    return redirect("/?edit=1")


def _lan_ip():
    """Best-effort LAN IP so a phone on the same Wi-Fi knows what to browse to."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the outbound iface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Default to localhost-only. Set HOST=0.0.0.0 to reach it from your phone on
    # the same Wi-Fi (read-only dashboard, but don't expose it to the open internet).
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Portfolio dashboard on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    if host == "0.0.0.0":
        print(f"  On your phone (same Wi-Fi):  http://{_lan_ip()}:{port}")
    app.run(host=host, port=port, debug=False)
