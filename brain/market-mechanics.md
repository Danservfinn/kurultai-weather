# Market Mechanics

## Daily Temperature Buckets

The scanner targets weather and daily high-temperature markets. Market titles and rule text are parsed into:

- City or location.
- Target local date.
- Outcome bucket text, such as an open-ended high-temperature threshold or a bounded range.
- Settlement/source hints, including source URLs, station IDs, and rule excerpts when available.

[scanner.py](../scanner.py) currently parses numeric buckets into `BucketSpec` records and computes model probabilities with a simple normal distribution around the forecast high.

## Settlement Source Is the Core Risk

A daily temperature market is not only "what will the city high be." The practical settlement question is:

- Which station or provider resolves the market?
- Which local day boundary applies?
- Is the market using Weather Underground, an airport station, NWS/NOAA data, or another source?
- Does the rule define inclusive thresholds or bucket ranges?
- Is the source delayed, blocked, or ambiguous?

The paper engine should skip or downgrade markets where source/station ambiguity dominates forecast edge.

## Touched and Impossible States

Daily high markets are path-dependent. Once a station has already touched an over threshold, the event can become effectively settled even before market resolution. Conversely, some bucket outcomes become impossible once the observed high moves outside their range.

The scanner records observed highs when a station/date can be inferred and now classifies contracts as threshold, exact-high, range, or unknown. Settlement state is conservative:

- Threshold high >= K: YES is certain after the observed high reaches K; NO is impossible then. Final NO waits for local-day completion or final source confirmation with no touch.
- Exact high = K: YES is impossible and NO is certain after the observed high exceeds K. Touching K before local close remains unresolved.
- Range/bucket: early NO/YES-impossible only occurs after the observed high exceeds the upper bound. Final YES/NO requires the final high unless the state is mathematically absorbing.

Signals can be marked as:

- `already_touched` or `already_won`
- `impossible_now` or `already_lost`
- `still_possible`
- `source_missing`

These states feed signal classification and feature rows.

## Event Model and Strategy Families

Each weather candidate receives an `event_key` from city, target date, source, station, and rule hash. Event records carry the latent final-high mean/sigma, observed high, local-day completion, contract count, and paper exposure. Contract payout rows store the mapping from final high to YES/NO payout.

Strategy families are explicit: `latency_absorbing_state`, `complement_arb`, `ladder_inconsistency`, `settlement_source_edge`, `diurnal_nowcast`, `forecast_distribution_directional`, plus `watch`, `skip`, and `unknown`.

Complement arbitrage is separate from forecast edge. It only fires for a same-market YES/NO pair when executable YES ask plus NO ask is below payout after margin and both legs pass depth and quote-age checks.

The ladder monitor computes implied ladder distributions, flags no-arb violations, stale/thin linked outcomes, discontinuities, and records candidate correction trades for paper review.

## Executable Paper Price

The project prefers public CLOB order-book asks/depth over displayed prices. A paper entry is only treated as executable when public book depth supports the requested simulated size. Displayed prices can remain diagnostic, but they should not become paper fills unless explicitly allowed by a future paper-only goal.

See [scanner.py](../scanner.py) for CLOB book capture and simulated paper ledger code, and [safety boundaries](safety-boundaries.md) for what must remain absent.
