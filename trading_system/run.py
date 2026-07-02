"""
CLI for the OMXS30 momentum trading system.

Commands:
  signal              show this week's ranking + buy/hold/sell recommendation
  status              show current positions and account value
  rebalance           run the weekly Friday reconciliation (buy/sell orders)
  stops               run the daily trailing-stop check
  reset               clear state back to starting cash (paper only)

Safety:
  - Default broker is 'paper' (simulated). Nothing hits a real account.
  - Live trading requires:  --broker nordnet --live  AND  NORDNET_VERIFIED=1
    AND typing the confirmation phrase when prompted.

Scheduling (cron on your own machine):
  # weekly rebalance — Friday ~17:35 CET, after Stockholm close
  35 17 * * 5  cd /path/trading_system && python run.py rebalance --broker nordnet --live
  # daily trailing-stop check — weekdays ~17:35 CET
  35 17 * * 1-5 cd /path/trading_system && python run.py stops --broker nordnet --live
"""

import argparse
import datetime as dt

import config
import strategy
import engine
import broker as broker_mod


def _log(line):
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with open(config.LOG_FILE, "a") as f:
        f.write(f"{stamp} {line}\n")
    print(line)


def cmd_signal(_args):
    prices = strategy.fetch_prices(config.TICKERS)
    sig = strategy.weekly_signal(prices)
    state = engine.load_state()
    held = set(state["positions"])
    print(f"\nSignal as of week ending {sig['asof']}  (top-{config.TOP_N}, "
          f"buffer to top-{config.TOP_N + config.RANK_BUFFER})\n")
    print(f"{'#':>2} {'stock':14} {'ticker':11} {'12w mom':>8}  action")
    for i, (tkr, mom, _) in enumerate(sig["ranking"][:config.TOP_N + config.RANK_BUFFER]):
        if i < config.TOP_N:
            act = "BUY / core hold"
        else:
            act = "hold if owned (buffer)"
        marker = " *held" if tkr in held else ""
        print(f"{i+1:>2} {strategy.NAMES.get(tkr, tkr):14} {tkr:11} {mom:+7.1%}  {act}{marker}")
    print("\n(* = currently in your book) — run 'rebalance' to act on this.")


def cmd_status(_args):
    prices = strategy.fetch_prices(config.TICKERS)
    px = strategy.latest_prices(prices)
    state = engine.load_state()
    pv = engine.portfolio_value(state, px)
    print(f"\nAccount value: {pv:,.0f} SEK   (cash {state['cash']:,.0f})")
    if not state["positions"]:
        print("No open positions.")
        return
    print(f"\n{'stock':14} {'shares':>7} {'entry':>8} {'now':>8} {'peak':>8} {'P/L':>7} {'stop@':>8}")
    for tkr, pos in state["positions"].items():
        now = float(px.get(tkr, pos["entry"]))
        pl = now / pos["entry"] - 1
        stop = pos.get("peak", pos["entry"]) * (1 - config.TRAIL_STOP_PCT)
        print(f"{strategy.NAMES.get(tkr, tkr):14} {pos['shares']:>7} {pos['entry']:>8.1f} "
              f"{now:>8.1f} {pos.get('peak', pos['entry']):>8.1f} {pl:>+6.1%} {stop:>8.1f}")


def _confirm_live(args):
    if args.broker == "nordnet" and args.live:
        phrase = f"TRADE {args.command.upper()} LIVE"
        print(f"\n!! LIVE Nordnet trading. Type exactly:  {phrase}")
        if input("> ").strip() != phrase:
            print("Aborted.")
            return False
    return True


def _execute(orders, args):
    if not orders:
        _log("No orders.")
        return
    state = engine.load_state()
    bkr = broker_mod.get_broker(args.broker, live=args.live)
    for o in orders:
        fill = bkr.execute(state, o)
        _log(f"{o.reason:10} {o.side:4} {o.shares:>5} {o.ticker:11} @~{o.price:.1f}  -> {fill['status']} [{fill['mode']}]")
    engine.save_state(state)
    px = strategy.latest_prices(strategy.fetch_prices(config.TICKERS))
    _log(f"Account value now ~{engine.portfolio_value(state, px):,.0f} SEK")


def cmd_rebalance(args):
    prices = strategy.fetch_prices(config.TICKERS)
    px = strategy.latest_prices(prices)
    sig = strategy.weekly_signal(prices)
    state = engine.load_state()
    # stops are checked first, then the weekly rank reconciliation
    breached, _ = strategy.trailing_stops(state, prices)
    engine.save_state(state)  # persist updated peaks
    orders = engine.plan_stop_orders(state, breached, px) + engine.plan_rebalance(state, sig, px)
    print(f"Planned {len(orders)} order(s) for week ending {sig['asof']}:")
    for o in orders:
        print(f"  {o.reason:10} {o.side:4} {o.shares:>5} {o.ticker:11} @~{o.price:.1f}")
    if not _confirm_live(args):
        return
    _execute(orders, args)


def cmd_stops(args):
    prices = strategy.fetch_prices(config.TICKERS)
    px = strategy.latest_prices(prices)
    state = engine.load_state()
    breached, _ = strategy.trailing_stops(state, prices)
    engine.save_state(state)
    orders = engine.plan_stop_orders(state, breached, px)
    if not orders:
        print("No trailing stops breached.")
        return
    print(f"{len(orders)} trailing stop(s) breached:")
    for o in orders:
        print(f"  {o.side} {o.shares} {o.ticker} @~{o.price:.1f}")
    if not _confirm_live(args):
        return
    _execute(orders, args)


def cmd_reset(args):
    if args.broker != "paper":
        print("reset is paper-only.")
        return
    engine.save_state({"cash": config.CAPITAL, "positions": {}})
    print(f"State reset to {config.CAPITAL:,.0f} SEK cash.")


def main():
    ap = argparse.ArgumentParser(description="OMXS30 momentum trading system")
    ap.add_argument("command", choices=["signal", "status", "rebalance", "stops", "reset"])
    ap.add_argument("--broker", choices=["paper", "nordnet"], default="paper")
    ap.add_argument("--live", action="store_true", help="send real orders (nordnet only)")
    args = ap.parse_args()
    {"signal": cmd_signal, "status": cmd_status, "rebalance": cmd_rebalance,
     "stops": cmd_stops, "reset": cmd_reset}[args.command](args)


if __name__ == "__main__":
    main()
