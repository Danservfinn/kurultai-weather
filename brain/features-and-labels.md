# Features and Labels

## No-Lookahead Contract

Features must only use records timestamped at or before the decision time. Future source records are excluded and counted. Label/final-outcome fields are stripped from feature input records.

The implementation lives in [features.py](../features.py). The scanner persists feature payloads in `training_rows.features_json`.

## Feature Families

Current feature families:

- Settlement source/station confidence.
- Forecast ensemble and spread placeholders.
- Live observation, touched-threshold, and impossible-outcome state.
- Station-local time and day-boundary context.
- Order-book microstructure and executable depth.
- Cross-outcome ladder consistency.
- Source quality and missingness.
- Portfolio/risk context.
- Event key, latent final-high distribution, strategy family, contract type, and payout-map hash.
- No-lookahead audit fields.

The detailed feature backlog is in [research/data_sources_and_tunable_variables.md](../research/data_sources_and_tunable_variables.md).

## Labels

Labels belong in separate label/evaluation fields, not in `features_json`.

Current label-related fields in `training_rows` include:

- `label_status`
- `label_value`
- `label_source`
- `labeled_at`

Delayed final labels come from the labeler path in [scanner.py](../scanner.py). NOAA/NCEI daily summaries are treated as final when a daily high is available. NWS station observations and IEM/ASOS daily highs are supporting/provisional evidence unless the labeler later obtains a final NOAA/NCEI row. Intraday station reads can inform touched/impossible states, but they are not automatically final labels unless the market rules and source match.

`label_attempts` stores the provenance layer for every delayed attempt: provider, family, source URL/status, fetch timestamp, station confidence, final/provisional high, threshold bounds, label value, status, and reason. Final labels update only the `training_rows.label_*` columns; they do not rewrite historical feature snapshots.

## Training Row Snapshot Design

Each row should preserve enough IDs to reconstruct decision context:

- Forecast snapshot ID.
- Station observation ID.
- Order-book snapshot ID.
- Market metadata fields and settlement hints.
- Signal classification and reason.
- Feature JSON built at decision time.
- Label fields filled later.
- Label-attempt provenance rows linked by market/outcome/target/station.
- Lifecycle attribution rows linking candidate, signal, simulated fill, label, settlement, calibration row, and strategy family.

This separation is the main guard against accidental lookahead and overfitting.

## Lifecycle and Calibration

Paper-only attribution tables connect the full research path:

- `lifecycle_attribution` links candidate key, event key, strategy family, signal snapshot, simulated order/fill/position, final label attempt, paper settlement, and calibration row.
- `calibration_rows` stores post-label prediction probability, binary label, Brier score, log loss, event-time bucket, strategy family, and contract type.
- `events`, `contract_payouts`, and `event_exposure_snapshots` preserve event keys, latent final-high distribution, payout mapping, and paper exposure.

These tables are for research evaluation only. They do not enable wallets, live trading, order placement, signing, or secrets.
