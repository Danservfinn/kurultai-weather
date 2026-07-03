# Frontend and Ops

## Dashboard Outputs

[render_brain_performance.py](../render_brain_performance.py) renders a static paper performance dashboard:

- HTML dashboard.
- JSON sidecar snapshot.
- Embedded JSON fallback for `file://` viewing.
- Optional local polling when served over HTTP.

The dashboard reads the local SQLite ledger and tuning iteration JSONL. It does not mutate the paper ledger, call trading APIs, or expose controls for live orders.

## Visible Sections

The dashboard includes:

- Paper bankroll stats.
- Official paper account status, including the canonical clean account name, official fills, skipped order counts, and explicit idle reasons such as below-min-entry or shadow-only gates.
- Core row counts.
- Evidence progress.
- Labeling and settlement progress.
- Research metrics: strategy PnL, edge buckets, Brier/log loss by contract type, fill realism, label delay, stale quote/reaction lag, ladder violations, station/source ambiguity, time-to-local-close, lifecycle funnel, and event exposure/latent final-high summary.
- Equity trail.
- Tuning readiness and performance trace.
- Tuning iteration performance.
- Shadow proxy leaderboard; its `strategy_lab_rows` count includes both durable `training_rows` and paper-only `strategy_candidates` so broad shadow-lane fanout remains visible even before proxy labels exist.
- Data sources and feature tuning.
- Runtime tunables.
- Latest runs.
- Recent paper orders, fills, and positions.

The Tuning Iteration Performance section is expected to show insufficient-data tune attempts before labels exist.

The Labeling and Settlement Progress section shows final labeled rows, pending rows, unresolved/open paper positions, settled positions, recent label attempts, source coverage, and blockers. Provisional label attempts remain visible but do not settle paper positions.

## Live Local Refresh

[serve_brain_projects.py](../serve_brain_projects.py) can serve the output directory with Python stdlib HTTP on localhost port 8766. Port 8765 is reserved by the authenticated Brain gateway. The HTML polls the JSON sidecar when opened over local HTTP. File mode uses the embedded snapshot.

The normal render command is:

```bash
python3 render_brain_performance.py --output /private/tmp/pweather.html --json-output /private/tmp/pweather.json
```

The normal tuning command is:

```bash
python3 scanner.py tune --goal goals/paper_weather_edge_v1.yaml
```

The normal delayed-label refresh command is:

```bash
python3 scanner.py label --limit 25 --min-age-days 2
```

The cron-safe wrapper is:

```bash
./run_labeler_and_render.sh
```

## Operational Rules

- Keep scans unattended but paper-only.
- Refresh the dashboard after scans or tune checks.
- Do not add API keys, cookies, wallets, private keys, or signing flows to dashboard code.
- Treat the dashboard as observability and review UI, not an execution UI.
- Keep labeler provider flags in `runtime_tunables.env`; commercial/paid providers remain disabled by default.
- `PAPER_ACCOUNT_NAME=paper_account_v2_clean_post_gate` is the canonical official paper ledger cutover knob. Scanner account creation, order simulation, health, portfolio views, and dashboard rendering must respect it while keeping `default-paper`/archived accounts historical-only.
