# Pre-registered predictions for the 24h Demo run

Written before the run starts, so the result cannot be reinterpreted after the
fact. The strategies are unchanged from the 373-trade history analysed earlier
in this research; nothing built during the backtest work touches them.

The point of the run is not to learn whether the bot is profitable — that is
already measured. It is an out-of-sample test of **the research tooling**: if
live execution lands near these numbers, the cost model and engine that all the
rejections rest on are validated. If it diverges materially, those conclusions
need review.

## Predictions

| Quantity | Predicted | Basis |
|---|---|---|
| Round-trip cost | ~11.0 bps | measured at exactly 11.00 over 253 exact-accounting trades |
| Expectancy | ~-0.12 USDT/trade | -0.11977 over 373 terminal executions |
| Win rate | 33-36% | 35.12% over 373, 33.60% over the exact cohort |
| Completed trades in 24h | 30-50 | the 10h run on 2026-08-03 completed 17 |
| Net PnL | -3 to -6 USDT | expectancy x trade count |
| Median holding time | 4-6 minutes | 275s median over the exact cohort |
| Exit mix | stale ~45%, stop ~30%, TP ~18% | 118/78/46 of 253 |
| Dominant strategy | Liquidation + OI | 135 + 114 of 253 exact trades |

## What each outcome means

**Close to predicted** — the cost model and the backtest engine describe reality.
Every rejection in this research stands on firmer ground than before.

**Materially better** — something changed in the market or in the runtime since
2026-08-03. The gap needs explaining before any conclusion is trusted.

**Materially worse** — likely costs above the modelled 11 bps, which would mean
the backtests were optimistic and the rejections were, if anything, too gentle.

## Safety

Demo account only. `demo_v2_soak.ps1` sets `APP_ENV=demo`, `TEST_MODE=false`,
`EXECUTION_MODE=BYBIT_DEMO`, `BYBIT_DEMO_TRADING_ENABLED=true` in a child
process; `.env` is not modified. Preflight before launch reported `ok: true`,
live/mainnet/testnet execution blocked, kill switch clear, zero open orders and
zero unresolved executions.

## Provenance

This section was appended when the file was moved under version control; the
predictions above are unchanged from what was written before the run.

The commit timestamp is *after* the results were seen, so the commit itself
proves only that this content exists now. The evidence for pre-registration is
the filesystem timestamp, recorded here before the move:

| Event | UTC |
|---|---|
| This file written | 2026-08-17 21:49:21 |
| Soak process launched | 2026-08-17 21:49:34 |
| First trade opened | 2026-08-18 01:01:47 |

Thirteen seconds before launch, three hours before the first fill. Stated
plainly because a pre-registration nobody can check is worth nothing.
