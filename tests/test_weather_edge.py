import argparse
import contextlib
import csv
import io
import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scanner
import weather_sources


class WeatherEdgeTests(unittest.TestCase):
    def test_bucket_bounds_fahrenheit_range_not_negative(self):
        self.assertEqual(scanner.bucket_bounds("81-82F"), (81.0, 82.0))
        self.assertEqual(scanner.bucket_bounds("81-82°F"), (81.0, 82.0))

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

    def test_no_live_trading_guard_blocks_prohibited_options(self):
        self.assertTrue(scanner.ensure_paper_only_guard(argparse.Namespace()))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(live_trading=True))
        with self.assertRaises(ValueError):
            scanner.ensure_paper_only_guard(argparse.Namespace(private_key="not-allowed"))


if __name__ == "__main__":
    unittest.main()
