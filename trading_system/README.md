# OMXS30 Momentum Trading System

An automated implementation of the validated momentum strategy (see the
repo-root [`README.md`](../README.md) and [`backtest.py`](../backtest.py)): own
the top-4 momentum names in OMXS30, hold through a top-8 rank buffer, protect
with a daily-checked 20% trailing stop. Runs in **paper mode** out of the box;
Nordnet live execution is an opt-in adapter you complete and enable yourself.

## Strategy (fixed parameters — do not tune)

Top-4 holdings · 12-week momentum ranking · 20-week SMA trend filter · hold
while ranked in the top 8 · daily 20% trailing stop · ~85% invested. The
walk-forward test showed re-tuning these *hurts*, so they live in
[`config.py`](config.py) and should stay put.

## Install

```bash
pip install -r requirements.txt
```

## Use (paper mode — safe, simulated)

```bash
python run.py signal        # this week's ranking + buy/hold/sell list
python run.py rebalance     # weekly Friday reconciliation (simulated fills)
python run.py stops         # daily trailing-stop check
python run.py status        # current positions + account value
python run.py reset         # clear paper state back to starting cash
```

Account size is set by the `CAPITAL` env var (default 100,000 SEK). State
persists in `state.json`; a human-readable audit trail is appended to
`trades.log`. Both are git-ignored.

## The routine this automates

- **Every Friday after the Stockholm close:** `rebalance` — sells any holding
  that fell out of the top 8, buys the top-4 names you don't already own,
  equal-weighted at ~21% each.
- **Every weekday after close:** `stops` — sells any position that has fallen
  20% from its peak close since entry.

Schedule it with cron on your own always-on machine (times are CET):

```cron
35 17 * * 5   cd /path/trading_system && python run.py rebalance --broker nordnet --live
35 17 * * 1-5 cd /path/trading_system && python run.py stops     --broker nordnet --live
```

## Going live with Nordnet — read this first

Live execution is deliberately gated. `broker.py`'s `NordnetBroker` is an
**integration skeleton against the documented nExt API, not a verified client.**
Nordnet's API is customer/partner-gated and its auth/endpoints change, so before
trading real money you must:

1. **Confirm API access** for your account at nordnet.se.
2. **Verify against the current official Nordnet API docs:** the login/auth
   scheme (implement `_build_auth()` — the RSA-encrypted `auth` blob), the
   instrument-lookup response shape (`resolve_instrument()`), and the order
   payload keys (`execute()`). Every one of these is marked `VERIFY` in the code.
3. Set the environment:
   ```bash
   export NORDNET_USER=... NORDNET_PASS=... NORDNET_ACCNO=...
   export NORDNET_VERIFIED=1        # only after you've done steps 1-2
   ```
4. Run with `--broker nordnet --live`. You will be asked to type a confirmation
   phrase before any real order is sent.

Without `NORDNET_VERIFIED=1`, live orders are refused. Without `--live`, the
Nordnet broker only dry-runs (prints intended orders, sends nothing).

## What this system does NOT do (limitations)

- **No live-order testing was possible in development** — the paper broker and
  signal logic are fully tested; the Nordnet order path is structured but must
  be verified by you against the live API.
- **No slippage/spread, tax, or partial-fill modelling.** Reference prices are
  last closes; real fills differ. Size expectations off the backtest's
  after-commission (not after-tax, not after-slippage) figures.
- **No intraday data.** Stops are checked on daily closes — a gap-down can
  exit worse than -20%. For a true intraday stop, place a resting stop order at
  the broker in addition.
- **Not financial advice.** It mechanically executes a backtested rule on a
  survivorship-biased universe. Understand the ~-27% drawdowns before funding it.

## Files

| File | Role |
|------|------|
| `config.py` | Strategy parameters, universe, account, Nordnet settings |
| `strategy.py` | Data fetch + weekly signal + trailing-stop evaluation |
| `engine.py` | State persistence + order planning (reconciliation) |
| `broker.py` | `PaperBroker` (works) + `NordnetBroker` (verify before live) |
| `run.py` | CLI entry point |
