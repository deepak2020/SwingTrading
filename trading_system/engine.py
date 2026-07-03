"""
Portfolio reconciliation: turn signals + current state into a list of Orders.

Keeps the strategy's own state (entry price, trailing peak) which the broker
does not track. State is a JSON file:

  {"cash": float, "positions": {ticker: {"shares": int, "entry": float,
                                         "peak": float, "since": "YYYY-MM-DD"}}}
"""

import json
import os
from dataclasses import dataclass, asdict

import pandas as pd

import config


@dataclass
class Order:
    side: str        # "BUY" | "SELL"
    ticker: str
    shares: int
    price: float     # reference price (last close); execution may differ
    reason: str      # RANK_EXIT | TRAIL_STOP | BUY

    def notional(self):
        return self.shares * self.price


# Storage: a Postgres DATABASE_URL (e.g. a free Neon/Supabase database) makes the
# state survive restarts when hosted; without it we fall back to a local JSON file.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_DEFAULT_STATE = lambda: {"cash": config.CAPITAL, "positions": {}}


def storage_mode():
    """'postgres' when a DATABASE_URL is configured, else 'file' (ephemeral on
    free hosts). Surfaced at /healthz so you can confirm persistence is active."""
    return "postgres" if DATABASE_URL else "file"


def _db():
    """A fresh Postgres connection with the state table ensured. Callers must
    close it (connections are not pooled here)."""
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    with conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS app_state (id INT PRIMARY KEY, data TEXT NOT NULL)")
    return conn


def load_state():
    if DATABASE_URL:
        conn = _db()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT data FROM app_state WHERE id = 1")
                row = cur.fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else _DEFAULT_STATE()
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return _DEFAULT_STATE()


def save_state(state):
    payload = json.dumps(state, default=str)
    if DATABASE_URL:
        conn = _db()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_state (id, data) VALUES (1, %s) "
                            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data", [payload])
        finally:
            conn.close()
        return
    with open(config.STATE_FILE, "w") as f:
        f.write(json.dumps(state, indent=2, default=str))


def portfolio_value(state, px):
    val = state["cash"]
    for tkr, pos in state["positions"].items():
        p = px.get(tkr)
        val += pos["shares"] * (float(p) if p is not None and pd.notna(p) else pos["entry"])
    return val


def plan_stop_orders(state, breached, px):
    """SELL orders for stop-breached positions."""
    orders = []
    for tkr in breached:
        pos = state["positions"][tkr]
        orders.append(Order("SELL", tkr, pos["shares"], float(px[tkr]), "TRAIL_STOP"))
    return orders


def plan_rebalance(state, signal, px):
    """Weekly reconciliation -> SELL names out of the buffer, BUY to refill top-N."""
    orders = []
    held = set(state["positions"])

    # 1. exit holdings that dropped out of the top-N+buffer
    for tkr in list(held):
        if tkr not in signal["hold_ok"]:
            p = px.get(tkr)
            if p is None or pd.isna(p):
                continue
            pos = state["positions"][tkr]
            orders.append(Order("SELL", tkr, pos["shares"], float(p), "RANK_EXIT"))
            held.discard(tkr)

    # 2. fill open slots from the top-N target
    slots = config.TOP_N - len(held)
    if slots > 0:
        pv = portfolio_value(state, px)
        alloc = pv * config.MAX_EXPOSURE / config.TOP_N
        for tkr in [t for t in signal["target"] if t not in held][:slots]:
            p = px.get(tkr)
            if p is None or pd.isna(p) or p <= 0:
                continue
            shares = int(alloc // float(p))
            if shares <= 0:
                continue
            orders.append(Order("BUY", tkr, shares, float(p), "BUY"))
    return orders


def apply_fill(state, order):
    """Update state for an executed order (used by paper broker & live sync)."""
    fee = max(order.notional() * config.COMMISSION_PCT, config.COMMISSION_MIN)
    if order.side == "BUY":
        state["cash"] -= order.notional() + fee
        state["positions"][order.ticker] = {
            "shares": order.shares, "entry": order.price,
            "peak": order.price, "since": pd.Timestamp.today().date().isoformat(),
        }
    else:  # SELL
        state["cash"] += order.notional() - fee
        state["positions"].pop(order.ticker, None)
    return fee
