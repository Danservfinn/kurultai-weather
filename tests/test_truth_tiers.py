from __future__ import annotations

import unittest

import truth_tiers


class TruthTierTests(unittest.TestCase):
    def test_truth_tier_maps_legacy_statuses_to_canonical_tiers(self) -> None:
        self.assertEqual(truth_tiers.tier_for_label("final", "noaa_ncei"), "official_ncei")
        self.assertEqual(truth_tiers.tier_for_label("final_official", "noaa_ncei"), "official_ncei")
        self.assertEqual(
            truth_tiers.tier_for_label("final_proxy_consensus", "proxy_consensus:nws+iem"),
            "multi_provider_proxy_consensus",
        )
        self.assertEqual(
            truth_tiers.tier_for_label("final_proxy_consensus", "proxy_consensus:nws"),
            "single_provider_proxy",
        )

    def test_truth_tier_counts_expose_canonical_dashboard_buckets_only(self) -> None:
        counts = truth_tiers.tier_counts()
        self.assertIn("official_ncei", counts)
        self.assertIn("multi_provider_proxy_consensus", counts)
        self.assertIn("single_provider_proxy", counts)
        self.assertIn("provisional", counts)
        self.assertNotIn("final_proxy_consensus", counts)
        self.assertNotIn("final_labels", counts)


if __name__ == "__main__":
    unittest.main()
