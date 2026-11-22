import unittest

import numpy as np
import pandas as pd

from cheetah_synth.event_timeline import build_event_timeline


def _ts(s):
    return pd.Timestamp(s, tz="UTC")


def _make_journeys():
    # row 0: non-converting click
    # row 1: converting, within October — should produce a click + an install
    # row 2: converting, but censored (conversion lands after Oct) — click only, no install
    # row 3: non-converting click, timestamped before row 0 to exercise sorting
    return pd.DataFrame({
        "click_id": [100, 101, 102, 103],
        "time": [_ts("2026-10-05 12:00:00"), _ts("2026-10-05 12:00:01"),
                 _ts("2026-10-05 12:00:02"), _ts("2026-10-01 00:00:00")],
        "game_placement_id": ["00035m", "00035m", "00077q", "00035m"],
        "country": ["DE", "DE", None, "FR"],
        "converts": [False, True, True, False],
        "censored": [False, False, True, False],
        "transaction_id": [np.nan, 5001.0, 5002.0, np.nan],
        "conversion_time": [pd.NaT, _ts("2026-10-05 13:00:00"), _ts("2026-11-02 00:00:00"), pd.NaT],
        "amount": [np.nan, 10.5, 99.0, np.nan],
        "commission": [np.nan, 2.5, 20.0, np.nan],
        "payout": [np.nan, 1.0, 10.0, np.nan],
        "margin": [np.nan, 1.5, 10.0, np.nan],
    })


class TestEventTimeline(unittest.TestCase):
    def test_every_click_produces_exactly_one_click_row(self):
        timeline = build_event_timeline(_make_journeys())
        clicks = timeline[timeline["kind"] == "click"]
        self.assertEqual(len(clicks), 4)
        self.assertEqual(set(clicks["source_id"]), {"100", "101", "102", "103"})

    def test_converting_non_censored_also_produces_install_with_full_financials(self):
        timeline = build_event_timeline(_make_journeys())
        installs = timeline[timeline["kind"] == "install"]
        self.assertEqual(len(installs), 1)
        row = installs.iloc[0]
        self.assertEqual(row["source_id"], "5001")
        self.assertEqual(row["amount"], 10.5)
        self.assertEqual(row["commission"], 2.5)
        self.assertEqual(row["payout"], 1.0)
        self.assertEqual(row["margin"], 1.5)

    def test_censored_conversion_has_click_but_no_install(self):
        timeline = build_event_timeline(_make_journeys())
        self.assertNotIn("5002", set(timeline[timeline["kind"] == "install"]["source_id"]))
        self.assertIn("102", set(timeline[timeline["kind"] == "click"]["source_id"]))

    def test_output_sorted_ascending_by_event_time(self):
        timeline = build_event_timeline(_make_journeys())
        times = timeline["event_time_utc"].tolist()
        self.assertEqual(times, sorted(times))

    def test_click_rows_have_null_financials(self):
        timeline = build_event_timeline(_make_journeys())
        clicks = timeline[timeline["kind"] == "click"]
        for col in ("amount", "commission", "payout", "margin"):
            self.assertTrue(clicks[col].isna().all())


if __name__ == "__main__":
    unittest.main()
