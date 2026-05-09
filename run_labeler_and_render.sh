#!/usr/bin/env bash
set -euo pipefail
cd /Users/kublai/polymarket-weather-edge

if [[ -f runtime_tunables.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source runtime_tunables.env
  set +a
fi

LABEL_ARGS=(
  --limit "${LABEL_LIMIT:-25}"
  --min-age-days "${LABEL_MIN_AGE_DAYS:-2}"
  --retry-after-hours "${LABEL_RETRY_AFTER_HOURS:-12}"
  --pause "${LABEL_PAUSE_SECONDS:-${SCAN_PAUSE_SECONDS:-0.05}}"
  --http-timeout "${HTTP_TIMEOUT_SECONDS:-8}"
  --cache-ttl "${OBSERVATION_CACHE_TTL_SECONDS:-300}"
)

if [[ "${ENABLE_NCEI_DAILY:-0}" == "1" ]]; then
  LABEL_ARGS+=(--enable-ncei)
else
  LABEL_ARGS+=(--disable-ncei)
fi

if [[ "${ENABLE_NWS:-0}" == "1" ]]; then
  LABEL_ARGS+=(--enable-nws)
else
  LABEL_ARGS+=(--disable-nws)
fi

if [[ "${ENABLE_IEM:-0}" == "1" ]]; then
  LABEL_ARGS+=(--enable-iem)
else
  LABEL_ARGS+=(--disable-iem)
fi

python3 scanner.py label "${LABEL_ARGS[@]}"
python3 render_brain_performance.py \
  --output /Users/kublai/brain/projects/polymarket-weather-engine-performance.html \
  --json-output /Users/kublai/brain/projects/polymarket-weather-engine-performance.json
