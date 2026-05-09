import datetime as dt
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import weather_sources


class WeatherSourceAdapterTests(unittest.TestCase):
    def test_nws_adapter_uses_cache_and_provenance(self):
        calls = []

        def fake_fetch(url, headers, timeout):
            calls.append((url, headers, timeout))
            return json.dumps(
                {
                    "properties": {
                        "gridId": "BOU",
                        "gridX": 62,
                        "gridY": 61,
                        "forecast": "https://api.weather.gov/gridpoints/BOU/62,61/forecast",
                    }
                }
            )

        adapter = weather_sources.NWSAdapter(enabled=True, timeout_seconds=3, cache_ttl_seconds=60, fetch_text=fake_fetch)
        first = adapter.fetch_points(39.7392, -104.9903)
        second = adapter.fetch_points(39.7392, -104.9903)

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "ok")
        self.assertEqual(len(calls), 1)
        self.assertTrue(first.provenance["read_only"])
        self.assertEqual(first.provenance["timeout_seconds"], 3)
        self.assertEqual(adapter.forecast_metadata(first)["nws_grid_office"], "BOU")

    def test_metar_parser_handles_negative_temperature(self):
        parsed = weather_sources.parse_metar_report("KDEN 091653Z 01010KT 10SM FEW050 M05/M11 A3012")
        self.assertEqual(parsed["station_id"], "KDEN")
        self.assertEqual(parsed["temperature_c"], -5.0)
        self.assertAlmostEqual(parsed["temperature_f"], 23.0)

    def test_iem_and_ncei_url_builders_are_read_only_and_tokenless(self):
        start = dt.datetime(2026, 5, 9, 0, 0, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(hours=1)
        iem_url = weather_sources.IEMMetarAdapter.asos_url("kden", start, end)
        ncei_url = weather_sources.NOAADelayedLabelAdapter.ncei_daily_url("GHCND:USW00003017", "2026-05-09")

        self.assertIn("station=KDEN", iem_url)
        self.assertIn("report_type=1", iem_url)
        self.assertIn("report_type=2", iem_url)
        self.assertIn("dataset=daily-summaries", ncei_url)
        self.assertNotIn("token", ncei_url.lower())

    def test_meteostat_and_commercial_adapters_are_graceful_stubs(self):
        meteostat = weather_sources.MeteostatAdapter(enabled=False).dependency_record()
        commercial = weather_sources.CommercialWeatherAdapter("tomorrow_io", enabled=False).fetch_stub()

        self.assertEqual(meteostat.status, "disabled")
        self.assertEqual(commercial.status, "disabled")
        self.assertTrue(commercial.provenance["read_only"])
        self.assertFalse(commercial.provenance["requires_credentials"])


if __name__ == "__main__":
    unittest.main()
