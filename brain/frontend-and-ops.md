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
- Core row counts.
- Evidence progress.
- Equity trail.
- Tuning readiness and performance trace.
- Tuning iteration performance.
- Data sources and feature tuning.
- Runtime tunables.
- Latest runs.
- Recent paper orders, fills, and positions.

The Tuning Iteration Performance section is expected to show insufficient-data tune attempts before labels exist.

## Live Local Refresh

[serve_brain_projects.py](../serve_brain_projects.py) can serve the output directory with Python stdlib HTTP. The HTML polls the JSON sidecar when opened over local HTTP. File mode uses the embedded snapshot.

The normal render command is:

```bash
python3 render_brain_performance.py --output /private/tmp/pweather.html --json-output /private/tmp/pweather.json
```

The normal tuning command is:

```bash
python3 scanner.py tune --goal goals/paper_weather_edge_v1.yaml
```

## Operational Rules

- Keep scans unattended but paper-only.
- Refresh the dashboard after scans or tune checks.
- Do not add API keys, cookies, wallets, private keys, or signing flows to dashboard code.
- Treat the dashboard as observability and review UI, not an execution UI.
