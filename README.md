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
