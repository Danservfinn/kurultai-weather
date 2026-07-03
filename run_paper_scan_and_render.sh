#!/usr/bin/env bash
set -euo pipefail
cd /Users/kublai/polymarket-weather-edge

if [[ -f runtime_tunables.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source runtime_tunables.env
  set +a
fi

SCAN_ARGS=(
  --pause "${SCAN_PAUSE_SECONDS:-0.01}"
  --paper-size "${PAPER_SIZE_SHARES:-5.0}"
  --edge-threshold "${EDGE_THRESHOLD:-0.08}"
  --max-spread "${MAX_SPREAD:-0.12}"
  --min-entry "${MIN_ENTRY:-0.02}"
  --max-entry "${MAX_ENTRY:-0.95}"
  --max-position-pct "${MAX_POSITION_PCT:-0.02}"
  --max-city-date-pct "${MAX_CITY_DATE_PCT:-0.10}"
  --max-open-exposure-pct "${MAX_OPEN_EXPOSURE_PCT:-0.50}"
  --min-fill-shares "${MIN_FILL_SHARES:-1.0}"
)
if [[ "${ENABLE_WU:-0}" == "1" ]]; then
  SCAN_ARGS+=(--enable-wu)
fi

python3 scanner.py scan "${SCAN_ARGS[@]}"
if [[ -f scripts/strategy_lab_shadow_backfill.py ]]; then
  python3 scripts/strategy_lab_shadow_backfill.py \
    --limit-per-family "${STRATEGY_LAB_SHADOW_LIMIT_PER_FAMILY:-200}" \
    --lookback-runs "${STRATEGY_LAB_SHADOW_LOOKBACK_RUNS:-3}"
fi
python3 scanner.py summary
python3 scanner.py evaluate \
  --edge-threshold "${EDGE_THRESHOLD:-0.08}" \
  --min-entry "${MIN_ENTRY:-0.02}" \
  --max-entry "${MAX_ENTRY:-0.95}"
python3 edge_validation.py --persist
python3 render_brain_performance.py \
  --output /Users/kublai/brain/projects/polymarket-weather-engine-performance.html \
  --json-output /Users/kublai/brain/projects/polymarket-weather-engine-performance.json
