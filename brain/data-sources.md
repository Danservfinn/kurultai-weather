# Data Sources

The source strategy is to collect enough public, timestamped evidence to reconstruct what was knowable at decision time. Every source should preserve provider, endpoint/source URL when safe, fetch time, status, timeout/cache metadata, and read-only provenance.

## Core Sources

- Polymarket page/Gamma-style metadata: market discovery, title/rules/source text, outcomes, token IDs, and settlement hints. Implemented mainly in [scanner.py](../scanner.py).
- Polymarket public CLOB order books: best bid/ask, spread, and executable depth for paper fills. Implemented in [scanner.py](../scanner.py).
- Open-Meteo: baseline geocoding and daily high forecasts. Implemented in [scanner.py](../scanner.py), with future source-family treatment in [weather_sources.py](../weather_sources.py).
- NOAA/NCEI delayed daily summaries: preferred final label source for settled daily-high rows when station/date data is available.

## Optional Read-Only Weather Families

[weather_sources.py](../weather_sources.py) contains adapters or stubs for:

- NWS points, stations, observations, and forecast metadata.
- AviationWeather/METAR current reports.
- IEM/ASOS/METAR station observations.
- NOAA/NCEI delayed daily labels.
- Meteostat history/climatology stubs.
- Optional commercial weather provider stubs, disabled by default.

These are optional evidence families. They must not require credentials by default, and commercial providers must stay disabled unless explicitly configured outside the repo.

## Weather Underground

Weather Underground station pages are useful when market rules explicitly name a WU station. They are also brittle and often blocked. The current default is disabled unless `--enable-wu` or `ENABLE_WU=1` is set. WU reads should remain best-effort, cached, timeout-bounded, and non-critical for unattended scans.

## Source Expansion Priority

The detailed source backlog is in [research/data_sources_and_tunable_variables.md](../research/data_sources_and_tunable_variables.md). The highest-value additions are:

- Versioned settlement metadata from exact market rules.
- A station crosswalk and confidence layer.
- NWS and IEM current observations for U.S./airport-like markets.
- NCEI delayed labels for post-resolution validation.
- Repeated public CLOB snapshots and Polymarket public history.

No source should introduce wallet access, private keys, authenticated trading, live order creation, or secret material.

## Label Source Priority

The labeler uses read-only/free sources only:

1. NOAA/NCEI daily summaries for final daily-high labels.
2. NWS station observations as provisional supporting evidence when enabled.
3. IEM/ASOS daily highs as provisional supporting evidence when enabled.

Missing data is a normal state. The labeler records `pending`, `skipped`, or `error` attempts in SQLite and exits successfully so cron can retry later with bounded limits and retry windows.
