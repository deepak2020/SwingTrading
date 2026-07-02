# SwingTrading — OMXS30 Weekly Momentum Backtest

A fully systematic weekly momentum swing-trading backtest for the **OMXS30**
(Swedish large-cap universe), implemented in [`backtest.py`](backtest.py).

## Strategy rules

| Component | Rule |
|-----------|------|
| Universe | OMXS30 constituents (`.ST` tickers) |
| Trend filter | Price must be above its **20-week SMA** |
| Signal | Rank by **12-week momentum** (total return) |
| Entry | Buy **top 6** ranked stocks, equal weight, every Friday close |
| Exit | Sell when a name drops out of the top 6 **or** breaks the trend filter |
| Risk mgmt | **10% per-position stop-loss**, max 85% capital exposure |
| Costs | Nordnet **0.15% commission, min 39 SEK** per trade |
| Capital | 50,000 SEK starting |
| Period | 2015-01-01 → 2026-06-30 |

## Results (live run, 2015-08-14 → 2026-07-03)

| Metric | Value |
|--------|-------|
| Initial capital | 50,000 SEK |
| Final equity | **88,093 SEK** |
| Total return | 76.2% |
| CAGR | 5.3% |
| Max drawdown | -26.7% |
| Sharpe (annualized) | 0.40 |
| Trades | 774 buys / 32 stop-losses / 736 rank exits |
| Total fees paid | **60,138 SEK** |
| Avg implied monthly profit | **292 SEK** (target: 2,000 SEK) |

**Verdict: average monthly profit (~292 SEK) is well below the 2,000 SEK target.**
The strategy is profitable gross, but weekly re-ranking generates ~1,500 trades and
Nordnet's 39 SEK minimum commission consumes ~61% of gross gains (60,138 SEK of fees
vs. 38,093 SEK of net profit). 3 of 29 tickers (`EQT.ST`, `ESSITY-B.ST`, `EPI-A.ST`)
are auto-excluded by the 80%-coverage filter because they listed mid-period.

Output files from this run are in [`outputs/`](outputs/).

## Alternative strategy: support/resistance swing ([`backtest_sr.py`](backtest_sr.py))

A second, independent strategy: buy pullbacks to the 12-week Donchian low
(bullish weekly close + 40-week SMA uptrend filter required), take profit near
the 12-week high, stop out 5% below the entry support level, force-exit after
12 weeks. Positions are per-stock, not ranked/rotated, so turnover is
structurally low. Run with `CAPITAL=100000 python backtest_sr.py`.

Live results (2015-10-16 → 2026-07-03):

| Metric | @ 50k SEK | @ 100k SEK |
|--------|-----------|------------|
| Final equity | 84,560 | 203,978 |
| CAGR | 5.0% | 6.9% |
| Max drawdown | **-11.8%** | **-11.2%** |
| Sharpe | 0.56 | 0.73 |
| Trades (buys) | 272 | 272 |
| Take-profit / stop / max-hold | 201 / 45 / 24 | 202 / 44 / 24 |
| Total fees | 21,138 (61% of gross) | 21,905 (21% of gross) |
| Avg monthly profit | 269 SEK | **809 SEK** |
| Benchmark buy & hold CAGR | 16.7% | 16.7% |

Takeaways: very high hit rate (~74% of exits are take-profits) and less than
half the drawdown of the momentum strategy, but much lower absolute returns —
it spends a lot of time in cash and its winners are capped at the channel top.
Both strategy variants underperform simple equal-weight buy & hold (16.7%
CAGR) on raw return; the S/R strategy's edge is purely risk-adjusted comfort,
not profit. It does not reach the 2,000 SEK/month target at either capital
level.

The benchmark's full "trade log" is in
[`outputs/buyhold_positions.csv`](outputs/buyhold_positions.csv): 26 buys on
2015-10-16 and nothing after. 100k → 522.5k SEK with 1 loser out of 26; the
top 5 positions (EVO +1369%, ADDT-B +1353%, SAAB +968%, LIFCO +883%, ABB
+638%) make up 42% of the final value — the compounders no exit rule was
allowed to cut. Note the benchmark carries survivorship bias: the universe is
today's OMXS30 members, so its 16.7% CAGR is an optimistic figure (this bias
affects all backtests in this repo, buy & hold most of all).

### Hybrid: S/R entries + trailing-stop exits

[`experiments/sr_trailing_search.py`](experiments/sr_trailing_search.py) keeps
the S/R entry logic untouched but replaces the resistance take-profit with a
trailing stop (exit when close falls X% below the highest close since entry;
no profit cap, no max hold). Full sweep in
[`experiments/sr_trailing_results.csv`](experiments/sr_trailing_results.csv).
At 100,000 SEK:

| Exit rule | Monthly SEK | CAGR | Max DD | Sharpe | Buys | Fees |
|-----------|------------|------|--------|--------|------|------|
| TP at resistance (original) | 789 | 6.8% | -11.2% | 0.72 | 272 | 21,876 |
| **Trailing 15%** | **2,671** | 14.9% | -23.1% | **0.96** | 64 | 6,797 |
| Trailing 20% | 2,961 | 15.8% | -25.9% | 0.96 | 43 | 4,289 |
| Trailing 25% | 3,381 | 16.9% | -29.7% | 0.90 | 36 | 3,392 |

Uncapping the winners is worth 3–4x the monthly profit. **The 15% trail is the
most robust configuration found in this repo:** in a split-half test it earned
~1,800 SEK/month in 2015–2020 *and* ~1,900 SEK/month in 2021–2026 — the only
strategy variant that did not decay in the recent half (the momentum variants
dropped to ~550–1,100). Fees become negligible (~6.8k over a decade) because
the strategy makes only ~6 trades per year. Caveats: with so few trades the
statistics rest on a small sample and a handful of large winners, and wider
trails (25%) increasingly just converge toward concentrated buy & hold.

### Daily-timeframe swing trading: a negative result

[`experiments/swing_daily_search.py`](experiments/swing_daily_search.py) runs
the S/R bounce concept on **daily bars** (20–90 day Donchian channels, holds
of days instead of months, 48 configs at 100k SEK) — full results in
[`experiments/swing_daily_results.csv`](experiments/swing_daily_results.csv).

**Zero of 48 configs reach 2,000 SEK/month.** The best earns 325 SEK/month
while paying 140,676 SEK in fees on 1,806 trades; short 20-day channels churn
the account to near-zero (-97%). Win rates are fine (53–59%) — the problem is
per-trade economics: with ~15k SEK positions, the ~78 SEK round-trip minimum
commission is ~0.5%, while the average daily-swing win over 5–11 days is only
a few percent. Fee cost per trade rivals edge per trade.

Conclusion: on this universe with Nordnet's fee structure at 100k, higher
trade frequency destroys profit monotonically. The weekly cadence of the
other strategies is not a limitation — it is what keeps them alive. Positions
need to exceed ~26,000 SEK (39 / 0.0015) before the minimum commission stops
binding; at 6 slots that implies a ~185k+ account before daily-frequency
trading even becomes worth re-testing.

### Should the parameters be fine-tuned? (walk-forward test)

[`experiments/walk_forward.py`](experiments/walk_forward.py) answers this
honestly: every January from 2019, re-optimize all momentum parameters on the
trailing 4 years only, then trade the next year blind, chaining equity
(yearly detail in
[`experiments/walk_forward_results.csv`](experiments/walk_forward_results.csv)).

Result 2019–2026 from 100k SEK:

| Approach | Final | CAGR | ~SEK/month |
|----------|-------|------|-----------|
| Re-tuned yearly (pick trailing best by return) | 201,131 | 9.8% | 1,124 |
| Re-tuned yearly (pick trailing best by Sharpe) | 217,003 | 10.9% | 1,300 |
| Fixed top-4 / mom-12 / buffer-4 / stop-15 | 237,081 | 12.2% | 1,523 |
| Fixed original params (never tuned at all) | 232,704 | 11.9% | 1,474 |

**Yearly re-tuning lost to never tuning.** The adaptive picker chased the
previous regime — it entered 2023 and 2026 with aggressive parameters fitted
to the prior rally and gave back double-digit returns in both (e.g. 2023:
tuned -4% vs fixed +11–17%). Even the completely untuned original parameters
beat both adaptive selectors. (The fixed sweep-winner row is slightly
flattered here since it was chosen using this same period; the untuned
original row is the clean comparison — and it still wins.)

Practical conclusion: pick structurally robust parameters (small N, a rank
buffer, a trend filter, a wide stop), then leave them alone.

### Daily-checked and trailing stops

[`experiments/daily_stop_search.py`](experiments/daily_stop_search.py) keeps the
weekly ranking/buffer but checks the stop every DAY (not just Fridays) and tests
a trailing stop that ratchets up from each position's peak. Results in
[`experiments/daily_stop_results.csv`](experiments/daily_stop_results.csv), 100k SEK:

| Config | Monthly | CAGR | Sharpe | Max DD | Worst trade | Stop-out share |
|--------|---------|------|--------|--------|-------------|----------------|
| Weekly fixed 15% (champion) | 2,922 | 15.5% | 0.85 | -28.7% | -24% | 3% |
| Daily fixed 15% | 3,037 | 15.9% | 0.87 | -27.8% | -21% | 3% |
| Daily trail 15% | 2,901 | 15.5% | 0.87 | -28.3% | -21% | 12% |
| **Daily trail 20%** | **3,102** | **16.1%** | **0.89** | **-26.7%** | -21% | 4% |
| Daily trail 25% | 2,647 | 14.7% | 0.82 | -27.8% | -23% | 2% |
| Daily trail 30% | 2,825 | 15.3% | 0.84 | -28.7% | -24% | 0% |

Checking the stop daily instead of weekly is a small free win (worst trade
-24%→-21%, slightly better returns, no extra turnover). A ~20% daily trailing
stop was the best config: best Sharpe, mildest drawdown, and it rarely fires so
it doesn't churn. Tighter trails (15%) whipsaw (12% stop-out share, lower
return). Caveats: the 20/25/30 ordering is partly small-sample noise (read as
"~20% trail is a good safe choice"), and the worst trade only reaches -21% not
-15% because stops are checked on daily closes — a gap-down still slips through;
truly capping near -15% needs an intraday broker stop order (more false
triggers). The stop remains a backstop, not the return engine.

### Shorter holds for "classic" 2-3 week swing cadence

[`experiments/hold_period_search.py`](experiments/hold_period_search.py) tests
whether the champion's ~6-week hold can be cut to 2-3 weeks (faster momentum
lookback, smaller buffer, optional max-hold cap) at 100k SEK — full results in
[`experiments/hold_period_results.csv`](experiments/hold_period_results.csv).

| Config | Mean hold | Trades/yr | Fees | Monthly | CAGR | Sharpe | Max DD |
|--------|-----------|-----------|------|---------|------|--------|--------|
| Champion (mom12, buf4) | 7.8w | 26 | 48,581 | 2,919 | 15.5% | 0.85 | -28.7% |
| mom8, buf2 | 4.5w | 46 | 61,277 | 1,393 | 10.0% | 0.60 | -24.8% |
| mom6, buf0 | 2.8w | 73 | 81,532 | 1,217 | 9.1% | 0.58 | -24.1% |
| mom3, buf0 | 2.0w | 101 | 87,293 | 262 | 2.7% | 0.24 | -28.6% |
| mom4, buf0, cap 2w | 1.6w | 132 | 113,269 | -114 | -1.5% | 0.00 | -47.1% |

Shortening holds to ~3 weeks (mom6/buf0) nearly doubles fees (48k→82k over the
decade) and roughly halves monthly profit (2,919→1,217) — the extra turnover
cost almost exactly equals the lost profit. Below ~3 weeks it degrades further
on two fronts: more fees *and* signal decay (momentum is a 3-12 month effect;
a 3-4 week lookback measures noise, drifting toward short-term reversal), so
mom3/mom4 lose on return *and* drawdown (-40%/-47%). The 2-week forced-exit
variant loses money outright. Best genuine 2-3 week config: mom6/buf0
(~1,217 SEK/mo, milder -24% drawdown). Reasonable middle ground: mom8/buf2
(~4.5w, 1,393 SEK/mo, best shortened-set Sharpe). But 6-week holds are already
within the classic swing definition and remain the risk-adjusted champion.

## Options: short iron condor with dynamic leg adjustment

[`experiments/iron_condor_sim.py`](experiments/iron_condor_sim.py) simulates a
45-DTE short iron condor (~16-delta shorts, 100-pt wings) on 16 years of real
OMXS30 index paths, comparing three management styles. Per-trade log in
[`experiments/iron_condor_trades.csv`](experiments/iron_condor_trades.csv).

**Modelling note:** index paths are real; option prices are Black-Scholes on a
realized-vol proxy (trailing 20d RV + 3-pt vol risk premium). The path-dependent
logic (tested/untested, roll credit, whipsaws) is faithful — only absolute
premium levels carry a vol assumption, so compare the arms, not the SEK totals.

| Arm | Trades | Win % | Avg/trade | Max loss | Total P&L |
|-----|--------|-------|-----------|----------|-----------|
| Static (hold to expiry) | 128 | 85% | +686 | -8,552 | +87,776 |
| Mechanical (50% profit take, exit 21 DTE) | 299 | 76% | +259 | -7,143 | +77,393 |
| Dynamic (roll untested side, 50% trigger / 80% credit) | 300 | 68% | +1 | -5,172 | +404 |

Rolling the untested side (buy back the decayed spread, re-sell it closer to
spot to finance the tested side) **cut max loss to the lowest of the three but
destroyed the edge.** Splitting the dynamic trades: those that never rolled won
100% (+832 avg); those that rolled won only 37% (-819 avg) — the classic
whipsaw, where pulling the untested spread toward a moving market means a
reversal hits the side you just moved into danger.

The chosen rule (roll early at 50% of the distance, aggressively to 80% of
tested premium) is near the worst cell of the roll-parameter grid; waiting to
70% and rolling only to 60% credit raises the dynamic total from +404 to
+38,300 — but **even the best dynamic variant still loses to simply managing
winners** (+77,393). Conclusion: rolling the untested side is a tail-shape tool
(lower max loss, lower win rate, ~zero net edge after whipsaws and
commissions), not an edge-adder — at least as a mechanical rule. Caveats: this
isolates one tool (real practitioners also roll in time, adjust the tested
side, and only sell condors when IV is elevated — the sim sells always-on
including today's low 15% vol), and it tests the rule, not skilled discretion.
Mechanical management (take profit at 50%, exit 21 DTE) was the risk-adjusted
sweet spot: ~88% of static's profit with materially smaller tails.

## Finding a profitable configuration at 100,000 SEK

[`experiments/strategy_search.py`](experiments/strategy_search.py) sweeps 162
variants of the strategy at 100,000 SEK capital (top-N, momentum lookback,
rebalance cadence, rank buffer, stop) — full results in
[`experiments/sweep_results_100k.csv`](experiments/sweep_results_100k.csv).

Key findings (2015–2026):

| Config | Monthly SEK | CAGR | Max DD | Sharpe | Fees |
|--------|------------|------|--------|--------|------|
| Original params @ 50k | 292 | 5.3% | -26.7% | 0.40 | 60,138 |
| Original params @ 100k | 1,553 | 11.0% | -24.6% | 0.70 | 67,357 |
| **Top-4, mom 12w, weekly, buffer 4, stop 15%** @ 100k | **2,919** | **15.5%** | -28.7% | 0.85 | 48,581 |
| Top-4, mom 12w, 4-weekly, buffer 4, stop 10% @ 100k | 2,873 | 15.4% | -26.3% | 0.86 | 32,803 |

- **Capital size alone matters:** doubling capital to 100k quintuples monthly
  profit, because Nordnet's 39 SEK minimum commission stops dominating.
- **The rank buffer is the biggest single improvement:** holding a position
  while it stays within top-N+4 (instead of selling the moment it leaves the
  top N) cuts trades from ~1,540 to ~400–570 and raises returns.
- **Honest caveat — regime dependence, not a money machine:** in a split-half
  test, all top variants earned most of their profit in 2015–2020. In
  2021–2026 the best variant averaged ~1,100 SEK/month, and none sustained
  2,000+. 39 of 162 combos beat 2,000 SEK/month over the full period, so the
  result is a robust *family* (small N + buffer + trend filter), but forward
  returns in a weak regime should be expected to be well below the full-period
  average. Grid-search winners always carry some look-ahead selection bias.

## Running it

```bash
pip install requests pandas numpy matplotlib
python backtest.py
```

Outputs are written to `./outputs/` (override with the `OUTPUT_DIR` env var):

- `backtest_results.png` — equity curve + drawdown
- `equity_curve.csv` — weekly equity, cash, position count
- `trade_log.csv` — every buy / stop-loss / rank-exit with fees

The console prints CAGR, max drawdown, Sharpe, trade counts, total fees paid,
and average implied monthly SEK profit (vs. a 2,000 SEK/month target).

## Network requirement

`backtest.py` pulls adjusted daily closes from **Yahoo Finance's public chart API**
using `requests`. It needs outbound HTTPS access to:

- `query1.finance.yahoo.com`, `query2.finance.yahoo.com`
- `fc.yahoo.com`, `finance.yahoo.com`

In a restricted/sandboxed environment where these hosts are blocked at the egress
gateway, the download step will fail (HTTP 403 on CONNECT) and no results can be
produced. Run in an environment whose network policy allows the hosts above.

## Notes on the strategy (structural — not bugs)

- **Fee drag.** The strategy re-ranks weekly and sells + rebuys any name dropping
  out of the top 6. With 6 slots on a 50,000 SEK account (~8,000 SEK each) the
  39 SEK minimum commission is a meaningful round-trip cost, and weekly turnover
  compounds it. Watch `Total fees paid` relative to net profit.
- **Ticker validity.** All symbols are structurally valid `.ST` tickers. If any
  come back "possibly delisted" on a live run, check for renamed variants
  (e.g. `EPI-A.ST`, `NDA-SE.ST`, `ADDT-B.ST`).
