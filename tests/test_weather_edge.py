import argparse
import contextlib
import csv
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scanner
import weather_sources


class WeatherEdgeTests(unittest.TestCase):
    def test_runtime_tunables_env_selects_clean_account_for_direct_cli_use(self):
        old_account_name = os.environ.pop("PAPER_ACCOUNT_NAME", None)
        try:
            self.assertEqual(scanner.active_paper_account_name(), "paper_account_v2_clean_post_gate")
        finally:
            if old_account_name is not None:
                os.environ["PAPER_ACCOUNT_NAME"] = old_account_name

    def test_paper_scan_wrapper_persists_strategy_survival_snapshot(self):
        with open(os.path.join(ROOT, "run_paper_scan_and_render.sh"), encoding="utf-8") as f:
            wrapper = f.read()
        self.assertIn("edge_validation.py", wrapper)
        self.assertIn("--persist", wrapper)

    def test_env_paper_account_name_routes_official_orders_to_clean_account(self):
        old_account_name = os.environ.get("PAPER_ACCOUNT_NAME")
        os.environ["PAPER_ACCOUNT_NAME"] = "paper_account_v2_clean_post_gate"
        try:
            with tempfile.TemporaryDirectory() as td:
                db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
                args = argparse.Namespace(
                    disable_ledger=False,
                    paper_size=5.0,
                    min_fill_shares=1.0,
                    min_entry=0.02,
                    max_entry=0.95,
                    max_spread=0.08,
                    max_position_pct=0.50,
                    max_city_date_pct=0.50,
                    max_open_exposure_pct=0.50,
                    allow_weak_families=True,
                    disable_weak_families=False,
                    strict_survival_gate=False,
                    shadow_ladder_inconsistency=False,
                    shadow_strategy_families="",
                )
                scanner.simulate_paper_order(
                    db,
                    args,
                    run_id=1,
                    signal_id=2,
                    created_at="2026-06-06T00:00:00Z",
                    m={"market_id": "clean-route", "title": "Clean routing weather test"},
                    outcome="Yes",
                    token_id="tok-clean",
                    city="Denver",
                    target_date="2026-06-06",
                    signal_type="paper_buy_edge",
                    quote={
                        "entry_price": 0.40,
                        "ask": 0.40,
                        "spread": 0.01,
                        "depth": 10.0,
                        "depth_sufficient": True,
                        "execution_source": "clob_book",
                        "quote_age_seconds": 1.0,
                        "stale_book_flag": False,
                        "raw_status": "ok",
                    },
                    event_key="event-clean-route",
                    candidate_key="cand-clean-route",
                    strategy_family="latency_absorbing_state",
                )

                order_account = db.execute(
                    """
                    select pa.name
                    from paper_orders po
                    join paper_accounts pa on pa.id = po.account_id
                    """
                ).fetchone()[0]
                self.assertEqual(order_account, "paper_account_v2_clean_post_gate")
                self.assertEqual(
                    db.execute("select count(*) from paper_orders where account_id=(select id from paper_accounts where name='default-paper')").fetchone()[0],
                    0,
                )
                db.close()
        finally:
            if old_account_name is None:
                os.environ.pop("PAPER_ACCOUNT_NAME", None)
            else:
                os.environ["PAPER_ACCOUNT_NAME"] = old_account_name

    def test_extract_next_json_accepts_extra_script_attributes(self):
        page = (
            '<html><script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'
            '{"props":{"pageProps":{"markets":[{"question":"Weather?","outcomes":["Yes","No"]}]}}}'
            '</script></html>'
        )
        data = scanner.extract_next_json(page)
        self.assertEqual(data["props"]["pageProps"]["markets"][0]["question"], "Weather?")

    def test_extract_next_json_accepts_app_router_flight_payload_markets(self):
        market = {
            "id": "m-app-router",
            "conditionId": "0xabc",
            "question": "Will the highest temperature in Test City be 80°F on June 30?",
            "slug": "highest-temperature-in-test-city-on-june-30-2026-80f",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/co/denver/KDEN",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.25", "0.75"],
            "clobTokenIds": ["tok-yes", "tok-no"],
        }
        flight_row = "21:" + json.dumps(["$", "component", None, {"dehydratedState": {"queries": [{"state": {"data": {"markets": [market]}}}]}}]) + "\n"
        page = f"<html><script>self.__next_f.push({json.dumps([1, flight_row])})</script></html>"

        data = scanner.extract_next_json(page)
        markets = scanner.extract_markets(data)

        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0]["market_id"], "0xabc")
        self.assertEqual(markets[0]["title"], market["question"])
        self.assertEqual(markets[0]["outcomes"], ["Yes", "No"])
        self.assertEqual(markets[0]["prices"], [0.25, 0.75])
        self.assertEqual(markets[0]["token_ids"], ["tok-yes", "tok-no"])
        self.assertEqual(markets[0]["source_url"], market["resolutionSource"])

    def test_bucket_bounds_fahrenheit_range_not_negative(self):
        for label in ("81-82F", "81-82°F", "81–82F", "81 — 82 F", "81 to 82°F"):
            with self.subTest(label=label):
                self.assertEqual(scanner.bucket_bounds(label), (81.0, 82.0))

    def test_bucket_bounds_open_tail_wording(self):
        below = scanner.parse_bucket("below 81F")
        self.assertEqual(below.kind, "open_below_strict")
        self.assertEqual(below.high_int, 81)
        self.assertEqual(scanner.bucket_bounds("below 81F"), (-math.inf, 80.5))

        above = scanner.parse_bucket("above 82F")
        self.assertEqual(above.kind, "open_above_strict")
        self.assertEqual(above.low_int, 82)
        self.assertEqual(scanner.bucket_bounds("above 82F"), (82.5, math.inf))

        or_below = scanner.parse_bucket("81°F or below")
        self.assertEqual(or_below.kind, "open_below_inclusive")
        self.assertEqual(scanner.bucket_bounds("81°F or below"), (-math.inf, 81.5))

        or_above = scanner.parse_bucket("82°F or above")
        self.assertEqual(or_above.kind, "open_above_inclusive")
        self.assertEqual(scanner.bucket_bounds("82°F or above"), (81.5, math.inf))

    def test_bucket_bounds_celsius_range_without_degree_symbol(self):
        bucket = scanner.parse_bucket("18-19C")
        self.assertEqual(bucket.unit, "C")
        self.assertEqual((bucket.low_int, bucket.high_int), (18, 19))
        lo, hi = scanner.bucket_bounds("18-19C")
        self.assertAlmostEqual(lo, 64.4, places=1)
        self.assertAlmostEqual(hi, 66.2, places=1)

    def test_bucket_bounds_single_celsius(self):
        lo, hi = scanner.bucket_bounds("19°C")
        self.assertAlmostEqual(lo, 65.3, places=1)
        self.assertAlmostEqual(hi, 67.1, places=1)

    def test_market_family_and_ease(self):
        family = scanner.market_family("Highest temperature in London on May 8?")
        self.assertEqual(family, "daily_temperature")
        score = scanner.ease_score(family, "high", 0.02, "clob_book")
        self.assertGreaterEqual(score, 8.0)

    def test_signal_requires_edge_over_spread_and_uncertainty(self):
        args = argparse.Namespace(edge_threshold=0.02, max_spread=0.10, max_entry=0.95)
        sig, reason = scanner.classify_signal(
            edge=0.03,
            entry_price=0.44,
            source_conf="high",
            spread=0.04,
            threshold_status="unknown",
            args=args,
            uncertainty_margin=0.02,
            market_family_name="daily_temperature",
            ease=9.0,
        )
        self.assertEqual(sig, "watch")
        self.assertIn("required_edge", reason)

    def test_touched_threshold_signal_class(self):
        args = argparse.Namespace(edge_threshold=0.02, max_spread=0.10, max_entry=0.95)
        sig, reason = scanner.classify_signal(
            edge=0.20,
            entry_price=0.70,
            source_conf="high",
            spread=0.01,
            threshold_status="already_touched",
            args=args,
            uncertainty_margin=0.02,
            market_family_name="daily_temperature",
            ease=9.0,
        )
        self.assertEqual(sig, "paper_buy_touched_threshold")

    def test_settlement_states_follow_threshold_exact_range_semantics(self):
        threshold = scanner.parse_bucket("80°F+")
        state = scanner.settlement_state_for_bucket(threshold, 80.0, "Yes", "2026-05-09", "2026-05-09")
        self.assertEqual(state.early_state, "yes_certain")
        no_state = scanner.settlement_state_for_bucket(threshold, 80.0, "No", "2026-05-09", "2026-05-09")
        self.assertEqual(no_state.early_state, "no_impossible")
        final_no = scanner.settlement_state_for_bucket(threshold, 79.0, "Yes", "2026-05-09", "2026-05-10")
        self.assertEqual(final_no.final_state, "final_no")

        exact = scanner.parse_bucket("80°F")
        touched = scanner.settlement_state_for_bucket(exact, 80.0, "Yes", "2026-05-09", "2026-05-09")
        self.assertEqual(touched.early_state, "still_possible")
        exceeded = scanner.settlement_state_for_bucket(exact, 81.0, "Yes", "2026-05-09", "2026-05-09")
        self.assertEqual(exceeded.early_state, "yes_impossible")
        final_exact = scanner.settlement_state_for_bucket(exact, 80.0, "Yes", "2026-05-09", "2026-05-10")
        self.assertEqual(final_exact.final_state, "final_yes")

        bucket = scanner.parse_bucket("80-82°F")
        range_possible = scanner.settlement_state_for_bucket(bucket, 81.0, "Yes", "2026-05-09", "2026-05-09")
        self.assertEqual(range_possible.early_state, "still_possible")
        range_exceeded = scanner.settlement_state_for_bucket(bucket, 83.0, "Yes", "2026-05-09", "2026-05-09")
        self.assertEqual(range_exceeded.early_state, "yes_impossible")

    def test_event_key_uses_city_date_source_station_and_rule(self):
        key = scanner.event_key_for("New York", "2026-05-09", "Weather Underground", "KLGA", "high >= 80")
        same = scanner.event_key_for("new york", "2026-05-09", "weather underground", "klga", "high >= 80")
        other_station = scanner.event_key_for("New York", "2026-05-09", "Weather Underground", "KJFK", "high >= 80")
        other_rule = scanner.event_key_for("New York", "2026-05-09", "Weather Underground", "KLGA", "high >= 81")
        self.assertEqual(key, same)
        self.assertNotEqual(key, other_station)
        self.assertNotEqual(key, other_rule)

    def test_complement_arb_detector_requires_depth_and_fresh_quotes(self):
        rows = [
            {"outcome": "Yes", "ask": 0.42, "depth": 5.0, "quote_age_seconds": 10},
            {"outcome": "No", "ask": 0.55, "depth": 5.0, "quote_age_seconds": 10},
        ]
        arb = scanner.detect_complement_arbitrage(rows, margin=0.01, min_depth=2.0, max_quote_age_seconds=60)
        self.assertTrue(arb["is_arb"])
        self.assertEqual(arb["candidate_trade"], "buy_yes_and_no")

        stale = scanner.detect_complement_arbitrage(
            [{**rows[0], "quote_age_seconds": 90}, rows[1]],
            margin=0.01,
            min_depth=2.0,
            max_quote_age_seconds=60,
        )
        self.assertFalse(stale["is_arb"])
        self.assertEqual(stale["status"], "stale_quote")

        thin = scanner.detect_complement_arbitrage(
            [{**rows[0], "depth": 1.0}, rows[1]],
            margin=0.01,
            min_depth=2.0,
            max_quote_age_seconds=60,
        )
        self.assertFalse(thin["is_arb"])
        self.assertEqual(thin["status"], "insufficient_depth")

    def test_strategy_family_classification(self):
        self.assertEqual(scanner.classify_strategy_family("watch"), "watch")
        self.assertEqual(scanner.classify_strategy_family("skip"), "skip")
        self.assertEqual(
            scanner.classify_strategy_family("paper_buy_complement_arb", complement_status="complement_arb"),
            "complement_arb",
        )
        self.assertEqual(
            scanner.classify_strategy_family("paper_buy_forecast_distribution", bucket_state="already_won"),
            "latency_absorbing_state",
        )
        self.assertEqual(
            scanner.classify_strategy_family("paper_buy_forecast_distribution", ladder_status="ladder_violation"),
            "ladder_inconsistency",
        )
        self.assertEqual(
            scanner.classify_strategy_family("paper_buy_forecast_distribution"),
            "forecast_distribution_directional",
        )

    def test_paper_buy_survival_gate_defaults_to_bootstrap_kill_only(self):
        args = argparse.Namespace(allow_weak_families=False, disable_weak_families=True, strict_survival_gate=False)
        original = scanner.edge_validation.disabled_families
        calls = []
        def fake_disabled(_path, *, strict=False):
            calls.append(strict)
            return {"ladder_inconsistency"}
        scanner.edge_validation.disabled_families = fake_disabled
        try:
            self.assertTrue(scanner.paper_buy_survival_gate_disabled(args, "ladder_inconsistency"))
            self.assertFalse(scanner.paper_buy_survival_gate_disabled(args, "forecast_distribution_directional"))
            self.assertEqual(calls, [False])
        finally:
            scanner.edge_validation.disabled_families = original

    def test_paper_buy_survival_gate_strict_mode_disables_non_promoted(self):
        args = argparse.Namespace(allow_weak_families=False, disable_weak_families=True, strict_survival_gate=True)
        original = scanner.edge_validation.disabled_families
        calls = []
        def fake_disabled(_path, *, strict=False):
            calls.append(strict)
            return {"latency_absorbing_state"}
        scanner.edge_validation.disabled_families = fake_disabled
        try:
            self.assertTrue(scanner.paper_buy_survival_gate_disabled(args, "latency_absorbing_state"))
            self.assertEqual(calls, [True])
        finally:
            scanner.edge_validation.disabled_families = original

    def test_paper_buy_survival_gate_can_be_overridden_for_research_smoke(self):
        args = argparse.Namespace(allow_weak_families=True, disable_weak_families=True)
        original = scanner.edge_validation.disabled_families
        scanner.edge_validation.disabled_families = lambda _path: {"ladder_inconsistency"}
        try:
            self.assertFalse(scanner.paper_buy_survival_gate_disabled(args, "ladder_inconsistency"))
        finally:
            scanner.edge_validation.disabled_families = original

    def test_ladder_diagnostics_flags_gaps(self):
        rows = [
            {"outcome": "80F", "model_prob": 0.20, "entry_price": 0.20},
            {"outcome": "81F", "model_prob": 0.35, "entry_price": 0.10},
            {"outcome": "82F", "model_prob": 0.25, "entry_price": 0.25},
        ]
        diag = scanner.ladder_diagnostics(rows)
        self.assertIn("underpriced_bucket", diag)

    def test_parser_unicode_negative_and_tail_buckets(self):
        negative = scanner.parse_bucket("−5–−1°F")
        self.assertIsNotNone(negative)
        self.assertEqual((negative.low_int, negative.high_int), (-5, -1))
        self.assertAlmostEqual(negative.lo, -5.5)
        self.assertAlmostEqual(negative.hi, -0.5)

        celsius = scanner.parse_bucket("≤ −10℃")
        self.assertIsNotNone(celsius)
        self.assertEqual(celsius.unit, "C")
        self.assertEqual(celsius.kind, "open_below_inclusive")

        tail = scanner.parse_bucket("90°F+")
        self.assertIsNotNone(tail)
        self.assertEqual(tail.kind, "open_above_inclusive")
        self.assertEqual(tail.low_int, 90)
        self.assertTrue(math.isinf(tail.hi))

    def test_portfolio_ledger_records_simulated_fill(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                max_position_pct=0.02,
                max_city_date_pct=0.10,
                max_open_exposure_pct=0.50,
                min_fill_shares=1.0,
            )
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.40,
                "ask": 0.40,
                "depth": 10.0,
                "depth_sufficient": True,
                "raw_status": "ok",
                "quote_age_seconds": 3.0,
                "stale_book_flag": False,
            }
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=1,
                created_at="2026-05-09T00:00:00+00:00",
                m={"market_id": "m1", "title": "Highest temperature in Austin on May 9?"},
                outcome="90°F+",
                token_id="token-1",
                city="Austin",
                target_date="2026-05-09",
                signal_type="paper_buy_forecast_distribution",
                quote=quote,
            )
            metrics = scanner.portfolio_metrics(db)
            self.assertAlmostEqual(metrics["cash"], 998.0)
            self.assertAlmostEqual(metrics["open_exposure"], 2.0)
            self.assertEqual(metrics["unresolved_positions"], 1)
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 1)

    def test_lifecycle_schema_links_candidate_fill_label_settlement_and_calibration(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                max_position_pct=0.02,
                max_city_date_pct=0.10,
                max_open_exposure_pct=0.50,
                min_fill_shares=1.0,
            )
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.40,
                "bid": 0.39,
                "ask": 0.40,
                "spread": 0.01,
                "depth": 10.0,
                "depth_sufficient": True,
                "raw_status": "ok",
                "quote_age_seconds": 3.0,
                "stale_book_flag": False,
            }
            bucket = scanner.parse_bucket("80°F+")
            event_key = scanner.event_key_for("Denver", "2026-05-09", "example.com", "KDEN", "high >= 80")
            candidate_key = scanner.candidate_key_for(event_key, "m-life", "Yes", "token-1", "2026-05-09T12:00:00+00:00")
            row_id = scanner.record_training_row(
                db,
                1,
                7,
                "2026-05-09T12:00:00+00:00",
                {
                    "market_id": "m-life",
                    "title": "Will Denver high temperature be 80°F or above?",
                    "source_url": "https://example.com",
                    "source_host": "example.com",
                    "station_id": "KDEN",
                    "forecast_high_f": 83.0,
                    "observed_high_f": 78.0,
                    "event_key": event_key,
                },
                "Yes",
                "token-1",
                "Denver",
                "2026-05-09",
                "KDEN",
                "station_registry",
                1,
                1,
                1,
                bucket,
                "still_possible",
                quote,
                0.40,
                0.75,
                0.35,
                "paper_buy_forecast_distribution",
                "passes paper filters",
                "high",
                "daily_temperature",
                "clean_station",
                9.0,
                0.03,
                0.08,
                event_key=event_key,
                candidate_key=candidate_key,
                strategy_family="forecast_distribution_directional",
                contract_type="threshold",
                settlement_state="still_possible",
                early_state="still_possible",
                final_state="unresolved",
                payout_mapping={"yes_condition": "final_high_f >= 79.5"},
                latent_final_high_mean_f=83.0,
                latent_final_high_sigma_f=3.5,
                local_day_complete=False,
            )
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=7,
                created_at="2026-05-09T12:00:00+00:00",
                m={"market_id": "m-life", "title": "Will Denver high temperature be 80°F or above?"},
                outcome="Yes",
                token_id="token-1",
                city="Denver",
                target_date="2026-05-09",
                signal_type="paper_buy_forecast_distribution",
                quote=quote,
                event_key=event_key,
                candidate_key=candidate_key,
                strategy_family="forecast_distribution_directional",
            )
            attempt = {
                "label_value": 1.0,
                "source_provider": "noaa_ncei",
                "attempted_at": "2026-05-11T00:00:00+00:00",
            }
            updated = scanner.apply_final_label_to_training_rows(
                db,
                {"market_id": "m-life", "outcome": "Yes", "target_date": "2026-05-09", "station_id": "KDEN"},
                attempt,
                11,
            )
            settled = scanner.settle_paper_positions_from_labels(db, "2026-05-11T00:00:00+00:00")

            self.assertEqual(row_id, 1)
            self.assertEqual(updated, 1)
            self.assertEqual(settled, 1)
            lifecycle = db.execute(
                "select order_id, fill_id, position_id, label_attempt_id, paper_settlement_id, calibration_row_id, strategy_family from lifecycle_attribution where candidate_key=?",
                (candidate_key,),
            ).fetchone()
            self.assertIsNotNone(lifecycle)
            self.assertTrue(all(lifecycle[i] is not None for i in range(6)))
            self.assertEqual(lifecycle[6], "forecast_distribution_directional")
            self.assertEqual(db.execute("select count(*) from calibration_rows").fetchone()[0], 1)
            self.assertEqual(db.execute("select contract_type from calibration_rows").fetchone()[0], "threshold")

    def test_training_row_export_writes_csv(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            db.execute(
                """
                insert into training_rows(created_at, run_id, signal_id, market_id, title, outcome, signal_type, features_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("2026-05-09T00:00:00+00:00", 7, 8, "m1", "Weather market", "90°F+", "watch", "{}"),
            )
            out = os.path.join(td, "training.csv")
            count = scanner.export_training_rows(db, out)
            self.assertEqual(count, 1)
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["market_id"], "m1")
            self.assertEqual(rows[0]["outcome"], "90°F+")

    def test_training_features_preserve_no_lookahead_and_no_labels(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.42,
                "bid": 0.40,
                "ask": 0.42,
                "spread": 0.02,
                "depth": 5.0,
                "depth_sufficient": True,
                "raw_status": "ok",
            }
            bucket = scanner.parse_bucket("80°F+")
            row_id = scanner.record_training_row(
                db,
                1,
                1,
                "2026-05-09T12:00:00+00:00",
                {
                    "market_id": "m1",
                    "title": "Highest temperature in Denver on May 9?",
                    "source_url": "https://example.com",
                    "source_host": "example.com",
                    "station_id": "KDEN",
                    "forecast_high_f": 83.0,
                    "observed_high_f": 78.0,
                },
                "80°F+",
                "token-1",
                "Denver",
                "2026-05-09",
                "KDEN",
                "station_registry",
                1,
                1,
                1,
                bucket,
                "still_possible",
                quote,
                0.40,
                0.58,
                0.16,
                "paper_buy_forecast_distribution",
                "passes paper filters",
                "high",
                "daily_temperature",
                "clean_station",
                9.0,
                0.03,
                0.08,
            )
            payload = json.loads(db.execute("select features_json from training_rows where id=?", (row_id,)).fetchone()[0])
            self.assertTrue(payload["paper_only"])
            self.assertTrue(payload["no_lookahead_enforced"])
            self.assertEqual(payload["excluded_future_source_count"], 0)
            self.assertNotIn("label_value", json.dumps(payload))
            self.assertEqual(payload["settlement_station_id_normalized"], "KDEN")

    def test_label_outcome_determination_for_numeric_and_no_tokens(self):
        value, bucket, reason = scanner.determine_label_value(83.0, "Denver high temperature", "80°F+")
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(bucket)
        self.assertEqual(value, 1.0)

        value, bucket, reason = scanner.determine_label_value(83.0, "Will Denver high temperature be 80°F or above?", "No")
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(bucket)
        self.assertEqual(value, 0.0)

    def test_labeler_writes_final_labels_without_rewriting_features(self):
        class FakeNCEIAdapter:
            def __init__(self, *args, **kwargs):
                pass

            def fetch_daily_summary(self, station_id, date):
                return weather_sources.SourceRecord(
                    provider="noaa_ncei",
                    family="ncei_daily_labels",
                    status="ok",
                    fetched_at="2026-05-10T12:00:00+00:00",
                    source_url="https://www.ncei.noaa.gov/access/services/data/v1",
                    data={"daily_high_f": 83.0},
                    provenance={"read_only": True, "station_id": station_id, "date": date},
                )

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            db = scanner.init_db(db_path)
            before_features = '{"source_quality_score": 1, "no_lookahead_enforced": true}'
            for row_id in (1, 2):
                db.execute(
                    """
                    insert into training_rows(
                      created_at, market_id, title, outcome, city, target_date,
                      station_id, station_source, source_confidence, market_family,
                      signal_type, features_json
                    )
                    values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"2026-05-0{row_id}T12:00:00+00:00",
                        "m-label",
                        "Will Denver high temperature be 80°F or above?",
                        "Yes",
                        "Denver",
                        "2020-01-01",
                        "KDEN",
                        "station_registry",
                        "medium",
                        "daily_temperature",
                        "paper_buy_forecast_distribution",
                        before_features,
                    ),
                )
            db.commit()
            db.close()

            original = weather_sources.NOAADelayedLabelAdapter
            weather_sources.NOAADelayedLabelAdapter = FakeNCEIAdapter
            try:
                args = argparse.Namespace(
                    db=db_path,
                    limit=10,
                    min_age_days=0,
                    retry_after_hours=0,
                    pause=0.0,
                    http_timeout=1.0,
                    cache_ttl=1,
                    enable_ncei=True,
                    enable_nws=False,
                    enable_iem=False,
                    dry_run=False,
                    no_settle=True,
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    scanner.label(args)
            finally:
                weather_sources.NOAADelayedLabelAdapter = original

            db = scanner.init_db(db_path)
            rows = db.execute(
                "select label_status, label_value, label_source, features_json from training_rows order by id"
            ).fetchall()
            self.assertEqual([row[0] for row in rows], ["final", "final"])
            self.assertEqual([row[1] for row in rows], [1.0, 1.0])
            self.assertTrue(all(str(row[2]).startswith("noaa_ncei:attempt:") for row in rows))
            self.assertEqual([row[3] for row in rows], [before_features, before_features])
            self.assertEqual(db.execute("select count(*) from label_attempts where outcome_status='final'").fetchone()[0], 1)

    def test_label_candidate_groups_prioritizes_stationed_rows_and_metric_retry_key(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            db.execute(
                """
                insert into training_rows(
                  created_at, market_id, title, outcome, city, target_date,
                  station_id, station_source, source_confidence, market_family,
                  target_metric, signal_type
                ) values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "2020-01-01T00:00:00+00:00",
                    "m-missing-station",
                    "Will the highest temperature in Denver be 80°F or above?",
                    "Yes",
                    "Denver",
                    "2020-01-01",
                    "",
                    "parser",
                    "low",
                    "daily_temperature",
                    scanner.TARGET_METRIC_DAILY_HIGH,
                    "paper_buy_test",
                ),
            )
            db.execute(
                """
                insert into training_rows(
                  created_at, market_id, title, outcome, city, target_date,
                  station_id, station_source, source_confidence, market_family,
                  target_metric, signal_type
                ) values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "2020-01-02T00:00:00+00:00",
                    "m-stationed",
                    "Will the highest temperature in Denver be 80°F or above?",
                    "Yes",
                    "Denver",
                    "2020-01-02",
                    "KDEN",
                    "station_registry",
                    "high",
                    "daily_temperature",
                    scanner.TARGET_METRIC_DAILY_HIGH,
                    "paper_buy_test",
                ),
            )
            db.commit()

            candidates = scanner.label_candidate_groups(db, "2020-01-10", "1999-01-01T00:00:00+00:00", 1)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["market_id"], "m-stationed")
            self.assertEqual(candidates[0]["station_id"], "KDEN")
            self.assertEqual(candidates[0]["target_metric"], scanner.TARGET_METRIC_DAILY_HIGH)

            db.execute(
                """
                insert into label_attempts(
                  attempted_at, market_id, title, outcome, target_date, station_id,
                  source_provider, outcome_status, target_metric
                ) values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "2020-01-09T00:00:00+00:00",
                    "m-stationed",
                    "Will the highest temperature in Denver be 80°F or above?",
                    "Yes",
                    "2020-01-02",
                    "KDEN",
                    "noaa_ncei",
                    scanner.PENDING_LABEL_STATUS,
                    scanner.TARGET_METRIC_DAILY_HIGH,
                ),
            )
            db.commit()

            retried = scanner.label_candidate_groups(db, "2020-01-10", "2020-01-08T00:00:00+00:00", 1)
            self.assertEqual(len(retried), 1)
            self.assertEqual(retried[0]["market_id"], "m-missing-station")

    def test_label_based_settlement_preserves_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                max_position_pct=0.02,
                max_city_date_pct=0.10,
                max_open_exposure_pct=0.50,
                min_fill_shares=1.0,
            )
            quote = {
                "execution_source": "clob_book",
                "entry_price": 0.40,
                "ask": 0.40,
                "depth": 10.0,
                "depth_sufficient": True,
                "raw_status": "ok",
                "quote_age_seconds": 3.0,
                "stale_book_flag": False,
            }
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=1,
                created_at="2026-05-09T00:00:00+00:00",
                m={"market_id": "m-settle", "title": "Will Denver high temperature be 80°F or above?"},
                outcome="Yes",
                token_id="token-1",
                city="Denver",
                target_date="2026-05-09",
                signal_type="paper_buy_forecast_distribution",
                quote=quote,
            )
            db.execute(
                """
                insert into training_rows(
                  created_at, market_id, title, outcome, target_date, station_id,
                  market_family, label_status, label_value, label_source, labeled_at
                )
                values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "2026-05-09T00:00:00+00:00",
                    "m-settle",
                    "Will Denver high temperature be 80°F or above?",
                    "Yes",
                    "2026-05-09",
                    "KDEN",
                    "daily_temperature",
                    "final",
                    1.0,
                    "noaa_ncei:attempt:1",
                    "2026-05-11T00:00:00+00:00",
                ),
            )

            settled = scanner.settle_paper_positions_from_labels(db, "2026-05-11T00:00:00+00:00")
            metrics = scanner.portfolio_metrics(db)

            self.assertEqual(settled, 1)
            self.assertAlmostEqual(metrics["cash"], 1003.0)
            self.assertAlmostEqual(metrics["realized_pnl"], 3.0)
            self.assertAlmostEqual(metrics["equity"], 1003.0)
            self.assertEqual(metrics["unresolved_positions"], 0)
            self.assertEqual(db.execute("select count(*) from paper_settlements").fetchone()[0], 1)

    def test_init_db_migrates_old_schema_with_lifecycle_columns(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "old-paper.sqlite3")
            old = sqlite3.connect(db_path)
            old.executescript(
                """
                create table markets (
                  market_id text primary key, title text not null, url text, first_seen text not null, last_seen text not null
                );
                create table signals (
                  id integer primary key, run_id integer not null, market_id text not null, title text not null,
                  city text, target_date text, forecast_high_f real, outcome text not null,
                  market_prob real, model_prob real, edge real, created_at text not null
                );
                create table training_rows (
                  id integer primary key, run_id integer, signal_id integer, created_at text not null,
                  market_id text, title text, outcome text
                );
                create table paper_positions (
                  id integer primary key, account_id integer not null, market_id text not null,
                  outcome text not null, shares real not null, avg_price real not null,
                  cost_basis real not null, status text not null, updated_at text not null
                );
                """
            )
            old.close()

            db = scanner.init_db(db_path)
            try:
                signal_cols = {row[1] for row in db.execute("pragma table_info(signals)")}
                training_cols = {row[1] for row in db.execute("pragma table_info(training_rows)")}
                position_cols = {row[1] for row in db.execute("pragma table_info(paper_positions)")}
                tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}

                for col in ("event_key", "candidate_key", "strategy_family", "settlement_state", "ladder_violation_type"):
                    self.assertIn(col, signal_cols)
                    self.assertIn(col, training_cols)
                for col in ("event_key", "strategy_family"):
                    self.assertIn(col, position_cols)
                for table in ("events", "contract_payouts", "event_exposure_snapshots", "lifecycle_attribution", "calibration_rows", "shadow_orders", "shadow_fills"):
                    self.assertIn(table, tables)
            finally:
                db.close()

    def test_goal_tuning_scaffold_guardrails(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "goal.yaml")
            self.assertTrue(scanner.write_default_goal(path))
            text, exists = scanner.load_goal_text(path)
            self.assertTrue(exists)
            self.assertTrue(scanner.goal_guardrails_ok(text))
            self.assertIn("promotion: propose_only", text)

    def test_tune_command_appends_safe_iteration_log(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "paper.sqlite3")
            goal_path = os.path.join(td, "goal.yaml")
            log_path = os.path.join(td, "iterations.jsonl")
            db = scanner.init_db(db_path)
            db.close()
            self.assertTrue(scanner.write_default_goal(goal_path))

            args = argparse.Namespace(
                db=db_path,
                goal=goal_path,
                init_goal=False,
                overwrite_goal=False,
                iteration_log=log_path,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                scanner.tune(args)

            with open(log_path, encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "insufficient_data")
            self.assertEqual(records[0]["evidence_counts"]["training_rows"], 0)
            self.assertTrue(records[0]["safety"]["paper_only"])
            self.assertFalse(records[0]["safety"]["live_trading"])
            self.assertFalse(records[0]["approval"]["order_placement"])

    def test_survival_gated_family_records_shadow_fill_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                min_fill_shares=1.0,
                max_position_pct=0.50,
                max_city_date_pct=0.50,
                max_open_exposure_pct=0.50,
                allow_weak_families=False,
                disable_weak_families=True,
                strict_survival_gate=False,
                _paper_buy_disabled_families={"killed_family"},
            )
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=2,
                created_at="2026-05-09T00:00:00Z",
                m={"market_id": "m1", "title": "Weather test"},
                outcome="Yes",
                token_id="tok1",
                city="New York",
                target_date="2026-05-09",
                signal_type="paper_buy_edge",
                quote={
                    "entry_price": 0.40,
                    "ask": 0.40,
                    "depth": 10.0,
                    "depth_sufficient": True,
                    "execution_source": "clob_book",
                    "raw_status": "ok",
                },
                event_key="event1",
                candidate_key="cand1",
                strategy_family="killed_family",
            )
            self.assertEqual(db.execute("select status, reason from paper_orders").fetchone(), ("skipped", "strategy_family_survival_gate_disabled"))
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 0)
            self.assertEqual(db.execute("select count(*) from paper_positions").fetchone()[0], 0)
            self.assertEqual(db.execute("select cash from paper_accounts where name=?", (scanner.active_paper_account_name(),)).fetchone()[0], 1000.0)
            self.assertEqual(db.execute("select shadow_reason, strategy_family from shadow_orders").fetchone(), ("survival_gate_disabled", "killed_family"))
            self.assertEqual(db.execute("select shares, price, cost, source from shadow_fills").fetchone(), (5.0, 0.40, 2.0, "clob_book"))
            db.close()

    def test_dust_entry_below_min_entry_never_fills_official_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                min_fill_shares=1.0,
                min_entry=0.02,
                max_position_pct=0.50,
                max_city_date_pct=0.50,
                max_open_exposure_pct=0.50,
                allow_weak_families=True,
                disable_weak_families=False,
                strict_survival_gate=False,
            )
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=2,
                created_at="2026-05-09T00:00:00Z",
                m={"market_id": "dust", "title": "Dust weather test"},
                outcome="Yes",
                token_id="tok-dust",
                city="New York",
                target_date="2026-05-09",
                signal_type="paper_buy_ladder",
                quote={
                    "entry_price": 0.001,
                    "ask": 0.001,
                    "depth": 10000.0,
                    "depth_sufficient": True,
                    "execution_source": "clob_book",
                    "raw_status": "ok",
                },
                event_key="event-dust",
                candidate_key="cand-dust",
                strategy_family="ladder_inconsistency",
            )
            self.assertEqual(db.execute("select status, reason from paper_orders").fetchone(), ("skipped", "below_min_entry_shadow_only"))
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 0)
            self.assertEqual(db.execute("select count(*) from paper_positions").fetchone()[0], 0)
            self.assertEqual(db.execute("select shadow_reason from shadow_orders").fetchone()[0], "below_min_entry_shadow_only")
            self.assertEqual(db.execute("select cash from paper_accounts where name=?", (scanner.active_paper_account_name(),)).fetchone()[0], 1000.0)
            db.close()

    def test_min_entry_blocks_dust_for_all_strategy_families(self):
        families = [
            "ladder_inconsistency",
            "forecast_distribution_directional",
            "latency_absorbing_state",
            "settlement_source_delta",
            "complement_arb",
            "unknown",
        ]
        for family in families:
            with self.subTest(family=family), tempfile.TemporaryDirectory() as td:
                db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
                args = argparse.Namespace(
                    disable_ledger=False,
                    paper_size=5.0,
                    min_fill_shares=1.0,
                    min_entry=0.02,
                    max_entry=0.95,
                    max_spread=0.08,
                    max_position_pct=0.50,
                    max_city_date_pct=0.50,
                    max_open_exposure_pct=0.50,
                    allow_weak_families=True,
                    disable_weak_families=False,
                    strict_survival_gate=False,
                    shadow_ladder_inconsistency=False,
                    shadow_strategy_families="",
                )
                scanner.simulate_paper_order(
                    db,
                    args,
                    run_id=1,
                    signal_id=2,
                    created_at="2026-05-09T00:00:00Z",
                    m={"market_id": f"dust-{family}", "title": "Dust weather test"},
                    outcome="Yes",
                    token_id="tok-dust",
                    city="New York",
                    target_date="2026-05-09",
                    signal_type="paper_buy_edge",
                    quote={
                        "entry_price": 0.001,
                        "ask": 0.001,
                        "spread": 0.001,
                        "depth": 10000.0,
                        "depth_sufficient": True,
                        "execution_source": "clob_book",
                        "quote_age_seconds": 1.0,
                        "stale_book_flag": False,
                        "raw_status": "ok",
                    },
                    event_key=f"event-dust-{family}",
                    candidate_key=f"cand-dust-{family}",
                    strategy_family=family,
                )
                self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 0)
                self.assertEqual(db.execute("select reason from paper_orders").fetchone()[0], "below_min_entry_shadow_only")
                self.assertEqual(db.execute("select count(*) from shadow_fills").fetchone()[0], 1)
                db.close()

    def test_ladder_inconsistency_shadow_mode_preserves_counterfactual_without_official_fill(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                min_fill_shares=1.0,
                min_entry=0.02,
                max_position_pct=0.50,
                max_city_date_pct=0.50,
                max_open_exposure_pct=0.50,
                allow_weak_families=True,
                disable_weak_families=False,
                strict_survival_gate=False,
                shadow_ladder_inconsistency=True,
            )
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=2,
                created_at="2026-05-09T00:00:00Z",
                m={"market_id": "ladder", "title": "Ladder weather test"},
                outcome="Yes",
                token_id="tok-ladder",
                city="New York",
                target_date="2026-05-09",
                signal_type="paper_buy_ladder",
                quote={
                    "entry_price": 0.40,
                    "ask": 0.40,
                    "depth": 10.0,
                    "depth_sufficient": True,
                    "execution_source": "clob_book",
                    "raw_status": "ok",
                },
                event_key="event-ladder",
                candidate_key="cand-ladder",
                strategy_family="ladder_inconsistency",
            )
            self.assertEqual(db.execute("select status, reason from paper_orders").fetchone(), ("skipped", "strategy_family_shadow_only"))
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 0)
            self.assertEqual(db.execute("select count(*) from paper_positions").fetchone()[0], 0)
            self.assertEqual(db.execute("select shadow_reason, strategy_family from shadow_orders").fetchone(), ("ladder_shadow_until_labels", "ladder_inconsistency"))
            self.assertEqual(db.execute("select shares, price, cost, source from shadow_fills").fetchone(), (5.0, 0.40, 2.0, "clob_book"))
            db.close()

    def test_ladder_inconsistency_shadow_only_before_calibration_even_with_override(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            args = argparse.Namespace(
                disable_ledger=False,
                paper_size=5.0,
                min_fill_shares=1.0,
                min_entry=0.02,
                max_entry=0.95,
                max_spread=0.08,
                max_position_pct=0.50,
                max_city_date_pct=0.50,
                max_open_exposure_pct=0.50,
                allow_weak_families=True,
                disable_weak_families=False,
                strict_survival_gate=False,
                shadow_ladder_inconsistency=False,
                shadow_strategy_families="",
            )
            scanner.simulate_paper_order(
                db,
                args,
                run_id=1,
                signal_id=2,
                created_at="2026-05-09T00:00:00Z",
                m={"market_id": "ladder-override", "title": "Ladder weather test"},
                outcome="Yes",
                token_id="tok-ladder",
                city="New York",
                target_date="2026-05-09",
                signal_type="paper_buy_ladder",
                quote={
                    "entry_price": 0.40,
                    "ask": 0.40,
                    "spread": 0.01,
                    "depth": 10.0,
                    "depth_sufficient": True,
                    "execution_source": "clob_book",
                    "quote_age_seconds": 1.0,
                    "stale_book_flag": False,
                    "raw_status": "ok",
                },
                event_key="event-ladder-override",
                candidate_key="cand-ladder-override",
                strategy_family="ladder_inconsistency",
            )
            self.assertEqual(db.execute("select count(*) from paper_fills").fetchone()[0], 0)
            self.assertEqual(db.execute("select shadow_reason from shadow_orders").fetchone()[0], "ladder_shadow_until_labels")
            db.close()

    def test_touch_watchlist_seed_activates_near_threshold_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            bucket = scanner.parse_bucket("90 or above")
            spec = scanner.contract_spec_for_bucket(bucket, "Yes")
            scanner.seed_touch_watchlist_candidate(
                db,
                event_key="event-touch",
                market={"market_id": "m-touch", "title": "Touch watchlist test"},
                outcome="Yes",
                token_id="tok-touch",
                city="New York",
                station_id="KNYC",
                target_date="2026-05-09",
                strategy_family="latency_absorbing_state",
                contract_spec=spec,
                observed_high_f=89.0,
                quote={"ask": 0.40, "bid": 0.39, "depth": 25.0, "depth_sufficient": True},
                source_confidence="high",
                timezone_name="America/New_York",
                now_iso="2026-05-09T16:00:00Z",
            )
            row = db.execute(
                "select active, threshold_f, current_high_f, distance_to_threshold_f, last_seen_ask, strategy_family "
                "from touch_watchlist where event_key=? and market_id=? and token_id=?",
                ("event-touch", "m-touch", "tok-touch"),
            ).fetchone()
            self.assertEqual(row, (1, 89.5, 89.0, 0.5, 0.40, "latency_absorbing_state"))
            db.close()

    def test_labeler_uses_enabled_read_only_final_label_sources_by_default(self):
        args = scanner.build_parser().parse_args(["label", "--dry-run", "--limit", "0"])
        # NCEI is disabled by default (runtime_tunables.env ENABLE_NCEI_DAILY=0)
        # because www.ncei.noaa.gov stalls on SSL handshake, causing labeler
        # hangs (fix commit eb25d8a). Source weight is 0.00 so disabling has
        # zero prediction impact.
        self.assertFalse(args.enable_ncei)
        self.assertTrue(args.enable_nws)
        self.assertTrue(args.enable_iem)

    def test_no_live_trading_guard_blocks_prohibited_options(self):
        self.assertTrue(scanner.ensure_paper_only_guard(argparse.Namespace()))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(live_trading=True))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(private_key="not-allowed"))

    def test_reconcile_zombie_positions_syncs_stale_open_to_settled(self):
        """Positions stuck in 'open' with an existing settlement record should be reconciled."""
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            # Simulate a position + settlement (the zombie state)
            now = "2026-07-03T21:00:00+00:00"
            acct = scanner.account_row(db)
            account_id = int(acct[0])
            db.execute(
                """insert into paper_positions
                   (account_id, market_id, outcome, shares, avg_price, cost_basis,
                    realized_pnl, status, updated_at, strategy_family)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (account_id, "m-zombie", "Yes", 100.0, 0.05, 5.0, 0.0, "open", "2026-06-06T00:00:00+00:00", "ladder_inconsistency"),
            )
            pos_id = db.execute("select last_insert_rowid()").fetchone()[0]
            db.execute(
                """insert into paper_settlements
                   (position_id, settled_at, outcome_status, payout, realized_pnl,
                    event_key, strategy_family)
                   values(?,?,?,?,?,?,?)""",
                (pos_id, "2026-05-09T05:00:00+00:00", "resolved_loss", 0.0, -5.0, "city-2026-05-09", "ladder_inconsistency"),
            )
            db.commit()
            # Before reconcile: position is open
            self.assertEqual(
                db.execute("select status from paper_positions where id=?", (pos_id,)).fetchone()[0],
                "open",
            )
            fixed = scanner.reconcile_zombie_positions(db, now)
            db.commit()
            self.assertEqual(fixed, 1)
            # After reconcile: position is settled with correct realized_pnl
            row = db.execute("select status, realized_pnl from paper_positions where id=?", (pos_id,)).fetchone()
            self.assertEqual(row[0], "settled")
            self.assertAlmostEqual(row[1], -5.0)

    def test_reconcile_does_not_touch_positions_without_settlements(self):
        """Positions genuinely open (no settlement) must not be touched."""
        with tempfile.TemporaryDirectory() as td:
            db = scanner.init_db(os.path.join(td, "paper.sqlite3"))
            now = "2026-07-03T21:00:00+00:00"
            acct = scanner.account_row(db)
            account_id = int(acct[0])
            db.execute(
                """insert into paper_positions
                   (account_id, market_id, outcome, shares, avg_price, cost_basis,
                    realized_pnl, status, updated_at, strategy_family)
                   values(?,?,?,?,?,?,?,?,?,?)""",
                (account_id, "m-real", "Yes", 100.0, 0.05, 5.0, 0.0, "open", now, "ladder_inconsistency"),
            )
            db.commit()
            fixed = scanner.reconcile_zombie_positions(db, now)
            self.assertEqual(fixed, 0)
            pos_id = db.execute("select id from paper_positions where market_id='m-real'").fetchone()[0]
            self.assertEqual(
                db.execute("select status from paper_positions where id=?", (pos_id,)).fetchone()[0],
                "open",
            )


if __name__ == "__main__":
    unittest.main()
