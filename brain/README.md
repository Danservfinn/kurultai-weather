# Polymarket Weather Edge Wiki

Repo-local handoff notes for reviewing this paper-only weather market scanner with GPT Pro or another coding agent.

Start here:

- [Project overview](project-overview.md)
- [Market mechanics](market-mechanics.md)
- [Data sources](data-sources.md)
- [Features and labels](features-and-labels.md)
- [Tuning and metrics](tuning-and-metrics.md)
- [Frontend and ops](frontend-and-ops.md)
- [Edge hypotheses](edge-hypotheses.md)
- [Safety boundaries](safety-boundaries.md)

Primary code and research references:

- [Scanner CLI and SQLite ledger](../scanner.py)
- [No-lookahead feature builder](../features.py)
- [Read-only weather adapters](../weather_sources.py)
- [Tuning readiness evaluator](../tuning_evaluator.py)
- [Performance dashboard renderer](../render_brain_performance.py)
- [Paper-only tuning goal](../goals/paper_weather_edge_v1.yaml)
- [Source and tunable research brief](../research/data_sources_and_tunable_variables.md)
- [Root README](../README.md)

The project is intentionally paper-only. It does not contain wallet loading, private key handling, transaction signing, authenticated trading, or live order placement.
