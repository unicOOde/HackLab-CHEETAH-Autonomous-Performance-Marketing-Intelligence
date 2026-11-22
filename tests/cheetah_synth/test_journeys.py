import unittest

import numpy as np
import pandas as pd

from cheetah_synth import journeys as journeys_mod
from cheetah_synth.identities import IdAllocator

from .fixtures import make_calibration


class TestSampling(unittest.TestCase):
    def setUp(self):
        self.calibration = make_calibration()
        self.rng = np.random.default_rng(1)

    def test_sample_segments_respects_distribution(self):
        n = 50_000
        placements, countries, cvr = journeys_mod.sample_segments(self.calibration, self.rng, n)
        self.assertEqual(len(placements), n)
        frac_p1 = (placements == "P1").mean()
        # P1's calibrated share is 0.40 — large n should land within a few percentage points.
        self.assertAlmostEqual(frac_p1, 0.40, delta=0.02)

    def test_sample_timestamps_stay_within_day(self):
        day = pd.Timestamp("2026-10-05", tz="UTC")
        times = journeys_mod.sample_timestamps_in_day(self.calibration, self.rng, day, 10_000)
        self.assertTrue((times >= day).all())
        self.assertTrue((times < day + pd.Timedelta(days=1)).all())

    def test_bootstrap_duration_within_jittered_pool_range(self):
        durations = journeys_mod.bootstrap_duration(self.calibration, self.rng, 1000, jitter_sigma=0.0)
        self.assertTrue(np.isin(durations, self.calibration.duration_pool).all())


class TestMaterializeConversions(unittest.TestCase):
    def test_converting_rows_get_filled_and_non_converting_stay_empty(self):
        calibration = make_calibration()
        rng = np.random.default_rng(2)
        click_alloc = IdAllocator(1)
        txn_alloc = IdAllocator(1)

        n = 10
        click_ids = click_alloc.next_batch(n)
        times = journeys_mod.sample_timestamps_in_day(calibration, rng, calibration.month_start, n)
        df = pd.DataFrame({
            "click_id": click_ids, "time": times,
            "game_placement_id": ["P1"] * n, "country": ["DE"] * n,
            "converts": [True, False] * 5,
        })
        for col, values in journeys_mod._empty_conversion_columns(n).items():
            df[col] = values

        mask = df["converts"].to_numpy()
        journeys_mod.materialize_conversions(df, mask, calibration, rng, txn_alloc)

        converting = df[mask]
        non_converting = df[~mask]
        self.assertTrue(converting["transaction_id"].notna().all())
        self.assertTrue(converting["conversion_time"].notna().all())
        self.assertTrue((converting["conversion_time"] > converting["time"]).all())
        self.assertTrue(non_converting["transaction_id"].isna().all())
        self.assertTrue(non_converting["conversion_time"].isna().all())


if __name__ == "__main__":
    unittest.main()
