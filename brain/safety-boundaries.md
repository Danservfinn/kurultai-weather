# Safety Boundaries

This repo is paper-only research tooling.

## Must Remain Absent

- Wallet loading or wallet-address requirements.
- Private keys, seed phrases, session cookies, or exchange credentials.
- API keys committed to the repo.
- Transaction signing.
- Authenticated trading endpoints.
- Live order creation, live order cancellation, or order placement.
- Live-money deployment.
- Dashboard controls that can trade, sign, deploy, or promote tuning automatically.

## Allowed Behavior

- Fetch public market pages and public order-book data.
- Fetch public weather metadata, forecasts, and observations.
- Store local SQLite observations and simulated paper ledger rows.
- Export training rows.
- Render local HTML/JSON dashboards.
- Append proposal-only tuning iteration JSONL records.
- Propose config changes after paper-forward gates pass, without applying them automatically.
- Fetch delayed public weather labels and settle simulated paper positions to $1/$0 outcomes.

## Guardrails in Code

- [scanner.py](../scanner.py) has a `ensure_paper_only_guard` check for prohibited live-trading, wallet, private-key, API-key, and order-placement options.
- [goals/paper_weather_edge_v1.yaml](../goals/paper_weather_edge_v1.yaml) requires paper-only guardrails and `promotion: propose_only`.
- [tuning_evaluator.py](../tuning_evaluator.py) reports safety flags and sanitizes loaded tuning iteration logs so unsafe records are marked rejected.
- [render_brain_performance.py](../render_brain_performance.py) displays safety labels and does not expose execution controls.
- [scanner.py](../scanner.py) keeps delayed labels in `training_rows.label_*` and `label_attempts`, separate from no-lookahead `features_json`.

## Review Rule

Any change that touches market execution, credentials, source authentication, cron automation, or tuning promotion must be reviewed as a safety-sensitive change. The default answer should be to add more paper evidence and diagnostics, not a live trading path.
