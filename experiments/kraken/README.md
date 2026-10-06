# Independent Kraken futures paper experiment

This is a new forward research trial, `kraken-slow-breakout-4h-v1`, using public Kraken Futures data for `PF_XBTUSD`, `PF_ETHUSD` and `PF_SOLUSD`. Internal labels are BTCUSD, ETHUSD and SOLUSD. Accounting is **simulated USD**, not USDT. Initial cash is 10,000 simulated USD. No exchange account, keys, funded orders, AI or external alerts are used. It accepts only specific public GET endpoints on `https://futures.kraken.com`, with redirects and environment proxies disabled.

The Binance book, code and disabled workflow are separate. Never combine these observations with Binance historical results. This is a new exchange/data-source hypothesis, without a validated profitable Kraken backtest. Its purpose is to collect a forward sample and check operational behavior. Never retune the frozen rules against that sample.

## Fixed rules and limits

Completed four-hour trade candles, previous 20-bar channel crossing, EMA50 direction, volume at least the prior 20-bar average, and 3ATR/price at least 0.006. Stop 3ATR and target 3R from the modeled fill. Risk 0.25% of equity, maximum 1,000 USD notional per position, maximum two positions, combined reserved stop risk 1%, daily loss latch 2%, persistent peak drawdown latch 5%. Limits cannot guarantee a loss ceiling. NO_TRADE applies when signals, costs, timing, data or risk checks fail.

Only completed candles are used. Setups more than five minutes old are consumed without entering. Repeated runs and restarts cannot replay entries. Quote timestamps, spread, deviation and contract precision are checked. Kraken linear contract quantity is in base units; tick and quantity precision come from public instrument metadata. Candle histories must be contiguous; a missed exit-history window halts the book.

Exits use completed fifteen-minute trade candles and current mark observations. Mark versus trade prices, intrabar order, gaps, depth and execution latency are approximations. If both SL and TP occur in a bar, SL takes priority. Simulated fees are 0.05% per side and adverse fills 0.02% per side; these are fixed research assumptions, not a statement of your account's fees. No real collateral conversion is modeled.

## Kraken funding accounting

Kraken funding accrues continuously at each published hourly rate, rather than the Binance discrete settlement model. For these linear USD instruments, the adapter uses `fundingRate` in USD per base unit per hour and prorates it for actual modeled time held. The timestamp identifies the start of the hour in which that rate is active. The latest hourly history matches the current ticker rate; no predicted rates are used. Missing hourly coverage blocks entries and preserves unresolved closed-trade funding for review.

The shared eight-column funding checkpoint schema uses `funding_time` as the **hour start**, `rate` as **USD/unit/hour**, `mark=1` as a unit conversion, and `amount` as the accumulated payment for that hour. It is not a price of the asset. Only the difference from the previously applied cumulative payment affects cash. Atomic updates, checkpoint restoration and immutable-rate checks prevent duplicate charges. Partial entry and exit hours are prorated. Funding is deducted from paper cash as it accrues so risk checks include it. A modeled bar exit charges through its bar close, whose actual intrabar exit time is unknown. This timing approximation can differ from real exchange funding.

## Run locally

Use Python 3.12 and activate your virtual environment:

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m bot.slow_paper --preflight
python -m bot.slow_paper --offline --once
python -m bot.slow_paper
```

Run from this experiment directory. No `.env` is loaded and environment settings cannot enable trading. Local ledgers are `data/kraken-paper.sqlite` and `data/kraken-offline.sqlite`; neither is published. Persistent services omit `--checkpoint`. Scheduled jobs restore a validated JSON checkpoint into an empty temporary SQLite database, run one cycle, and atomically export it. Missing or wrong-experiment checkpoints cannot reset the balance.

## GitHub operation

The separate workflows are **Kraken paper preflight** and **Kraken four-hour paper research**. The preflight must pass on hosted runners before setting the repository variable `KRAKEN_PAPER_RUN_ENABLED=true`. The supplied schedule checks approximately every five minutes; jobs can be delayed or dropped. It is not a continuous server. Only `experiments/kraken/state/paper.json` is committed by this workflow, using a separate writer lock and no forced push. Pausing stops simulated exits too. Re-enable only after reviewing missing history.

Public standard Ubuntu GitHub runners are the zero-cost option used here. No artifacts, dependency caches, paid runners or paid fallback are configured. Jobs refuse to run in private repositories. Source and simulated state are public. Check the workflow's logs and account summary for completion and failures. No Telegram or other messages are sent by this bot. A data failure produces a failed run while retaining the diagnostic checkpoint. A 10MB state-size limit requires archive/review before continuing.

Official API references checked October 7, 2026:

- https://docs.kraken.com/api-reference/candles/market-candles
- https://docs.kraken.com/api-reference/market-data/get-instruments
- https://docs.kraken.com/api-reference/market-data/get-tickers
- https://docs.kraken.com/api-reference/market-data/get-orderbook
- https://docs.kraken.com/api-reference/historical-funding-rates/historical-funding-rates
- https://support.kraken.com/articles/360022835911-perpetual-contract-funding-rate

Account trading availability and eligibility are outside this public-data simulation. No restricted access is bypassed. An HTTP access denial stops the experiment.
