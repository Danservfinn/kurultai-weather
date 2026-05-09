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
- No-lookahead audit fields.

The detailed feature backlog is in [research/data_sources_and_tunable_variables.md](../research/data_sources_and_tunable_variables.md).

## Labels

Labels belong in separate label/evaluation fields, not in `features_json`.

Current label-related fields in `training_rows` include:

- `label_status`
- `label_value`
- `label_source`
- `labeled_at`

Delayed final labels should come from settlement-grade or high-confidence post-resolution sources, such as exact market resolution data or NOAA/NCEI station summaries where applicable. Intraday station reads can inform touched/impossible states, but they are not automatically final labels unless the market rules and source match.

## Training Row Snapshot Design

Each row should preserve enough IDs to reconstruct decision context:

- Forecast snapshot ID.
- Station observation ID.
- Order-book snapshot ID.
- Market metadata fields and settlement hints.
- Signal classification and reason.
- Feature JSON built at decision time.
- Label fields filled later.

This separation is the main guard against accidental lookahead and overfitting.
