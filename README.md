# Four-hour crypto paper research

This runs simulated BTCUSDT, ETHUSDT and SOLUSDT trades using public Binance futures market data. It needs no exchange credentials, AI account, Telegram token or paid service. The dedicated runner accepts public GET endpoints only. It cannot send exchange orders. Existing paper books are not included.

The strategy remains experimental. Its unchanged 2023 backtests returned +9.33% at normal modeled costs and +8.98% at higher costs, with 43 trades. It still failed the predeclared 50-trade threshold; the earlier 2024 loss remains relevant. Historical results do not establish future returns.

## Frozen strategy and execution

Signals use completed four-hour candles: a new close crossing the previous 20-bar high/low channel, EMA50 direction, volume at least the previous 20-bar average, and 3ATR/price at least 0.006. Stops are 3ATR and targets 3R from the modeled fill. Initial simulated cash is 10,000 USDT; risk is 0.25%, notional cap 1,000, maximum two positions, daily loss limit 2%, combined reserved stop risk 1%, persistent peak drawdown latch 5%.

AI and historical macro/news/sentiment gates are absent from this fixed technical experiment. The original full MVP remains separate. Fresh quote/spread checks and a five-minute maximum setup age are operational gates added to the paper trial. They can reject entries admitted by a historical backtest. Late signals are consumed and never traded later. Funding uses published settlement records with restart-safe deduplication and final reconciliation.

Exits are monitored with observed marks and completed fifteen-minute candles between four-hour signals. Simulated commission is 0.05% per side and adverse fills 0.02% per side. Intrabar paths, depth, gaps, publication delays and exact execution remain approximations. Limits do not guarantee a loss ceiling. Local polling defaults to 60 seconds; scheduled runs check approximately every five minutes and may be delayed.

## Local setup

Use Python 3.12:

```text
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m bot.slow_paper --offline --once
python -m bot.slow_paper --preflight
python -m bot.slow_paper --once
python -m bot.slow_paper
```

Activate the virtual environment before using `python`, or use its executable explicitly. Offline runs use `data/slow-offline.sqlite`; normal runs use `data/slow-paper.sqlite`. These ledgers cannot be adopted from another experiment. For a persistent server use `--db /path/to/slow-paper.sqlite --poll 60`. No `.env` is loaded by this runner. Existing MODE/AI/secret environment settings cannot turn it into demo/live execution.

## Free public GitHub scheduled option

1. Create a new **public** repository named `crypto-paper-research` and upload this reviewed package. It contains only code, public market test fixtures and a new empty simulated checkpoint.
2. Keep the repository public and use only the supplied standard Ubuntu runners. Jobs refuse to run in a private repository. No Actions artifacts, dependency caches, paid runners or paid fallbacks are configured.
3. Manually run **Public-data connectivity and paper tests** from Actions. It must pass from the actual hosted runner, not just a laptop. Some hosts cannot reach Binance's public API; HTTP failures stop the trial. No proxy or location-bypass fallback is configured.
4. Set the repository Actions variable `PAPER_RUN_ENABLED` to `true` only after the hosted preflight passes. Then manually run **Four-hour paper research** once and inspect the result. The schedule checks approximately every five minutes in UTC.
5. To pause the experiment set `PAPER_RUN_ENABLED` to `false`. Pausing suspends simulated exit monitoring too; resume after reviewing any missing history. Keep the state file and history; never silently reset cash or the drawdown latch.

Code/state publication makes the files public. No existing `.env`, credentials, user account information, private ledgers or old balances are included. `state/paper.json` holds only this new paper book: simulated cash/positions, published funding payments and bounded diagnostic events. It keeps all trade/funding/halt evidence. SQLite files and local data are ignored by Git. A temporary database is rebuilt for each scheduled cycle. Validated JSON checkpoints are saved atomically; process/workflow locks prevent concurrent writers. Only `state/paper.json` is staged by the workflow. Push failure invalidates persistence for that run and requires review; no forced push or automatic ledger reset is used.

Checkpoints retain daily and portfolio latches and funding deduplication keys. Missing, malformed, mismatched or credential-bearing state is rejected. Identical checkpoints are not rewritten. `python tools/status.py` reads simulated cash, trades and halts without network calls. The Actions run summary is the heartbeat; no external notifications are sent by the bot. GitHub's own account notifications may apply.

Scheduled Actions jobs are not a continuously running server. GitHub may delay/drop jobs; schedules can disable after prolonged repository inactivity. A setup older than five minutes is skipped, and gaps in exit history trigger a safety halt. Treat this as a forward research sample under scheduled execution, not an exact replacement for a 60-second server or funded trading. Runtime free pricing remains conditional on public-repository standard runners and platform policy.

## Hosting verification

Google's eligible e2-micro compute allowance does not cover always-on external IPv4: the checked price is $0.005/hour with only one free address-hour per month, approximately $3.65/month. The configured Binance API hosts had no IPv6 DNS records when checked. IPv6 alone therefore did not establish usable zero-cost connectivity, and no paid NAT or IPv4 VM was deployed. A Google systemd template is included in the original MVP for a separately funded server; it is not a verified zero-cost deployment.

Verified official sources on October 6, 2026:

- Google Free Tier: https://docs.cloud.google.com/free/docs/free-cloud-features
- Google external IP pricing: https://cloud.google.com/vpc/network-pricing
- Public standard GitHub runner pricing: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Schedule limits: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

This package is prepared locally. It is not deployed or collecting a cloud paper sample until a repository exists, the files are published, hosted connectivity passes and the variable is explicitly enabled. Review the first month for operational correctness; a meaningful trade sample may take much longer. Do not retune the frozen strategy on the new observations.
