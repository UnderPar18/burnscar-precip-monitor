import unittest
from datetime import datetime, timedelta, timezone

import shapely

from app.config import settings_from_dict
from app.net import redact
from app.pipeline.forecast import inches_from_qpf, parse_duration, rolling_max_inches
from app.pipeline.nbm import (
    accumulation_label,
    cog_six_hour_valids,
    cog_url,
    find_apcp,
    newest_hour,
    one_hour_leads,
    six_hour_leads,
)
from app.pipeline.geometry import buffer_geometry, sample_points
from app.pipeline.mrms import max_in_polygon, select_hourly_files, subset_window
from app.pipeline.synoptic import hourly_totals
from app.pipeline.perimeters import perimeter_where


class LogicTests(unittest.TestCase):
    def test_buffer_is_about_one_mile(self):
        import pyproj

        point = shapely.Point(-111.89, 40.76)
        buffered = buffer_geometry(point, 1)
        edge = shapely.Point(buffered.exterior.coords[0])
        _forward, _back, distance = pyproj.Geod(ellps="WGS84").inv(point.x, point.y, edge.x, edge.y)
        self.assertGreater(distance, 1500)
        self.assertLess(distance, 1720)

    def test_sample_points_stay_inside_the_buffer(self):
        buffered = buffer_geometry(shapely.Point(-114.5, 42.5), 5)
        points = sample_points(buffered, 5)
        self.assertGreaterEqual(len(points), 4)
        self.assertTrue(all(buffered.covers(point) for point in points))

    def test_mrms_history_keeps_one_file_per_hour(self):
        names = []
        for minute in (0, 2, 30, 58):
            names.append(f"MRMS_RadarOnly_QPE_01H_00.00_20261104-21{minute:02d}00.grib2.gz")
            names.append(f"MRMS_RadarOnly_QPE_01H_00.00_20261104-22{minute:02d}00.grib2.gz")
        names.append("MRMS_RadarOnly_QPE_01H_00.00_20261103-180000.grib2.gz")
        now = datetime(2026, 11, 4, 22, 40, tzinfo=timezone.utc)
        chosen = select_hourly_files(names, now, hours_back=2)
        self.assertEqual([item[0].strftime("%H%M") for item in chosen], ["2100", "2200"])

    def test_grid_window_stays_on_the_fire_buffers(self):
        transform = (0, 1, 0, 100, 0, -1)
        west = shapely.box(1.2, 2.2, 3.4, 4.6)
        window = subset_window(transform, 200, 200, [west])
        col0, row0, col1, row1 = window
        self.assertLess(col1, 20)
        self.assertGreater(row0, 80)
        self.assertLess(row1 - row0, 20)
        self.assertIsNone(subset_window(transform, 10, 10, [shapely.box(50, 50, 60, 60)]))

    def test_station_hours_keep_the_larger_report(self):
        points = hourly_totals(
            [
                {"last_report": "2026-11-04T21:04:00Z", "total": 0.02},
                {"last_report": "2026-11-04T21:46:00Z", "total": 0.08},
                {"last_report": "2026-11-04T22:00:00Z", "total": 0.01},
            ]
        )
        self.assertEqual(
            points,
            [
                {"valid": "2026-11-04T21:00:00Z", "inches": 0.08},
                {"valid": "2026-11-04T22:00:00Z", "inches": 0.01},
            ],
        )

    def test_negative_mrms_cells_are_nodata(self):
        array = [[1.5, -3.0], [4.0, 0.2]]
        transform = (0, 1, 0, 2, 0, -1)
        wet = shapely.box(0.1, 1.1, 0.9, 1.9)
        self.assertAlmostEqual(max_in_polygon(array, transform, wet), 1.5)
        dry = shapely.box(1.1, 1.1, 1.9, 1.9)
        self.assertIsNone(max_in_polygon(array, transform, dry))
        bottom = shapely.box(0.1, 0.1, 1.9, 0.9)
        self.assertAlmostEqual(max_in_polygon(array, transform, bottom), 4.0)

    def test_rolling_forecast_windows(self):
        now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
        buckets = []
        amounts = [0.1, 0.2, 0.4, 0.05]
        for index, amount in enumerate(amounts):
            start = now + timedelta(hours=6 * index)
            buckets.append((start, start + timedelta(hours=6), amount))
        self.assertAlmostEqual(rolling_max_inches(buckets, 6, 72, now), 0.4)
        self.assertAlmostEqual(rolling_max_inches(buckets, 12, 72, now), 0.6)
        self.assertAlmostEqual(rolling_max_inches(buckets, 24, 72, now), 0.75)
        self.assertIsNone(rolling_max_inches(buckets, 1, 72, now))

    def test_qpf_millimeters_become_inches(self):
        self.assertAlmostEqual(inches_from_qpf(25.4, "wmoUnit:mm"), 1.0)
        self.assertAlmostEqual(inches_from_qpf(1, "wmoUnit:in"), 1.0)
        self.assertEqual(parse_duration("PT6H"), timedelta(hours=6))

    def test_token_is_redacted(self):
        hidden = redact("https://api.example.com/precip?token=super-secret&units=english")
        self.assertNotIn("super-secret", hidden)
        self.assertIn("token=REDACTED", hidden)

    def test_state_filter_uses_wfigs_codes(self):
        settings = settings_from_dict({"states": "or, US-CO", "min_acres": 250})
        clause = perimeter_where(settings)
        self.assertIn("'US-OR'", clause)
        self.assertIn("'US-CO'", clause)
        self.assertIn("250", clause)
        with self.assertRaises(ValueError):
            settings_from_dict({"states": "DROP TABLE"})

    def test_forecast_source_defaults_to_nbm(self):
        settings = settings_from_dict({})
        self.assertEqual(settings.forecast_source, "nbm")
        with self.assertRaises(ValueError):
            settings_from_dict({"forecast_source": "hrrr"})

    def test_nbm_index_selects_deterministic_qpf(self):
        index = "\n".join(
            [
                "93:1000:d=2026110412:APCP:surface:5-6 hour acc fcst:",
                "94:2000:d=2026110412:APCP:surface:0-6 hour acc fcst:",
                "95:3500:d=2026110412:APCP:surface:0-6 hour acc fcst:prob >0.254:prob fcst 255/255",
                "96:4000:d=2026110412:TMP:surface:0-6 hour acc fcst:",
            ]
        )
        self.assertEqual(accumulation_label(6, 6), "0-6 hour acc fcst")
        self.assertEqual(find_apcp(index, "0-6 hour acc fcst"), (2000, 3499))
        self.assertEqual(six_hour_leads(24), [6, 12, 18, 24])
        self.assertEqual(one_hour_leads(4), [1, 2, 3, 4])
        self.assertIn(36, one_hour_leads(40))
        self.assertIn(39, one_hour_leads(40))
        self.assertNotIn(37, one_hour_leads(40))

    def test_cog_run_uses_the_newest_hour_and_six_hour_blocks(self):
        prefixes = [
            "blendv5.0/conus/2026/11/03/0000/",
            "blendv5.0/conus/2026/11/03/1100/",
            "blendv5.0/conus/2026/11/03/0600/",
        ]
        self.assertEqual(newest_hour(prefixes), "1100")
        run = datetime(2026, 11, 3, 11, tzinfo=timezone.utc)
        valids = cog_six_hour_valids(run, 24)
        self.assertEqual(valids[0], datetime(2026, 11, 3, 18, tzinfo=timezone.utc))
        self.assertEqual(valids[1], datetime(2026, 11, 4, 0, tzinfo=timezone.utc))
        url = cog_url(run, "qpf01", datetime(2026, 11, 3, 12, tzinfo=timezone.utc))
        self.assertIn("/2026/11/03/1100/qpf01/", url)
        self.assertTrue(url.endswith("blendv5.0_conus_qpf01_2026-11-03T11:00_2026-11-03T12:00.tif"))


if __name__ == "__main__":
    unittest.main()
