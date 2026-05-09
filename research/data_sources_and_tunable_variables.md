# Data Sources and Tunable Variables for Paper-Only Weather Markets

Date: 2026-05-09

Scope: paper-only Polymarket daily high-temperature/weather-market research. This brief is for data collection, offline analysis, proposal-only tuning, and future no-lookahead ML. It must not introduce wallet logic, live order placement, private keys, signed orders, or production trading.

## 1. Executive Summary: Top 10 Recommended Additions

1. **Persist exact market resolution text, source URLs, station IDs, and parsed rule fields as versioned settlement metadata.** This is the highest-value addition because settlement edge depends on the exact station/source/date rules, not just city title parsing.
2. **Add a station crosswalk and station-confidence layer.** Map Polymarket/WU station IDs, airport/METAR IDs, NWS station identifiers, latitude/longitude, elevation, timezone, and nearby fallback stations.
3. **Add NWS current observations and forecast-office metadata for U.S. markets.** NWS API data is free/open, cron-suitable, and useful for station-local observations, forecast issuance times, and alert context.
4. **Add Iowa Environmental Mesonet ASOS/METAR pulls for airport-station observations.** IEM is cron-friendly and valuable for intraday touched-threshold detection and historical station behavior; use it as an observation source, not an official settlement oracle unless rules name it.
5. **Add NOAA/NCEI daily summaries as delayed settlement-grade reference data.** NCEI is slower than intraday sources but useful for final labels, station climatology, and post-settlement validation.
6. **Expand Open-Meteo into model-specific forecast ensemble snapshots.** Capture individual model highs, hourly temperature paths, forecast run age, and ensemble dispersion instead of only one daily high.
7. **Collect repeated CLOB snapshots plus Polymarket public price history.** Current executable ask/depth is good; add time-series microstructure features such as spread persistence, quote churn, volume/last-trade context, and late-day price reaction.
8. **Track live touched-threshold and impossible-outcome states from hourly/minutely observations.** Markets around daily highs are path-dependent; once a threshold is touched, the forecast distribution is less important than observation reliability and station identity.
9. **Add ladder consistency checks across same city/date threshold markets.** Daily temperature markets often form a monotonic ladder; violations can identify stale books, bad parsing, settlement ambiguity, or model/market disagreement.
10. **Build no-lookahead training exports around immutable decision snapshots.** Every feature row should identify the source snapshot IDs and only include data observed before the station-local decision cutoff.

## 2. Data Sources

Integration priorities:

- **P0:** add soon; high value, free/public, cron-suitable, low safety risk.
- **P1:** high value but needs parsing, crosswalk, or reliability guards.
- **P2:** optional enhancer, useful after core labels and snapshots mature.
- **P3:** experimental, brittle, paid/commercial, or manual-review biased.

| Source | What It Adds | Example Features | Latency/Cadence | Cost/Rate-Limit Risk | Reliability Risk | Priority | Unattended Cron | Optional Flag |
|---|---|---|---|---|---|---|---|---|
| Polymarket Gamma/search metadata | Market discovery, titles, descriptions, tags, resolution dates, outcome tokens, settlement rules. | `market_slug`, `event_id`, `question_text`, `resolution_source_url_count`, `parsed_city`, `parsed_date`, `parsed_threshold_f`, `condition_id`, `token_id`. | Poll every scan or every 15-60 min with caching. | Public API/no auth for metadata per Polymarket docs; rate limits can change. | API shape and page JSON can drift. | P0 | Yes | No; core source. |
| Polymarket CLOB public orderbook | Executable ask/bid/depth for paper fills and microstructure. | `best_bid`, `best_ask`, `spread`, `mid`, `ask_depth_5_shares`, `paper_fill_price`, `depth_sufficient`, `book_age_seconds`. | Every scan; batch endpoints preferred. | Public market-data endpoints; avoid trading endpoints/auth. | Stale snapshots, thin books, transient HTTP failures. | P0 | Yes | No; core execution simulation. |
| Polymarket public price history / last trade | Market momentum, realized volatility, price reaction around observations. | `price_change_15m`, `price_change_1h`, `last_trade_price`, `last_trade_side`, `trade_count_proxy`, `mid_vs_last_trade_gap`. | 15-60 min; denser near station-local afternoon. | Public but rate limits not guaranteed. | History intervals may be relative; careful snapshot timestamping required. | P1 | Yes | Yes, `ENABLE_POLY_HISTORY`. |
| Existing Open-Meteo forecast API | Baseline free forecast source and geocoding. | `om_daily_high_f`, `om_hourly_max_f`, `om_model_name`, `om_forecast_run_age_hours`, `om_forecast_update_hour_local`. | Hourly or per scan with cache. | Free non-commercial tier currently lists generous limits; production/commercial use may require plan. | Model blending can obscure source model; location interpolation error. | P0 | Yes | No for baseline; model expansion can be flagged. |
| Open-Meteo model-specific forecasts | Forecast ensemble across HRRR/GFS/NAM/ICON/ECMWF-style available models depending on region/API support. | `model_high_f_min/max/mean`, `model_spread_f`, `hrrr_minus_gfs_f`, `latest_run_high_delta_f`, `forecast_consensus_rank`. | 1-6 hours depending on model. | Same Open-Meteo caveats; watch call volume. | Model availability varies by location/variable. | P1 | Yes | Yes, `ENABLE_OM_MODELS`. |
| Open-Meteo historical forecast / previous runs | No-lookahead reconstruction of what forecasts said at decision time; forecast error history. | `forecast_error_at_tminus_6h`, `run_to_run_high_delta`, `forecast_age_bucket`, `model_bias_station_30d`. | Offline/backfill plus daily. | Free tier may not include every historical/previous-run use; commercial plan may be needed for heavy use. | API availability/licensing may change. | P2 | Yes for bounded pulls | Yes, `ENABLE_OM_HISTORICAL_FORECASTS`. |
| NWS API: points, stations, observations, forecasts | Free/open U.S. official observations, forecast office/gridpoint metadata, station lists. | `nws_station_id`, `nws_obs_temp_f`, `nws_obs_time`, `nws_forecast_high_f`, `nws_grid_office`, `nws_station_distance_mi`, `nws_obs_age_minutes`. | Observations often hourly/subhourly; cache-friendly API. | Free/open; rate limit is not public but described as generous for typical use. | U.S.-only; station mapping and local timezone handling needed. | P0 for U.S. | Yes | Yes for non-U.S. or outages, `ENABLE_NWS`. |
| Iowa Environmental Mesonet ASOS/METAR archive | Airport ASOS/AWOS observations and historical METAR; useful station-high/touched-threshold proxy. | `iem_metar_tmpf`, `iem_max_tmpf_so_far`, `iem_obs_count_today`, `iem_station_network`, `iem_tmpf_qc_missing_rate`, `last_metar_age_minutes`. | Current METAR near real time; one-minute ASOS archive has about 24h delay. | Free/public; be polite with cache and station batching. | Limited QC; not authoritative for all settlements; some networks sparse. | P0/P1 | Yes | Yes, `ENABLE_IEM`. |
| NOAA/NCEI Access Data Service daily summaries | Delayed high-quality daily station summaries for labels, climatology, and validation. | `ncei_tmax_f`, `ncei_station_id`, `ncei_station_name`, `ncei_record_lag_days`, `ncei_label_match_flag`. | Usually delayed; daily/offline backfill. | Access Data Service is public; CDO token API has token/rate limits, so prefer no-token Access Data where possible. | Data can lag; station IDs may not match settlement source directly. | P1 | Yes for daily/backfill | Yes, `ENABLE_NCEI_DAILY`. |
| NOAA CDO API | Station metadata, daily summaries, normals; broader discovery. | `cdo_station_coverage`, `cdo_normal_tmax`, `station_period_of_record_days`, `station_data_completeness`. | Daily/offline. | Requires token; current CDO docs list 5 requests/sec and 10,000/day per token. Do not make mandatory. | Token management and quota; not for secretless default cron. | P3 | Only if token provided | Yes, `ENABLE_CDO`. |
| Meteostat | Free historical station data, nearby-station discovery, normals, completeness checks. | `meteostat_tmax_f`, `nearby_station_count`, `station_elevation_ft`, `historical_bias_30d`, `coverage_pct`, `climatology_percentile`. | Offline/backfill; hourly/daily data. | Python library/dependencies; JSON API may have limits. | Some records are interpolated or provider-blended; use provenance fields. | P1/P2 | Yes after dependency decision | Yes, `ENABLE_METEOSTAT`. |
| Weather Underground station pages/API-like reads | Settlement-source matching where markets explicitly name WU station IDs; current scanner already treats this as best effort. | `wu_station_id`, `wu_observed_high_f`, `wu_source_confidence`, `wu_fetch_status`, `wu_blocked_flag`, `wu_high_age_minutes`. | Slow/brittle; per scan only when enabled and cached. | Anonymous scraping often blocked; commercial/private endpoints not mandatory. | High brittleness; HTML/API shape changes. | P2 | Only with strict timeout | Yes, keep `ENABLE_WU=false` default. |
| MADIS / NOAA integrated observations | Broad observation database with QC flags and many provider networks. | `madis_temp_f`, `madis_qc_flag`, `madis_provider`, `madis_station_type`, `qc_pass_rate`. | Near real time to historical depending on access method. | Public access exists but tooling is more complex; guest/public endpoints vary. | Integration complexity; formats and access paths are less simple than NWS/IEM. | P2 | Maybe, after prototype | Yes, `ENABLE_MADIS`. |
| Airport METAR direct feeds / aviationweather.gov | Direct station reports for airport settlement proxies. | `metar_tmpf`, `metar_raw`, `metar_report_time`, `metar_temp_rounded_flag`, `metar_parse_quality`. | Often subhourly/hourly. | Public, but endpoint policies can change. | METAR temps are rounded and may not equal daily max source. | P1/P2 | Yes | Yes, `ENABLE_METAR_DIRECT`. |
| Timezone/geospatial libraries or static DB | Correct station-local day boundaries and distance/elevation features. | `station_tz`, `market_local_date`, `decision_time_local`, `distance_city_to_station_mi`, `elevation_delta_ft`. | Static cache; refresh rarely. | Free if using bundled/static data; dependency choice matters. | DST and geocoding ambiguity. | P0 | Yes | No for timezone/date logic; external geocoding optional. |
| Polymarket comments/news text | Manual-review context, ambiguity flags, crowd corrections to station/source assumptions. | `comment_count`, `recent_rule_comment_flag`, `source_dispute_terms`, `settlement_ambiguity_terms`. | Low cadence; diagnostics only. | Public but scraping/API shape uncertain. | Noisy and easily leaks post-resolution sentiment if mishandled. | P3 | No default | Yes, `ENABLE_COMMENTS_DIAGNOSTIC`. |
| Commercial weather APIs: Tomorrow.io, AccuWeather, Visual Crossing, Weatherbit, Xweather | Extra forecast ensembles and station observations if user chooses to provide credentials. | `commercial_model_high_f`, `provider_bias_30d`, `provider_update_age`, `provider_disagreement_f`. | Provider-specific. | Paid/keyed; never mandatory; free fallback is Open-Meteo/NWS/IEM/NCEI. | Vendor lock-in, terms, outages. | P3 | Only with explicit key | Yes, disabled unless configured. |

Reference surfaces checked on 2026-05-09:

- NWS API docs: https://www.weather.gov/documentation/services-web-api
- Open-Meteo docs/pricing: https://open-meteo.com/en/docs and https://open-meteo.com/en/pricing
- Polymarket API docs: https://docs.polymarket.com/api-reference and https://docs.polymarket.com/trading/orderbook
- NCEI Access Data Service docs: https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation
- NOAA CDO docs: https://www.ncei.noaa.gov/cdo-web/webservices/v2
- IEM ASOS/METAR docs: https://mesonet.agron.iastate.edu/request/download.phtml and https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?help=
- Meteostat docs: https://dev.meteostat.net/
- MADIS overview: https://madis.ncep.noaa.gov/

## 3. Feature Groups for Training

### Settlement Station/Source Features

- `settlement_source_type`: `weather_underground`, `nws`, `ncei`, `airport_metar`, `other`, `unknown`.
- `settlement_source_url_hash`: stable hash of source URL as observed in market rules.
- `settlement_station_id_raw`, `settlement_station_id_normalized`, `station_id_namespace`.
- `station_lat`, `station_lon`, `station_elevation_ft`, `station_timezone`.
- `city_lat`, `city_lon`, `city_to_station_distance_mi`, `city_station_elevation_delta_ft`.
- `station_confidence_score`: parser confidence that the station is the settlement station.
- `station_confidence_reason`: compact category such as `explicit_rule`, `source_link_match`, `nearby_airport_guess`, `unknown`.
- `settlement_rule_version_hash`: hash of normalized rules text at snapshot time.
- `threshold_operator`: `above`, `below`, `at_or_above`, `range`, `exact_bucket`.
- `threshold_low_f`, `threshold_high_f`, `bucket_width_f`.
- `ambiguous_rule_flag`, `multi_station_rule_flag`, `source_unavailable_flag`.

### Forecast Model Ensemble Features

- `forecast_provider`, `forecast_model`, `forecast_run_time_utc`, `forecast_age_hours`.
- `forecast_daily_high_f`, `forecast_hourly_max_f`, `forecast_high_time_local`.
- `ensemble_high_mean_f`, `ensemble_high_median_f`, `ensemble_high_min_f`, `ensemble_high_max_f`.
- `ensemble_high_std_f`, `ensemble_high_iqr_f`, `model_disagreement_f`.
- `provider_spread_f`: max forecast high minus min forecast high across providers.
- `forecast_delta_1h_f`, `forecast_delta_3h_f`, `forecast_delta_6h_f`, `forecast_delta_prev_run_f`.
- `forecast_vs_threshold_f`, `forecast_z_to_threshold`.
- `climatology_percentile`: forecast high relative to station history for date-of-year.
- `bias_corrected_high_f`: forecast high adjusted by station/provider historical bias.

### Live Observation/Touched-Threshold Features

- `obs_provider`, `obs_station_id`, `obs_time_utc`, `obs_age_minutes`.
- `current_temp_f`, `observed_high_so_far_f`, `observed_high_time_local`.
- `threshold_touched_flag`, `threshold_touch_time_local`, `minutes_since_touch`.
- `threshold_remaining_margin_f`: threshold minus observed high for over markets, or observed high minus threshold for under markets.
- `impossible_now_flag`: outcome impossible given already-observed high or mutually exclusive bucket logic.
- `obs_count_today`, `obs_gap_minutes_max`, `obs_missing_rate_today`.
- `obs_temp_rise_1h_f`, `obs_temp_rise_3h_f`, `cooling_after_peak_flag`.
- `nearby_station_touched_count`, `nearby_station_max_temp_f`, `station_vs_nearby_delta_f`.

### Time/Local-Day Features

- `market_date_local`, `station_local_date`, `decision_time_local`.
- `minutes_since_local_midnight`, `minutes_until_local_end_of_day`.
- `minutes_until_typical_peak`: use station/history or default afternoon peak.
- `local_hour`, `local_weekday`, `day_of_year`, `month`.
- `dst_transition_flag`, `timezone_confidence`.
- `pre_open_flag`, `morning_flag`, `peak_heating_window_flag`, `late_day_flag`, `post_peak_flag`.
- `resolution_deadline_utc`, `minutes_to_resolution_deadline`.

### Market Microstructure/Orderbook Features

- `best_bid`, `best_ask`, `mid_price`, `displayed_price`, `last_trade_price`.
- `spread`, `spread_pct_mid`, `mid_vs_displayed_gap`, `mid_vs_last_trade_gap`.
- `ask_depth_to_5_shares`, `ask_depth_to_10_shares`, `bid_depth_to_5_shares`.
- `executable_ask_2_shares`, `executable_ask_5_shares`, `executable_ask_10_shares`.
- `book_imbalance_top1`, `book_imbalance_top3`, `book_level_count_bid`, `book_level_count_ask`.
- `price_change_15m`, `price_change_1h`, `price_change_6h`, `micro_volatility_1h`.
- `quote_churn_rate`: count of best bid/ask changes per hour.
- `stale_book_flag`, `wide_spread_flag`, `crossed_or_locked_book_flag`.

### Liquidity/Execution Features

- `paper_size_shares`, `paper_fill_price`, `paper_fill_shares`, `paper_slippage`.
- `paper_fill_source`: `executable_clob`, `displayed_price_fallback`, `skipped`.
- `depth_sufficient_flag`, `min_fill_satisfied_flag`.
- `cost_basis`, `notional_at_risk`, `max_loss_if_no`.
- `entry_price_bucket`, `entry_price_distance_to_bounds`.
- `estimated_exit_spread`, `liquidity_score`, `thin_market_flag`.
- `executable_edge_after_slippage`: model probability minus executable fill price.

### Cross-Market Ladder Consistency Features

- `ladder_group_key`: city/date/station/source grouping.
- `threshold_rank`, `num_thresholds_in_ladder`, `adjacent_threshold_gap_f`.
- `market_prob_monotonicity_violation_flag`.
- `model_prob_monotonicity_violation_flag`.
- `adjacent_market_mid_gap`, `adjacent_model_prob_gap`.
- `butterfly_inconsistency_score`: detects non-monotone or locally inconsistent bucket pricing.
- `bucket_sum_implied_probability`, `bucket_sum_deviation_from_one`.
- `same_ladder_best_edge_rank`, `same_ladder_signal_count`.
- `contradictory_signal_flag`: multiple paper buys that cannot all settle yes.

### Calibration/History Features

- `provider_station_bias_7d/30d/90d`, `provider_station_mae_30d`.
- `model_brier_30d`, `market_brier_30d`, `model_vs_market_brier_delta_30d`.
- `edge_bin_hit_rate`, `price_bin_hit_rate`, `threshold_margin_bin_hit_rate`.
- `station_source_disagreement_rate`.
- `observed_label_lag_days`: time from market date to usable final label.
- `same_city_recent_count`, `same_station_recent_count`, `same_threshold_recent_count`.
- `paper_signal_prior_win_rate`: diagnostics only until sample size is adequate.

### Portfolio/Risk Features

- `paper_cash`, `paper_equity`, `open_position_count`, `open_exposure_pct`.
- `position_pct_equity`, `city_date_exposure_pct`, `station_date_exposure_pct`.
- `same_ladder_exposure_pct`, `max_correlated_loss_pct`.
- `unresolved_exposure_count`, `days_since_oldest_unresolved_position`.
- `drawdown_pct`, `rolling_realized_pnl`, `paper_account_age_days`.
- `paper_buy_frequency_24h`, `skipped_cash_guard_count_24h`.

### Source-Quality and Missingness Features

- `source_fetch_status`, `source_http_status`, `source_latency_ms`.
- `source_snapshot_age_seconds`, `source_timeout_flag`, `source_parse_error_flag`.
- `field_missing_mask`: stable bitmask or JSON object for missing critical fields.
- `source_quality_score`: weighted completeness/freshness/parser-confidence score.
- `source_disagreement_score`: normalized spread across providers/nearby stations.
- `fallback_source_used_flag`, `fallback_source_type`.
- `decision_features_complete_flag`: whether all P0 features were available at decision time.

## 4. Tunable Variables

These variables should remain proposal-only until there are enough labeled outcomes. Runtime changes should be suggested by `tune` or an offline report, not applied automatically.

### Runtime/Cadence Knobs

- `scan_interval_minutes`: current allowed scaffold includes 5-120 minutes.
- `dashboard_poll_seconds`: display-only refresh cadence.
- `scan_pause_seconds`: polite delay between public HTTP requests.
- `market_metadata_cache_ttl_seconds`.
- `forecast_cache_ttl_seconds`.
- `observation_cache_ttl_seconds`.
- `orderbook_cache_ttl_seconds`.
- `late_day_scan_multiplier`: increased collection frequency near station-local peak/end-of-day.
- `max_http_retries`, `http_timeout_seconds`, `source_backoff_minutes`.
- `per_source_daily_call_budget`.

### Entry/Edge Knobs

- `edge_threshold`: minimum modeled probability minus executable entry.
- `edge_threshold_touched`: separate threshold for already-touched markets where label risk is lower but settlement-source risk remains.
- `edge_threshold_ambiguous_station`: higher required edge when station confidence is weak.
- `min_entry`, `max_entry`: avoid dust and near-certain prices unless explicitly supported by labels.
- `min_model_probability`, `max_model_probability`.
- `min_source_confidence`.
- `min_forecast_consensus`: require ensemble agreement.
- `max_forecast_disagreement_f`: skip when models diverge too much.

### Probability/Calibration Knobs

- `sigma_f`: current normal-distribution temperature uncertainty.
- `sigma_by_time_of_day`: lower uncertainty late day when observations dominate.
- `sigma_by_model_spread_multiplier`: inflate uncertainty when ensembles disagree.
- `station_bias_window_days`: e.g. 7, 30, 90.
- `forecast_provider_weight_open_meteo`, `forecast_provider_weight_nws`, `forecast_provider_weight_meteostat`.
- `market_implied_prior_weight`: blending model probability with market price for calibrated output, diagnostic first.
- `calibration_method`: raw normal, isotonic, Platt/logistic, beta-binomial bin smoothing.
- `min_samples_per_calibration_bin`.
- `probability_floor`, `probability_ceiling`: avoid pathological 0/1 outputs.

### Execution/Liquidity Knobs

- `paper_size_shares`.
- `min_fill_shares`.
- `max_spread`.
- `max_slippage`.
- `require_executable_clob_depth`: should stay true for paper fills.
- `allowed_fill_sources`: keep `executable_clob` as the only fill source for portfolio accounting; displayed fallback can remain diagnostic.
- `min_top_of_book_depth`.
- `stale_book_max_age_seconds`.
- `max_quote_churn_rate`: optional skip for unstable books.
- `min_liquidity_score`.

### Portfolio/Risk Knobs

- `max_position_pct`.
- `max_city_date_pct`.
- `max_station_date_pct`.
- `max_ladder_group_pct`.
- `max_open_exposure_pct`.
- `max_correlated_loss_pct`.
- `max_open_positions`.
- `max_positions_per_market`.
- `daily_paper_loss_limit_pct`: proposal-only guard for future simulation.
- `unresolved_position_age_limit_days`: diagnostics and risk reporting.

### Data-Source Enable/Disable and Weighting Knobs

- `enable_nws`, `enable_iem`, `enable_ncei_daily`, `enable_meteostat`, `enable_wu`, `enable_poly_history`, `enable_om_models`.
- `source_weight_open_meteo`, `source_weight_nws`, `source_weight_iem`, `source_weight_wu`, `source_weight_ncei`.
- `source_timeout_seconds_by_provider`.
- `source_priority_order_observations`: settlement source first, then official/nearby fallbacks.
- `allow_paid_provider_features`: default false.
- `require_p0_source_completeness`: skip or downgrade rows missing core source snapshots.

### Settlement/Station Uncertainty Knobs

- `min_station_confidence_for_buy`.
- `station_distance_penalty_per_mile`.
- `elevation_delta_penalty_per_100ft`.
- `unknown_station_edge_haircut`.
- `ambiguous_rule_edge_haircut`.
- `multi_station_rule_policy`: skip, average, worst-case, manual-review only.
- `settlement_source_missing_policy`: skip, watch, or diagnostic-only.
- `touched_threshold_source_requirement`: require explicit settlement station vs allow nearby proxy.

Tiny schema sketch for future planning only:

```sql
source_snapshots(
  id integer primary key,
  created_at text not null,
  source_name text not null,
  source_kind text not null,
  market_id text,
  station_id text,
  observed_at text,
  request_url_hash text,
  payload_hash text not null,
  parsed_json text not null,
  raw_path text
);

training_rows.features_json should reference immutable snapshot IDs:
{
  "forecast_snapshot_ids": [123],
  "observation_snapshot_ids": [456, 457],
  "orderbook_snapshot_id": 789,
  "source_snapshot_ids": [101, 102],
  "decision_time_utc": "2026-05-09T18:45:00Z"
}
```

## 5. Training Labels and No-Lookahead Rules

### Final Binary Outcome Labels

- Label each market/outcome row as `1` if the official Polymarket-settled outcome is yes, `0` if no.
- Store `label_status`: `unlabeled`, `settled_yes`, `settled_no`, `voided`, `ambiguous`, `manual_review`.
- Store `label_source`: Polymarket resolved outcome first; weather-source verification second.
- Store `label_observed_at_utc`: when the label became available to this system.
- Store `label_market_date_local`: the station-local date the weather outcome concerns.
- Do not infer final labels from latest market price unless the market is officially resolved or rule/source verification is explicit enough for a separate `weather_verified_label`.

### Marked-to-Market Labels Only for Diagnostics

- Keep current mark-to-market evaluation separate from final outcome labels.
- Suggested fields: `mtm_price`, `mtm_time_utc`, `mtm_return`, `mtm_source_signal_id`.
- Never train final classifiers on later market prices as if they were truth.
- Use MTM only for diagnostics: quote quality, liquidity decay, interim signal drift, and paper ledger reporting.

### Station-Local Cutoff Handling

- Every row needs `decision_time_utc`, `station_timezone`, and `decision_time_local`.
- A feature is eligible only if its `observed_at` or `source_created_at` is at or before `decision_time_utc`.
- Weather day boundaries must use station-local date, not server UTC date.
- Forecasts issued after the decision time are forbidden even if they describe the same target date.
- Intraday observations after decision time are forbidden unless building a later decision row.
- Final label data, NCEI delayed summaries, and post-settlement market status are label/evaluation fields only, never decision features.

### Preserve Raw Snapshots Exactly as Observed at Decision Time

- Store raw or canonicalized payload hashes and parsed snapshots at fetch time.
- Do not mutate historical snapshots when parsers improve; add a parser version and regenerated derived features separately.
- Feature exports should be reproducible from snapshot IDs, parser version, and decision time.
- For failed fetches, store failure metadata so missingness is learnable and no silent backfill leaks future availability.
- If source backfill is needed, mark `backfilled_at_utc` and exclude backfilled values from decision-time training unless the source can prove the data was available before the decision.

## 6. Goal-Seeking Roadmap

### What Can Be Tuned Now With Insufficient Labels

- Collection cadence proposals based on source freshness, call budget, and missingness.
- Parser confidence thresholds for watch/skip classes.
- Source timeout/backoff settings.
- Paper-fill requirements such as executable depth, minimum fill size, and stale-book filters.
- Dashboard/report ranking by data completeness and source confidence.
- Ladder grouping diagnostics and contradiction flags.
- Portfolio risk caps in simulation, without treating results as statistically meaningful.

### What Must Wait for Labeled Outcomes

- Edge threshold optimization for profit or Brier score.
- Probability calibration method selection.
- Forecast-provider weighting and model-specific bias corrections.
- Station-confidence numeric penalty calibration.
- Position sizing rules.
- Late-day vs morning strategy comparisons.
- Any claim of positive expected value.

### First Offline Experiments Once Labels Exist

1. **Baseline replication:** current normal forecast model vs market price, using strict decision snapshots.
2. **Observation-aware model:** add touched-threshold, observed-high-so-far, and time-of-day features.
3. **Ensemble forecast model:** compare Open-Meteo blended forecast against model-specific spread and NWS/IEM observations.
4. **Station uncertainty ablation:** explicit station confidence vs nearby proxy station fallback.
5. **Market microstructure ablation:** executable edge vs displayed-price edge vs mid-price edge.
6. **Ladder consistency filter:** evaluate whether monotonicity violations improve or harm paper returns.
7. **Calibration curves:** Brier/log loss by probability bin, city/station, hour-local, source completeness, and price bucket.
8. **Walk-forward validation:** train on earlier dates, validate on later dates, grouped by station-local calendar date to avoid same-day leakage.

### Promotion Gates

- At least 14 calendar days and 300 training rows for diagnostics, matching the existing scaffold.
- For ML/calibration promotion, prefer at least 100 final labeled outcomes and multiple stations/cities; otherwise keep outputs as exploratory.
- No unresolved no-lookahead violations in an audit sample.
- Paper-only guard tests still pass.
- Fill simulation uses executable CLOB depth, not displayed prices.
- New source failures degrade to missingness/watch/skip, not crashes.
- Any new runtime/tuning proposal remains proposal-only and requires human review before schedule/config changes.

## 7. Concrete Implementation Backlog

Implemented notes as of 2026-05-09:

- `weather_sources.py` defines bounded read-only adapter interfaces for NWS, AviationWeather/METAR, IEM URL building, NOAA/NCEI delayed labels, Meteostat optional history/climatology, Open-Meteo source metadata enrichment, and disabled commercial-provider stubs. Adapter records preserve provider provenance and do not require credentials by default.
- `features.py` builds no-lookahead decision-time feature dictionaries. Records timestamped after the decision time are excluded from value features, and label/final outcome fields are stripped before feature construction.
- `tuning_evaluator.py` and the dashboard report source-family status, feature coverage/missingness, metric readiness, and a post-label `approved-for-paper-forward-test` gate. This gate is config-proposal only and never authorizes live trading, order placement, wallet use, or live-money deployment.

1. **P0: Add `settlement_metadata` extraction and persistence.** Expected value: anchors every future feature and label to exact rules/source/station. Safety: store text/URLs only; no secrets; parser failures become low confidence.
2. **P0: Add station crosswalk table/cache.** Expected value: consistent station IDs across WU, NWS, IEM/METAR, NCEI, Meteostat. Safety: derived metadata only; include provenance and confidence.
3. **P0: Add station-local timezone normalization.** Expected value: prevents UTC/local-day leakage and bad labels. Safety: static timezone lookup; no external runtime dependency required if cached.
4. **P0: Add NWS U.S. observation fetcher behind `ENABLE_NWS`.** Expected value: reliable free current observations and station lists. Safety: public GETs, cache, timeout, no auth.
5. **P0: Add IEM METAR/ASOS current observation fetcher behind `ENABLE_IEM`.** Expected value: touched-threshold detection for airport-like stations. Safety: polite cadence and source status fields.
6. **P0: Add immutable generic `source_snapshots` table or equivalent.** Expected value: reproducible no-lookahead feature rows. Safety: payload hashes plus compact parsed JSON; avoid storing credentials or cookies.
7. **P0: Add feature missingness/source-quality fields to `training_rows.features_json`.** Expected value: lets future models learn source reliability and avoids silent survivor bias. Safety: no behavior change to paper fills.
8. **P1: Add Open-Meteo model-specific forecast pulls.** Expected value: ensemble dispersion and provider disagreement. Safety: feature-only, cache aggressively, flag-gated.
9. **P1: Add Polymarket price-history snapshots.** Expected value: market momentum and microstructure diagnostics. Safety: public read-only endpoints only; do not add auth/trading calls.
10. **P1: Add ladder-group builder for city/date/station/source.** Expected value: monotonicity checks and correlated-risk grouping. Safety: diagnostics first; do not auto-trade or auto-size.
11. **P1: Add delayed NCEI daily-summary backfill job behind `ENABLE_NCEI_DAILY`.** Expected value: final weather verification and station bias history. Safety: daily/offline cadence; labels remain separate from decision features.
12. **P1: Add final label ingestion from Polymarket resolved outcomes.** Expected value: unlocks supervised evaluation. Safety: store label availability timestamp; keep MTM separate.
13. **P1: Add no-lookahead export audit command.** Expected value: catches feature timestamps after decision time. Safety: read-only report.
14. **P2: Add Meteostat historical station/climatology backfill.** Expected value: station bias, normals, and completeness features. Safety: optional dependency/flag; record provider provenance.
15. **P2: Keep Weather Underground support optional and more bounded.** Expected value: useful when markets explicitly cite WU station IDs. Safety: short timeout, cache, disabled default, no scraping escalation.
16. **P2: Add nearby-station consensus features.** Expected value: flags suspect station readings and improves touched-threshold confidence. Safety: never override official settlement station; use as uncertainty feature.
17. **P2: Add source-disagreement dashboard panel.** Expected value: makes unreliable rows visible before tuning. Safety: reporting only.
18. **P3: Add commercial provider adapter interface.** Expected value: future plug-in forecasts if credentials are intentionally supplied. Safety: disabled by default; no provider is mandatory; never commit keys.
19. **P3: Add comment/news ambiguity diagnostics.** Expected value: identify disputed settlement rules. Safety: manual-review only; exclude post-resolution comments from decision features unless timestamped before decision.
20. **P3: Add experimental WebSocket collector for CLOB market channel.** Expected value: richer quote-churn and event timing data. Safety: read-only, feature-only, bounded storage, optional because polling is simpler and cron-friendly.
