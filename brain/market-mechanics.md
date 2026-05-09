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

The scanner records observed highs when a station/date can be inferred. Signals can be marked as:

- `already_touched` or `already_won`
- `impossible_now` or `already_lost`
- `still_possible`
- `source_missing`

These states feed signal classification and feature rows.

## Executable Paper Price

The project prefers public CLOB order-book asks/depth over displayed prices. A paper entry is only treated as executable when public book depth supports the requested simulated size. Displayed prices can remain diagnostic, but they should not become paper fills unless explicitly allowed by a future paper-only goal.

See [scanner.py](../scanner.py) for CLOB book capture and simulated paper ledger code, and [safety boundaries](safety-boundaries.md) for what must remain absent.
