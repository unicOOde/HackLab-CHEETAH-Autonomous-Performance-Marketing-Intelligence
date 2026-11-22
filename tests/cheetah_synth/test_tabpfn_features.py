import unittest

import numpy as np

from cheetah_synth import journeys as journeys_mod, tabpfn_features
from cheetah_synth.config import RunConfig, ScaleConfig
from cheetah_synth.identities import IdAllocator

from .fixtures import make_calibration


class TestTabPfnFeatures(unittest.TestCase):
    def test_every_segment_has_a_row_for_every_day_no_gaps(self):
        calibration = make_calibration(n_days=10)
        rng = np.random.default_rng(11)
        run_config = RunConfig(run_id="t", scale=ScaleConfig(scale=1.0, days=10))
        journeys, _, _, _ = journeys_mod.simulate_month(
            calibration, run_config, rng, click_alloc=IdAllocator(1), txn_alloc=IdAllocator(1),
        )
        table = tabpfn_features.build_daily_segment_table(journeys, calibration)

        n_segments = len(set(zip(journeys["game_placement_id"], journeys["country"])))
        n_days = (calibration.month_end.date() - calibration.month_start.date()).days + 1
        self.assertEqual(len(table), n_segments * n_days)
        self.assertFalse(table["clicks"].isna().any())

    def test_lag_features_have_no_future_leakage(self):
        calibration = make_calibration(n_days=10)
        rng = np.random.default_rng(12)
        run_config = RunConfig(run_id="t", scale=ScaleConfig(scale=1.0, days=10))
        journeys, _, _, _ = journeys_mod.simulate_month(
            calibration, run_config, rng, click_alloc=IdAllocator(1), txn_alloc=IdAllocator(1),
        )
        table = tabpfn_features.build_daily_segment_table(journeys, calibration)

        one_segment = table[(table["game_placement_id"] == "P1") & (table["country"] == "DE")]
        one_segment = one_segment.sort_values("date").reset_index(drop=True)
        # lag1 on row i must equal the raw value on row i-1 (and NaN on the first row).
        self.assertTrue(np.isnan(one_segment.loc[0, "clicks_lag1"]))
        for i in range(1, len(one_segment)):
            self.assertEqual(one_segment.loc[i, "clicks_lag1"], one_segment.loc[i - 1, "clicks"])


if __name__ == "__main__":
    unittest.main()
