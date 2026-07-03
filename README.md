# Polymarket Weather Paper Scanner

Local, stdlib-only Python MVP for autonomous paper trading research on Polymarket weather markets.

It does not trade, sign transactions, use private keys, or place orders. It only fetches public data, estimates weather outcome probabilities, stores observations in SQLite, and writes a local HTML report.

## What It Does

- Fetches a Polymarket weather/search page and extracts the embedded Next.js `__NEXT_DATA__` JSON.
- Recursively extracts weather-like markets with outcomes and outcome prices.
- Captures market rules, resolution/source text, source links, Weather Underground links, and station IDs when present.
- Infers a city and target date from market titles.
- Geocodes cities with Open-Meteo.
- Fetches Open-Meteo daily high temperature forecasts in Fahrenheit.
- Attempts best-effort Weather Underground station reads for daily/current observed highs, while degrading cleanly when blocked or unavailable.
- Scores numeric temperature buckets with a normal distribution.
- Classifies threshold, exact-high, and range/bucket contracts and computes conservative early/final settlement states from observed station highs and station-local day completion.
- Uses observed settlement-station highs, when available, to flag absorbing threshold touches, exact-high impossibility after overshoots, and range/bucket impossibility after upper-bound breaches.
- Fetches public CLOB order books when token IDs are present and records a small-size executable paper entry estimate. It falls back to displayed prices.
- Classifies rows as `paper_buy`, `watch`, or `skip` using source confidence, spread, edge, and dust/settled filters.
- Separates strategy families for `latency_absorbing_state`, `complement_arb`, `ladder_inconsistency`, `settlement_source_edge`, `diurnal_nowcast`, `forecast_distribution_directional`, plus `watch`, `skip`, and `unknown`.
- Detects same-market YES/NO complement underrounds from executable asks only, with depth and stale-quote checks, separate from forecast edge.
- Monitors ladder distributions for no-arb violations, stale/thin linked outcomes, discontinuities, and candidate correction trades.
- Records runs and paper signals in `paper_weather.sqlite3`.
- Records bounded forecast, station-observation, order-book, and training-row snapshots needed to reconstruct decision-time context.
- Persists event keys, latent final-high distributions, contract payout mappings, lifecycle attribution rows, and calibration rows so candidates can be traced through signal, simulated fill, label, paper settlement, and strategy family.
- Maintains a simulated paper-only portfolio ledger with paper orders, fills, positions, settlements, and account snapshots.
- Attempts delayed, read-only outcome labels for prior daily-temperature rows, preferring NOAA/NCEI daily highs and recording IEM/NWS evidence as provisional support.
- Settles open paper positions from final labels to simulated $1/$0 payouts while preserving cash, equity, realized PnL, and settlement provenance.
- Evaluates prior `paper_buy` candidates by default against the latest observed same title/outcome market price.
- Provides read-only adapter stubs for NWS, AviationWeather/METAR/IEM, NOAA/NCEI delayed labels, Meteostat, Open-Meteo metadata enrichment, and disabled-by-default commercial weather providers.
- Builds no-lookahead feature dictionaries for settlement/source confidence, forecast spread, live observations, local time, order-book microstructure, ladder consistency, source quality, and portfolio context.
- Generates `report.html` with source confidence, settlement source/station, execution source, edge, reason, and signal class.

## Usage

```bash
python3 scanner.py scan
python3 scanner.py summary
python3 scanner.py evaluate
python3 scanner.py health
python3 scanner.py stations
python3 scanner.py ladders
python3 scanner.py portfolio
python3 scanner.py label
python3 scanner.py export-training
python3 scanner.py tune
python3 render_brain_performance.py
python3 serve_brain_projects.py
```

Optional flags:

```bash
python3 scanner.py scan --url "https://polymarket.com/markets?search=weather"
python3 scanner.py scan --city "New York" --sigma 4.0
python3 scanner.py scan --paper-size 5 --edge-threshold 0.08 --max-spread 0.12
python3 scanner.py scan --disable-ledger
python3 scanner.py summary --limit 20
python3 scanner.py evaluate --limit 20
python3 scanner.py evaluate --all-signals
python3 scanner.py stations --city London
python3 scanner.py ladders --run-id 1
python3 scanner.py portfolio --snapshot
python3 scanner.py label --limit 25 --min-age-days 2
python3 scanner.py label --dry-run --limit 0
python3 scanner.py label --enable-ncei --enable-nws --enable-iem
python3 scanner.py export-training --output training_rows.csv --include-features
python3 scanner.py tune --init-goal
```

Environment overrides:

```bash
POLYMARKET_WEATHER_URL="https://polymarket.com/markets?search=weather" python3 scanner.py scan
WEATHER_SIGMA_F=3.5 python3 scanner.py scan
```

## Outputs

- `paper_weather.sqlite3` stores:
  - `runs`
  - `markets`
  - `signals`
  - `forecast_snapshots`
  - `station_observations`
  - `orderbook_snapshots`
  - `training_rows`
  - `label_attempts`
  - `events`, `contract_payouts`, `event_exposure_snapshots`
  - `lifecycle_attribution`, `calibration_rows`
  - `paper_accounts`, `paper_orders`, `paper_fills`, `paper_positions`, `paper_settlements`, `paper_account_snapshots`
- `report.html` shows recent scored signals and model-vs-market edges.
- `/Users/kublai/brain/projects/polymarket-weather-engine-performance.html`, generated by `render_brain_performance.py`, shows the paper-only $1,000 bankroll dashboard, evidence progress, latest runs, row counts, and recent simulated orders/fills/positions.
- `/Users/kublai/brain/projects/polymarket-weather-engine-performance.json` is the sidecar dashboard snapshot. The HTML embeds an initial snapshot for `file://` viewing and polls this JSON every 30 seconds when served over local HTTP. The snapshot includes strategy PnL, edge buckets, Brier/log loss by contract type, fill realism, label delay, stale quote/reaction lag, ladder violations, station/source ambiguity, time-to-local-close, lifecycle funnel, and event exposure/latent summaries.
- `training_rows.csv`, when exported, contains decision-time features for later paper-model work.
- `goals/paper_weather_edge_v1.yaml` defines a paper-only tuning scaffold. Post-label calibration and Brier/log loss by event-time, strategy family, and contract type are primary research metrics; return remains secondary. `tune` reports readiness for future config-only tuning; it does not deploy or trade.
- `weather_sources.py` contains bounded, cached, read-only source adapters. Optional/commercial providers are disabled by default and never require credentials.
- `features.py` contains decision-time feature construction. Label/final outcome fields stay separate and are not included in feature payloads.
- `run_labeler_and_render.sh` sources `runtime_tunables.env`, runs delayed labels with enabled free/read-only providers, settles final paper labels, and refreshes the dashboard artifacts.

The SQLite schema is migrated in place with nullable `ALTER TABLE` additions, so older `runs`, `markets`, and `signals` rows remain readable.

## Safety Notes

This is intentionally paper-only research code. It has no wallet logic, no live order placement, no signing, no key loading, and no live trading path. The CLOB integration is read-only: it fetches public books to estimate an executable paper entry for a small hypothetical size. The portfolio ledger records simulated paper orders/fills only after paper filters pass.

The probability model is deliberately simple: Open-Meteo forecast high temperature is treated as the mean of a normal distribution, with `--sigma` as forecast uncertainty in Fahrenheit. Treat outputs as research leads, not trading advice.

## Data Source Provenance and No-Lookahead

Every source adapter returns provider, endpoint, status, fetch time, cache TTL, timeout, and read-only provenance. NWS, METAR/IEM, NCEI/CDO, Meteostat, model-specific Open-Meteo, Polymarket history, and commercial providers are optional runtime families. Weather Underground remains disabled unless `--enable-wu` or `ENABLE_WU=1` is set.

Decision-time features only use records timestamped at or before the decision time. Future records are excluded and counted in `excluded_future_source_count`. Delayed labels, NCEI final summaries, post-settlement values, and evaluation results belong in label/evaluation fields only; they must not be copied into `features_json`.

## Delayed Labels and Paper Settlement

`python3 scanner.py label` scans unresolved prior daily-temperature training rows, skips future/not-aged rows, and retries unresolved groups only after `LABEL_RETRY_AFTER_HOURS`.

Source priority:

- NOAA/NCEI daily summaries via tokenless public endpoints are final labels when a daily high is found.
- NWS station observations and IEM/ASOS daily highs are stored as provisional supporting evidence when enabled.
- Disabled, missing, or unavailable sources are recorded as skipped/pending/error attempts without failing the command.

Final labels update only `training_rows.label_status`, `label_value`, `label_source`, and `labeled_at`; the original decision-time `features_json` snapshot is never rewritten. When final labels exist, open paper positions with matching market/outcome rows settle to simulated $1/$0 payouts in `paper_settlements`.

## Settlement, Events, and Lifecycle Attribution

Settlement state is conservative:

- Threshold high >= K: YES becomes certain once the observed high reaches K; NO becomes impossible then. Final NO requires local-day completion or final source confirmation with no touch.
- Exact high = K: YES becomes impossible and NO certain if the observed high exceeds K. Touching K before local close is not final YES.
- Range/bucket: YES becomes impossible early if the observed high exceeds the upper bound. Final YES/NO requires the final high unless the state is mathematically absorbing.

Each candidate receives an `event_key` derived from city, target date, source, station, and rule hash. Event records store the latent final-high mean/sigma, observed high, local-day completion, contract count, and paper exposure. Contract payout rows store the payout mapping for each market/outcome. Lifecycle attribution rows connect candidate key, signal snapshot, simulated order/fill/position, final label, paper settlement, calibration row, and strategy family.

## Paper-Only Autonomous Mode

`scan`, `summary`, and `evaluate` can be run unattended by cron or a local wrapper. In autonomous mode, keep it paper-only:

- Run `python3 scanner.py scan` on a schedule.
- Review `report.html` for `paper_buy`, `watch`, and `skip` rows.
- Run `python3 scanner.py evaluate` after repeated scans. Evaluation intentionally warns until there are at least 14 calendar days and 300 paper candidates.
- Do not add wallets, private keys, order creation, transaction signing, or live CLOB placement to this scanner.

## Live Local Dashboard

The brain performance artifact can be opened directly as a file, but browsers usually block `file://` JSON polling. For live refresh, serve the brain projects directory with the stdlib-only helper:

```bash
python3 serve_brain_projects.py --port 8766
```

Then open `http://127.0.0.1:8766/polymarket-weather-engine-performance.html`. Port 8765 is reserved for the authenticated Brain gateway on the Mac mini; the dashboard viewer stays on localhost port 8766 and only polls the local JSON snapshot written by `render_brain_performance.py`. It does not expose trading, wallet, signing, API-key, or order-placement controls.

## Known Gaps

- Polymarket page JSON shape can change; extraction is best-effort and recursive.
- Weather Underground often blocks anonymous scraping, and some useful WU/API routes require credentials. The scanner does not use hardcoded credentials.
- Intraday settlement logic is conservative and only uses observed station highs when a station/date can be inferred and fetched.
- CLOB depth is estimated from public order book snapshots and may be stale by the time a report is read.
- City/date parsing and bucket parsing are heuristic; ambiguous titles should be reviewed before trusting a paper signal.
- The model is not calibrated against historical station errors, nearby-station lead/lag, humidity, sensor quirks, or market microstructure.

## Hermes Wrapper

The intended wrapper path is:

```text
/Users/kublai/.hermes/scripts/polymarket_weather_paper_scan.py
```

Wrapper content:

```python
#!/usr/bin/env python3
import subprocess
import sys

SCANNER = "/Users/kublai/polymarket-weather-edge/scanner.py"

if __name__ == "__main__":
    cmd = [sys.executable, SCANNER, "scan", *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd))
```
