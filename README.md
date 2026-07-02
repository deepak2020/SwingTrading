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

## Running it

```bash
pip install yfinance pandas numpy matplotlib
python backtest.py
```

Outputs are written to `./outputs/` (override with the `OUTPUT_DIR` env var):

- `backtest_results.png` — equity curve + drawdown
- `equity_curve.csv` — weekly equity, cash, position count
- `trade_log.csv` — every buy / stop-loss / rank-exit with fees

The console prints CAGR, max drawdown, Sharpe, trade counts, total fees paid,
and average implied monthly SEK profit (vs. a 2,000 SEK/month target).

## Network requirement

`backtest.py` pulls price history from **Yahoo Finance** via `yfinance`. It needs
outbound HTTPS access to:

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
