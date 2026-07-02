"""
Short iron condor simulator with dynamic leg adjustment — OMXS30
=================================================================
Compares three ways of running a 45-DTE short iron condor over 16 years of
real OMXS30 index paths:

  1. STATIC      : open, hold to expiry.
  2. MECHANICAL  : + take profit at 50% of credit, hard exit at 21 DTE.
  3. DYNAMIC     : mechanical rules + roll the UNTESTED side when spot has
                   travelled >=50% of the distance from entry to a tested
                   short strike. The untested spread is re-placed close
                   enough to collect ~80% of the tested spread's current
                   value (credit-only, never inverting past the tested short).

IMPORTANT MODELLING NOTE
------------------------
The index *paths* are real (Yahoo ^OMX daily). The option *prices* are modelled
with Black-Scholes on a realized-vol proxy (trailing 20d RV, annualized, + a
3 vol-pt risk premium). So the path-dependent LOGIC (when you get tested, what
a roll collects, whipsaw outcomes) is faithful; only absolute premium levels
carry a vol assumption. The COMPARISON between the three arms is therefore
trustworthy even if the absolute SEK/trade is approximate.

Run:  python experiments/iron_condor_sim.py
Writes experiments/iron_condor_trades.csv (per-trade log for all arms).
"""

import os
from math import log, sqrt, exp
from statistics import NormalDist

import numpy as np
import pandas as pd

Ncdf = NormalDist().cdf

# ---- config ----
MULT = 100                 # SEK per index point
DTE0 = 45                  # days to expiry at entry
R = 0.025                  # risk-free
VRP = 0.03                 # vol risk premium added to realized vol
IV_FLOOR = 0.10
GRID = 20                  # strike grid (points)
WING = 100                 # wing width (points)
SHORT_DELTA = 0.16         # ~1 SD short strikes
PROFIT_TAKE = 0.50         # close at 50% of credit captured
MANAGE_DTE = 21            # hard management day
ROLL_TRIGGER_FRAC = 0.50   # roll untested when spot travels 50% toward tested short
ROLL_TARGET_FRAC = 0.80    # new untested credit ~ 80% of tested spread value
MAX_ROLLS = 3              # cap rolls per side
COMMISSION = 19            # SEK per contract per leg


def bs(S, K, T, iv, call):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (log(S / K) + (R + iv * iv / 2) * T) / (iv * sqrt(T))
    d2 = d1 - iv * sqrt(T)
    if call:
        return S * Ncdf(d1) - K * exp(-R * T) * Ncdf(d2)
    return K * exp(-R * T) * Ncdf(-d2) - S * Ncdf(-d1)


def delta(S, K, T, iv, call):
    if T <= 0:
        return float((S > K) if call else (S < K))
    d1 = (log(S / K) + (R + iv * iv / 2) * T) / (iv * sqrt(T))
    return Ncdf(d1) if call else Ncdf(d1) - 1.0


def snap(x):
    return round(x / GRID) * GRID


def short_strike(S, T, iv, call):
    """Grid strike whose delta is closest to +/-SHORT_DELTA."""
    step = GRID if call else -GRID
    best, berr = None, 1e9
    k = snap(S)
    for _ in range(80):
        d = abs(delta(S, k, T, iv, call))
        err = abs(d - SHORT_DELTA)
        if err < berr:
            berr, best = err, k
        if d < SHORT_DELTA:      # too far OTM already
            break
        k += step
    return best


class Position:
    """Tracks legs and realized cash for one condor trade."""
    def __init__(self):
        self.legs = {}     # (strike, is_call, is_short) -> qty (always 1 here)
        self.cash = 0.0    # net premium points collected (+in)
        self.commission = 0.0
        self.rolls = 0

    def _trade(self, strike, is_call, is_short, price, opening):
        # opening short => receive; opening long => pay; closing flips sign
        sign = 1 if (is_short == opening) else -1
        self.cash += sign * price
        self.commission += COMMISSION
        key = (strike, is_call)
        self.legs[key] = ("short" if is_short else "long")

    def open_spread(self, S, T, iv, is_call, short_k, long_k):
        self._trade(short_k, is_call, True, bs(S, short_k, T, iv, is_call), True)
        self._trade(long_k, is_call, False, bs(S, long_k, T, iv, is_call), True)
        self.spread = getattr(self, "spread", {})
        self.spread[is_call] = (short_k, long_k)

    def spread_value(self, S, T, iv, is_call):
        sk, lk = self.spread[is_call]
        return bs(S, sk, T, iv, is_call) - bs(S, lk, T, iv, is_call)

    def close_spread(self, S, T, iv, is_call):
        sk, lk = self.spread[is_call]
        self._trade(sk, is_call, True, bs(S, sk, T, iv, is_call), False)
        self._trade(lk, is_call, False, bs(S, lk, T, iv, is_call), False)

    def cost_to_close(self, S, T, iv):
        c = 0.0
        for is_call in (True, False):
            if is_call in self.spread:
                c += self.spread_value(S, T, iv, is_call)   # short-heavy: positive = pay to close
        return c

    def open_pnl(self, S, T, iv):
        return self.cash - self.cost_to_close(S, T, iv)


def run_trade(prices, i0, mode, iv_series):
    """Simulate one condor opened at index i0. Returns dict or None."""
    dates = prices.index
    S0 = prices.iloc[i0]
    iv0 = iv_series.iloc[i0]
    T0 = DTE0 / 365
    expiry_i = i0 + int(round(DTE0 * 252 / 365))
    if expiry_i >= len(prices):
        return None

    pos = Position()
    csk = short_strike(S0, T0, iv0, True)
    psk = short_strike(S0, T0, iv0, False)
    if csk is None or psk is None or psk >= csk:
        return None
    pos.open_spread(S0, T0, iv0, True, csk, csk + WING)
    pos.open_spread(S0, T0, iv0, False, psk, psk - WING)
    credit0 = pos.cash
    pos._credit_base = credit0

    for i in range(i0 + 1, expiry_i + 1):
        S = prices.iloc[i]
        iv = iv_series.iloc[i]
        dte = (expiry_i - i) * 365 / 252
        T = max(dte, 0) / 365

        if mode == "dynamic" and pos.rolls < 2 * MAX_ROLLS:
            csk_cur, _ = pos.spread[True]
            psk_cur, _ = pos.spread[False]
            # call side tested?
            if S >= S0 + ROLL_TRIGGER_FRAC * (csk_cur - S0) and pos.rolls < MAX_ROLLS:
                tested_val = pos.spread_value(S, T, iv, True)
                _roll(pos, S, T, iv, is_call_untested=False, tested_val=tested_val, csk=csk_cur, psk=psk_cur)
            # put side tested?
            elif S <= S0 - ROLL_TRIGGER_FRAC * (S0 - psk_cur) and pos.rolls < MAX_ROLLS:
                tested_val = pos.spread_value(S, T, iv, False)
                _roll(pos, S, T, iv, is_call_untested=True, tested_val=tested_val, csk=csk_cur, psk=psk_cur)

        if mode in ("mechanical", "dynamic"):
            if pos.open_pnl(S, T, iv) >= PROFIT_TAKE * max(pos.cash_credit(), 0.01):
                break
            if dte <= MANAGE_DTE:
                break

    # settle / close at current bar
    Sx = prices.iloc[min(i, expiry_i)]
    Tx = max((expiry_i - min(i, expiry_i)) * 365 / 252, 0) / 365
    ivx = iv_series.iloc[min(i, expiry_i)]
    pnl_pts = pos.cash - pos.cost_to_close(Sx, Tx, ivx)
    pnl_sek = pnl_pts * MULT - pos.commission
    return dict(entry=dates[i0].date(), exit=dates[min(i, expiry_i)].date(),
                S0=round(S0), Sx=round(Sx), rolls=pos.rolls,
                credit0=round(credit0 * MULT), pnl_sek=round(pnl_sek), fees=round(pos.commission))


def _roll(pos, S, T, iv, is_call_untested, tested_val, csk, psk):
    """Buy back untested spread, re-sell it to collect ~ROLL_TARGET_FRAC*tested_val, credit-only, no inversion."""
    target = ROLL_TARGET_FRAC * tested_val
    old_val = pos.spread_value(S, T, iv, is_call_untested)
    # search new short strike toward spot for one collecting >= target, not crossing the tested short
    if is_call_untested:
        limit = csk - GRID
        k = snap(S)
        best = None
        while k <= limit:
            v = bs(S, k, T, iv, True) - bs(S, k + WING, T, iv, True)
            if v >= target:
                best = k
            k += GRID
        newk = best
    else:
        limit = psk + GRID
        k = snap(S)
        best = None
        while k >= limit:
            v = bs(S, k, T, iv, False) - bs(S, k - WING, T, iv, False)
            if v >= target:
                best = k
            k -= GRID
        newk = best
    if newk is None:
        return
    new_val = bs(S, newk, T, iv, True) - bs(S, newk + WING, T, iv, True) if is_call_untested \
        else bs(S, newk, T, iv, False) - bs(S, newk - WING, T, iv, False)
    if new_val - old_val <= 0:      # credit-only guard
        return
    pos.close_spread(S, T, iv, is_call_untested)
    pos.open_spread(S, T, iv, is_call_untested, newk, newk + WING if is_call_untested else newk - WING)
    pos.rolls += 1


# small helper on Position for profit target base (cumulative credit received)
def _cash_credit(self):
    return getattr(self, "_credit_base", self.cash)
Position.cash_credit = _cash_credit


def summarize(name, trades):
    df = pd.DataFrame(trades)
    pnl = df.pnl_sek
    wins = (pnl > 0).mean()
    worst5 = pnl.quantile(0.05)
    print(f"{name:12s} trades {len(df):3d}  win {wins:4.0%}  "
          f"avg {pnl.mean():6,.0f}  median {pnl.median():6,.0f}  "
          f"worst5% {worst5:8,.0f}  max_loss {pnl.min():8,.0f}  "
          f"total {pnl.sum():9,.0f}  rolls/trade {df.rolls.mean():.2f}")
    return df


if __name__ == "__main__":
    print("Loading OMXS30...")
    close = pd.read_csv(
        "/tmp/claude-0/-home-user-SwingTrading/31cffeec-15a6-5f82-b0bf-e77cd82b35d0/scratchpad/omx.csv",
        index_col=0, parse_dates=True).iloc[:, 0].dropna()
    ret = np.log(close / close.shift(1))
    iv_series = (ret.rolling(20).std() * np.sqrt(252) + VRP).clip(lower=IV_FLOOR).bfill()

    all_trades = {m: [] for m in ("static", "mechanical", "dynamic")}
    for mode in all_trades:
        i = 25
        while i < len(close) - 40:
            t = run_trade(close, i, mode, iv_series)
            if t is None:
                i += 1
                continue
            all_trades[mode].append(t)
            # sequential non-overlapping: next entry after this trade's exit
            exit_i = close.index.get_loc(pd.Timestamp(t["exit"]))
            i = max(exit_i + 1, i + 1)

    print(f"\nOMXS30 {close.index[0].date()} -> {close.index[-1].date()}, "
          f"45-DTE short iron condor, ~16-delta shorts, {WING}-pt wings, 1 lot\n")
    frames = []
    for mode in ("static", "mechanical", "dynamic"):
        df = summarize(mode.upper(), all_trades[mode])
        df.insert(0, "arm", mode)
        frames.append(df)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iron_condor_trades.csv")
    pd.concat(frames).to_csv(out, index=False)
    print(f"\nPer-trade log: {out}")
    print("NOTE: option prices are BS-modelled on realized-vol proxy; compare arms, not absolute SEK.")
