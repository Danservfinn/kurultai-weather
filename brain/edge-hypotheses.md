# Edge Hypotheses

These are concrete paper-research hypotheses. They are not live-trading instructions.

## 1. Station-Specific Settlement Edge

Markets may price a city-level weather question while settlement depends on a specific station. Edge can appear when the settlement station has known distance, elevation, siting, or microclimate differences from the city forecast proxy.

Evidence needed:

- Exact source URL/rules text.
- Station ID and confidence.
- Station-local forecast and observations.
- Historical station/provider bias.

## 2. Touched-Threshold Lag

If the settlement station already touched an over threshold, public order books may lag the observation. Paper signals should distinguish true touched states from nearby-station proxies.

Evidence needed:

- Timestamped station observations.
- Confirmed settlement station.
- CLOB snapshot after the observation.
- Later label or market resolution.

## 3. Impossible Bucket Lag

For bounded or mutually exclusive buckets, some outcomes become impossible after the observed high exits the range. Thin markets may not immediately update all ladder buckets.

Evidence needed:

- Bucket parser correctness.
- Observed high so far.
- Cross-bucket ladder grouping.
- Public book depth for the stale outcome.

## 4. Ladder Inconsistency

Same city/date threshold markets should usually be monotonic. Violations can indicate stale quotes, bad parsing, liquidity gaps, or source ambiguity.

Evidence needed:

- Group markets by city/date/station/source.
- Normalize thresholds.
- Compare market probabilities to model probabilities.
- Avoid contradictory paper buys that cannot all resolve yes.

## 5. Forecast Ensemble Disagreement

Large provider/model spread may make naive single-source edges unreliable. Conversely, low disagreement with a wide market spread may be a stronger paper candidate.

Evidence needed:

- Model-specific forecast snapshots.
- Forecast run age.
- Provider disagreement features.
- Calibration after labels arrive.

## 6. Late-Day Observation Dominance

Near or after the typical daily high window, live observations may dominate forecast distribution. Sigma and edge thresholds should depend on local time and source confidence.

Evidence needed:

- Station-local time features.
- Observed high and recent temperature trend.
- Minutes until local end of day.
- Separate metrics for morning, peak-window, and late-day signals.

## 7. Liquidity and Stale Book Edge

Some apparent model edges may vanish after executable depth, spread, and stale-book filters. The real paper edge should be measured against public executable ask/depth, not displayed price alone.

Evidence needed:

- Repeated CLOB snapshots.
- Best ask, spread, and depth.
- Simulated fill price.
- Paper PnL and Brier metrics after labels.
