# Project Overview

## Thesis

Polymarket daily temperature markets can be researched as a station-local forecasting and market microstructure problem. The working hypothesis is that public weather data, exact settlement source parsing, live station observations, and executable public CLOB order books can identify mispriced paper entries before the market fully incorporates station-specific information.

The current project is a local Python MVP for paper research only. It fetches public market/weather data, scores daily high-temperature buckets, writes SQLite decision snapshots, simulates paper fills, and renders static/local dashboard artifacts. It does not trade.

## Current Architecture

- [scanner.py](../scanner.py) owns the CLI, schema migration, market extraction, signal scoring, paper ledger, evaluation, and tuning command.
- [weather_sources.py](../weather_sources.py) contains bounded read-only adapters for public weather families such as NWS, METAR/AviationWeather, IEM, NCEI, Meteostat stubs, and Open-Meteo enrichment.
- [features.py](../features.py) builds flat no-lookahead feature dictionaries from decision-time source snapshots.
- [tuning_evaluator.py](../tuning_evaluator.py) reads the paper ledger and goal config to report readiness, source coverage, feature coverage, metric availability, paper-forward approval status, and persisted tuning iterations.
- [render_brain_performance.py](../render_brain_performance.py) renders the local paper performance dashboard HTML plus a JSON sidecar.
- [goals/paper_weather_edge_v1.yaml](../goals/paper_weather_edge_v1.yaml) declares paper-only tuning goals, gates, guardrails, current runtime values, and allowed proposal-only tunables.
- [research/data_sources_and_tunable_variables.md](../research/data_sources_and_tunable_variables.md) is the detailed source/feature/tunable backlog.

## Runtime Artifacts

- `paper_weather.sqlite3` stores runs, markets, signals, source snapshots, training rows, paper orders/fills/positions, settlements, and account snapshots.
- `report.html` is the scanner-level signal report.
- `tuning_iterations.jsonl` records each `scanner.py tune` iteration as proposal-only JSONL.
- The dashboard renderer writes HTML/JSON snapshots from the local ledger. See [frontend and ops](frontend-and-ops.md).

## What GPT Pro Should Focus On

- Improve settlement source parsing and station confidence before model sophistication.
- Keep feature and label separation strict.
- Prefer adding read-only evidence and diagnostics over adding action paths.
- Treat tuning outputs as config proposals only, gated by labeled paper outcomes and human approval.
