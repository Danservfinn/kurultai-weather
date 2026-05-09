import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import features


class FeatureEngineeringTests(unittest.TestCase):
    def test_build_features_excludes_future_records_and_labels(self):
        built = features.build_decision_features(
            decision_time="2026-05-09T12:00:00+00:00",
            market={
                "created_at": "2026-05-09T11:59:00+00:00",
                "source_url": "https://example.com/rules",
                "station_id": "KDEN",
                "source_confidence": "high",
                "bucket_lo_f": 79.5,
                "bucket_hi_f": float("inf"),
                "bucket_state": "still_possible",
                "target_date": "2026-05-09",
                "timezone": "America/Denver",
                "label_value": 1.0,
            },
            forecast={"fetched_at": "2026-05-09T11:00:00+00:00", "forecast_high_f": 82.0},
            observation={"observed_at": "2026-05-09T13:00:00+00:00", "high_so_far_f": 85.0, "source": "nws"},
            orderbook={"captured_at": "2026-05-09T11:59:30+00:00", "bid": 0.41, "ask": 0.44, "spread": 0.03, "depth": 8, "depth_sufficient": True},
            source_records=[{"provider": "nws", "status": "ok", "fetched_at": "2026-05-09T13:00:00+00:00"}],
            tunables={"max_spread": 0.12, "min_fill_shares": 1.0},
        )

        self.assertTrue(built["no_lookahead_enforced"])
        self.assertEqual(built["excluded_future_source_count"], 2)
        self.assertNotEqual(built["observed_high_so_far_f"], 85.0)
        self.assertNotIn("label_value", json.dumps(built))
        self.assertEqual(built["settlement_station_id_normalized"], "KDEN")
        self.assertAlmostEqual(built["spread_pct_mid"], 0.03 / 0.425)

    def test_family_coverage_reports_missing_fields(self):
        built = features.build_decision_features(
            decision_time="2026-05-09T12:00:00+00:00",
            market={"created_at": "2026-05-09T12:00:00+00:00"},
        )
        coverage = features.family_coverage(built)

        self.assertIn("settlement_source", coverage)
        self.assertFalse(coverage["settlement_source"]["present"])
        self.assertTrue(coverage["no_lookahead"]["present"])


if __name__ == "__main__":
    unittest.main()
