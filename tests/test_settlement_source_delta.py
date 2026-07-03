import argparse
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import features
import scanner
import weather_sources


class SettlementSourceDeltaTests(unittest.TestCase):
    def open_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        db = scanner.init_db(tmp.name)
        self.addCleanup(lambda: db.close())
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return db

    def test_source_snapshot_registry_and_delta_features_record_all_statuses(self):
        db = self.open_db()
        for provider, status, high in (("wu", "success", 81.0), ("nws", "error", None), ("iem", "skipped", None), ("meteostat", "success", 83.0)):
            scanner.record_source_observation_snapshot(
                db,
                run_id=1,
                market_id="m1",
                event_key="nyc|2026-05-09|high",
                provider=provider,
                family="observation",
                status=status,
                station_id="KLGA",
                target_metric="daily_high",
                observed_f=high,
                observed_high_f=high,
                fetched_at="2026-05-09T20:00:00+00:00",
                source_url=f"https://example.test/{provider}",
                provenance={"public": True},
            )
        db.commit()
        self.assertEqual(db.execute("select count(*) from source_observation_snapshots").fetchone()[0], 4)
        self.assertEqual(db.execute("select count(*) from settlement_source_registry").fetchone()[0], 4)
        delta = scanner.source_delta_features(db, "nyc|2026-05-09|high", "m1")
        self.assertEqual(delta["source_delta_status"], "ready")
        self.assertEqual(delta["source_delta_provider_count"], 4)
        self.assertEqual(delta["source_delta_high_range_f"], 2.0)

    def test_label_attempt_snapshot_helper_persists_adapter_status_row(self):
        db = self.open_db()
        record = weather_sources.SourceRecord(
            provider="nws",
            family="daily_high",
            status="error",
            fetched_at="2026-05-10T12:00:00+00:00",
            source_url="https://api.weather.gov/stations/KLGA/observations",
            data={},
            provenance={"read_only": True, "requires_credentials": False},
            error="public adapter unavailable",
        )
        attempt = scanner.build_label_attempt(
            {"market_id": "m-status", "outcome": "80F+", "target_date": "2026-05-09", "station_id": "KLGA"},
            attempted_at="2026-05-10T12:00:00+00:00",
            source_record=record,
            source_provider="nws",
            source_family="daily_high",
            source_status="error",
            outcome_status=scanner.ERROR_LABEL_STATUS,
            reason="adapter_error",
        )
        scanner.record_source_snapshot_from_label_attempt(db, candidate={"market_id": "m-status", "station_id": "KLGA"}, attempt=attempt)
        row = db.execute("select provider, family, status, station_id, error from source_observation_snapshots").fetchone()
        self.assertEqual(row, ("nws", "daily_high", "error", "KLGA", "adapter_error"))

    def test_enabled_adapters_write_skipped_snapshots_when_station_missing(self):
        db = self.open_db()
        args = argparse.Namespace(
            enable_ncei=True,
            enable_nws=True,
            enable_iem=True,
            enable_metar_direct=True,
            enable_meteostat=True,
        )
        candidate = {
            "training_row_id": 1,
            "market_id": "m-missing-station",
            "title": "Will Denver high temperature be 80°F or above?",
            "outcome": "Yes",
            "target_date": "2026-05-09",
            "station_id": None,
        }

        attempts = scanner.attempt_delayed_label(candidate, args, "2026-05-10T12:00:00+00:00")
        self.assertEqual({attempt["reason"] for attempt in attempts}, {"no_station_id"})
        self.assertEqual(len(attempts), 4)
        for attempt in attempts:
            scanner.insert_label_attempt(db, attempt)
            scanner.record_source_snapshot_from_label_attempt(db, candidate=candidate, attempt=attempt)

        rows = db.execute(
            "select source_provider, status, error from source_observation_snapshots order by source_provider"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row[1] == "no_station_id" and row[2] == "no_station_id" for row in rows))

    def test_labeler_records_error_attempt_without_crashing(self):
        db = self.open_db()

        class FakeNCEIAdapter:
            def __init__(self, *args, **kwargs):
                pass

            def fetch_daily_summary(self, station_id, date):
                return weather_sources.SourceRecord(
                    provider="noaa_ncei",
                    family="ncei_daily_labels",
                    status="error",
                    fetched_at="2026-05-10T12:00:00+00:00",
                    source_url="https://example.test/ncei",
                    provenance={"read_only": True},
                    error="simulated fetch failure",
                )

        original = weather_sources.NOAADelayedLabelAdapter
        weather_sources.NOAADelayedLabelAdapter = FakeNCEIAdapter
        try:
            args = argparse.Namespace(
                enable_ncei=True,
                enable_nws=False,
                enable_iem=False,
                enable_metar_direct=False,
                enable_meteostat=False,
                http_timeout=1.0,
                cache_ttl=1,
                pause=0.0,
            )
            candidate = {
                "training_row_id": 1,
                "market_id": "m-error",
                "title": "Will Denver high temperature be 80°F or above?",
                "outcome": "Yes",
                "target_date": "2026-05-09",
                "station_id": "ZZZZ",
            }
            attempts = scanner.attempt_delayed_label(candidate, args, "2026-05-10T12:00:00+00:00")
        finally:
            weather_sources.NOAADelayedLabelAdapter = original

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome_status"], scanner.ERROR_LABEL_STATUS)
        scanner.insert_label_attempt(db, attempts[0])
        scanner.record_source_snapshot_from_label_attempt(db, candidate=candidate, attempt=attempts[0])
        self.assertEqual(db.execute("select outcome_status, reason from label_attempts").fetchone(), ("error", "simulated fetch failure"))
        self.assertEqual(db.execute("select status, error from source_observation_snapshots").fetchone(), ("error", "simulated fetch failure"))

    def test_source_delta_is_shadow_only_until_calibrated_and_unambiguous(self):
        db = self.open_db()
        args = argparse.Namespace(shadow_strategy_families="")
        self.assertEqual(scanner.classify_strategy_family("paper_buy_source_delta"), "settlement_source_delta")
        self.assertEqual(scanner.paper_buy_shadow_only_family(args, "settlement_source_delta"), (True, "settlement_source_delta_shadow_locked"))
        allowed, reason = scanner.source_delta_official_guard(db, station_id=None, source_key="wu:klga")
        self.assertFalse(allowed)
        self.assertIn("ambiguous", reason)
        allowed, reason = scanner.source_delta_official_guard(db, station_id="KLGA", source_key="wu:klga")
        self.assertFalse(allowed)
        self.assertEqual(reason, "settlement_source_delta_official_locked")

    def test_station_residuals_populate_from_final_label_and_snapshot(self):
        db = self.open_db()
        scanner.record_source_observation_snapshot(
            db,
            run_id=1,
            market_id="m2",
            event_key="event2",
            provider="wu",
            family="observation",
            status="success",
            station_id="KLGA",
            target_metric="daily_high",
            observed_f=80.0,
            observed_high_f=80.0,
            fetched_at="2026-05-09T20:00:00+00:00",
        )
        cur = db.execute(
            """
            insert into training_rows(created_at, market_id, outcome, target_date, station_id, event_key, strategy_family, model_prob, label_status)
            values(?,?,?,?,?,?,?,?,?)
            """,
            ("2026-05-09T20:00:00+00:00", "m2", "80F+", "2026-05-09", "KLGA", "event2", "settlement_source_delta", 0.70, scanner.PENDING_LABEL_STATUS),
        )
        training_id = cur.lastrowid
        attempt_id = scanner.insert_label_attempt(db, {
            "attempted_at": "2026-05-10T12:00:00+00:00",
            "training_row_id": training_id,
            "market_id": "m2",
            "outcome": "80F+",
            "target_date": "2026-05-09",
            "station_id": "KLGA",
            "source_provider": "wu",
            "source_family": "observation",
            "source_status": "success",
            "target_metric": "daily_high",
            "final_observed_f": 82.0,
            "final_high_f": 82.0,
            "label_value": 1.0,
            "outcome_status": scanner.FINAL_LABEL_STATUS,
        })
        updated = scanner.apply_final_label_to_training_rows(
            db,
            {"market_id": "m2", "outcome": "80F+", "target_date": "2026-05-09", "station_id": "KLGA", "target_metric": "daily_high"},
            {"label_value": 1.0, "source_provider": "wu", "attempted_at": "2026-05-10T12:00:00+00:00", "target_metric": "daily_high", "final_observed_f": 82.0, "final_high_f": 82.0, "outcome_status": scanner.FINAL_LABEL_STATUS},
            attempt_id,
        )
        self.assertEqual(updated, 1)
        self.assertGreater(db.execute("select count(*) from calibration_rows").fetchone()[0], 0)
        row = db.execute("select sample_count, mean_residual_f, mae_f from station_residuals").fetchone()
        self.assertEqual(row, (1, 2.0, 2.0))

    def test_station_residuals_populate_from_multi_provider_proxy_consensus_calibration(self):
        db = self.open_db()
        scanner.record_source_observation_snapshot(
            db,
            run_id=1,
            market_id="m-proxy",
            event_key="event-proxy",
            provider="iem_metar",
            family="daily_high",
            status="ok",
            station_id="KDEN",
            target_metric="unknown",
            observed_f=79.0,
            observed_high_f=79.0,
            fetched_at="2026-05-10T12:00:00+00:00",
        )
        cur = db.execute(
            """
            insert into training_rows(created_at, market_id, outcome, target_date, station_id, event_key, strategy_family, model_prob, label_status, target_metric)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            ("2026-05-09T20:00:00+00:00", "m-proxy", "80F+", "2026-05-09", "KDEN", "event-proxy", "settlement_source_delta", 0.70, scanner.PENDING_LABEL_STATUS, "daily_high"),
        )
        training_id = cur.lastrowid
        attempt = {
            "attempted_at": "2026-05-12T12:00:00+00:00",
            "training_row_id": training_id,
            "market_id": "m-proxy",
            "outcome": "80F+",
            "target_date": "2026-05-09",
            "station_id": "KDEN",
            "source_provider": "proxy_consensus:iem_metar+nws",
            "source_family": "proxy_consensus",
            "source_status": "ok",
            "target_metric": "daily_high",
            "final_observed_f": 82.0,
            "final_high_f": 82.0,
            "label_value": 1.0,
            "outcome_status": scanner.MULTI_PROVIDER_PROXY_CONSENSUS_LABEL_STATUS,
        }
        attempt_id = scanner.insert_label_attempt(db, attempt)

        updated = scanner.apply_final_label_to_training_rows(
            db,
            {"market_id": "m-proxy", "outcome": "80F+", "target_date": "2026-05-09", "station_id": "KDEN", "target_metric": "daily_high"},
            attempt,
            attempt_id,
        )

        self.assertEqual(updated, 1)
        cal = db.execute("select label_confidence, provider_set from calibration_rows").fetchone()
        self.assertEqual(cal, ("multi_provider_proxy_consensus", "iem_metar+nws"))
        row = db.execute("select station_id, source_key, sample_count, mean_residual_f, mae_f from station_residuals").fetchone()
        self.assertEqual(row, ("KDEN", "iem_metar:kden", 1, 3.0, 3.0))

    def test_single_provider_proxy_does_not_create_calibration_or_residuals(self):
        db = self.open_db()
        cur = db.execute(
            """
            insert into training_rows(created_at, market_id, outcome, target_date, station_id, event_key, strategy_family, model_prob, label_status, target_metric)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            ("2026-05-09T20:00:00+00:00", "m-single", "80F+", "2026-05-09", "KDEN", "event-single", "forecast_distribution_directional", 0.70, scanner.PENDING_LABEL_STATUS, "daily_high"),
        )
        attempt = {
            "attempted_at": "2026-05-12T12:00:00+00:00",
            "training_row_id": cur.lastrowid,
            "market_id": "m-single",
            "outcome": "80F+",
            "target_date": "2026-05-09",
            "station_id": "KDEN",
            "source_provider": "proxy_consensus:nws",
            "source_family": "proxy_consensus",
            "source_status": "ok",
            "target_metric": "daily_high",
            "final_observed_f": 82.0,
            "final_high_f": 82.0,
            "label_value": 1.0,
            "outcome_status": scanner.PROXY_FINAL_LABEL_STATUS,
        }
        attempt_id = scanner.insert_label_attempt(db, attempt)
        scanner.apply_final_label_to_training_rows(
            db,
            {"market_id": "m-single", "outcome": "80F+", "target_date": "2026-05-09", "station_id": "KDEN", "target_metric": "daily_high"},
            attempt,
            attempt_id,
        )
        self.assertEqual(db.execute("select count(*) from calibration_rows").fetchone()[0], 0)
        self.assertEqual(db.execute("select count(*) from station_residuals").fetchone()[0], 0)

    def test_feature_family_exports_source_delta_without_lookahead(self):
        flat = features.build_decision_features(
            market={"strategy_family": "settlement_source_delta", "source_delta_snapshot_count": 2, "source_delta_provider_count": 2, "source_delta_high_range_f": 1.5},
            decision_time="2026-05-09T18:00:00+00:00",
        )
        self.assertIn("settlement_source_delta", flat["feature_families"])
        self.assertEqual(flat["source_delta_snapshot_count"], 2)
        self.assertEqual(flat["source_delta_shadow_only_flag"], 1)
        self.assertTrue(flat["paper_only"])


if __name__ == "__main__":
    unittest.main()
