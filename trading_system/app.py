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
_CACHE = {"prices": None, "index": None, "ts": 0.0}
_CACHE_TTL = 300  # 5 minutes — matches the page's 5-min auto-refresh

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
    if force or _CACHE["prices"] is None or (time.time() - _CACHE["ts"]) > _CACHE_TTL:
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
    state = engine.load_state()
    sig = strategy.weekly_signal(prices)
    held = set(state["positions"])

    # --- positions ---
    positions = []
    invested = 0.0
    for tkr, pos in state["positions"].items():
        now = float(px.get(tkr, pos["entry"]))
        peak = float(pos.get("peak", pos["entry"]))
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

    total_pl = account_value - config.CAPITAL
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
        "invested": round(invested, 0),
        "exposure_pct": round(invested / account_value * 100, 1) if account_value else 0.0,
        "start_capital": config.CAPITAL,
        "total_pl": round(total_pl, 0),
        "total_pl_pct": round(total_pl / config.CAPITAL * 100, 1),
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
    }


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
      {% if edit %}<a class="btn" href="/">✓ Done</a>
      {% else %}<a class="btn ghost" href="?edit=1">✎ Edit</a>
      <a class="btn" href="?refresh=1">↻ Refresh</a>{% endif %}
    </span>
  </header>

  <div class="cards">
    <div class="card"><div class="label">Account value</div>
      <div class="val">{{ "{:,.0f}".format(d.account_value) }} <span class="sub">SEK</span></div></div>
    <div class="card"><div class="label">Total P/L</div>
      <div class="val {{ 'pos' if d.total_pl>=0 else 'neg' }}">
        {{ '+' if d.total_pl>=0 else '' }}{{ "{:,.0f}".format(d.total_pl) }}
        <span class="sub">({{ '+' if d.total_pl_pct>=0 else '' }}{{d.total_pl_pct}}%)</span></div></div>
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
      · now {{p.now}} · P/L <span class="{{ 'pos' if p.pl_pct>=0 else 'neg' }}">{{ '+' if p.pl_pct>=0 else '' }}{{p.pl_pct}}%</span></div>
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
    <div class="ename">Cash</div>
    <form class="frow" method="post" action="/cash">
      <label>Balance (SEK)<input type="number" name="cash" value="{{ "%.2f"|format(d.cash) }}" step="0.01" inputmode="decimal"></label>
      <button>Set cash</button>
    </form>
  </div>

  {% elif d.positions %}
  <div class="tablescroll"><table>
    <thead><tr>
      <th>Stock</th><th>Shares</th><th>Entry</th><th>Now</th><th>Peak</th>
      <th>P/L</th><th>Value</th><th>Weight</th><th>Stop @</th><th>To stop</th>
    </tr></thead><tbody>
    {% for p in d.positions %}
    <tr>
      <td><strong>{{p.name}}</strong> <span class="sub">{{p.ticker}}</span><br>
        <span class="pill {{p.action|lower}}">{{p.action}}</span></td>
      <td>{{p.shares}}</td><td>{{p.entry}}</td><td>{{p.now}}</td><td>{{p.peak}}</td>
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


def _sparkline(hist, w=920, h=120, pad=8):
    """Minimal inline-SVG equity line (no external deps)."""
    vals = [p["value"] for p in hist]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    def x(i): return pad + i * (w - 2 * pad) / (n - 1)
    def y(v): return pad + (h - 2 * pad) * (1 - (v - lo) / rng)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{pad},{h-pad} " + pts + f" {w-pad},{h-pad}"
    up = vals[-1] >= vals[0]
    col = "#22c55e" if up else "#ef4444"
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
        state["cash"] = float(request.form["cash"])
        engine.save_state(state)
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
