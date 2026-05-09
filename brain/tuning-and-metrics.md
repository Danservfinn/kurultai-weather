# Tuning and Metrics

## Current Goal

[goals/paper_weather_edge_v1.yaml](../goals/paper_weather_edge_v1.yaml) is the declarative paper-only tuning goal. It requires:

- `mode: paper_only`
- `paper_only: true`
- `promotion: propose_only`
- `live_trading: false`
- `wallet_required: false`
- `order_placement: false`
- `live_money_deployment: false`

The current readiness gates are 300 training rows, 300 labeled rows, and 14 calendar days.

## Tuning Evaluator

[tuning_evaluator.py](../tuning_evaluator.py) reads the SQLite ledger, goal config, and runtime tunables. It reports:

- Evidence counts and gate status.
- Source-family coverage.
- Feature-family coverage and no-lookahead leakage count.
- Available performance metrics.
- Current tunables and allowed proposal ranges.
- Proposed tunables only when paper-forward gates allow proposals.
- Paper-forward approval status.
- Safety flags showing no wallet, no live trading, no signing, no deployment, and no order placement.

## Persistent Iterations

Each `python3 scanner.py tune --goal goals/paper_weather_edge_v1.yaml` call appends a JSONL record to `tuning_iterations.jsonl`.

Each iteration includes:

- Timestamp and iteration ID.
- Status, such as `insufficient_data`, `ready_for_proposals`, or `approved_for_paper_forward_test`.
- Evidence counts.
- Gate details.
- Target metrics.
- Current tunables.
- Proposed tunables, if any.
- Available performance metrics.
- Approval status.
- Sanitized paper-only safety flags.

Unsafe log entries are sanitized when loaded for display and cannot turn on trading controls.

## Metrics

Primary metric: realized return percentage from the paper ledger.

Secondary metrics:

- Brier score when enough labeled rows have model probabilities and binary labels.
- Maximum drawdown from paper account snapshots.
- Unresolved rate from labeled vs. unlabeled training rows.

Tuning should stay proposal-only until the paper ledger has enough labeled evidence to evaluate whether an apparent edge survives spread, liquidity, station ambiguity, and stale data.
