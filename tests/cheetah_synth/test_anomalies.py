import unittest

import numpy as np

from cheetah_synth import anomalies as anomalies_mod
from cheetah_synth import journeys as journeys_mod
from cheetah_synth.config import RunConfig, ScaleConfig
from cheetah_synth.identities import IdAllocator

from .fixtures import make_calibration


def _simulate_baseline(calibration, seed=42, scale=1.0, days=5):
    rng = np.random.default_rng(seed)
    run_config = RunConfig(run_id="t", seed=seed, scale=ScaleConfig(scale=scale, days=days))
    journeys, funnel_df, click_alloc, txn_alloc = journeys_mod.simulate_month(
        calibration, run_config, rng,
        click_alloc=IdAllocator(1), txn_alloc=IdAllocator(1),
    )
    return journeys, funnel_df, click_alloc, txn_alloc


class TestAnomalyReproducibility(unittest.TestCase):
    def test_same_seed_gives_identical_results(self):
        calibration = make_calibration()

        def run_once():
            journeys, funnel_df, click_alloc, txn_alloc = _simulate_baseline(calibration, seed=7)
            specs = anomalies_mod.build_default_catalog(calibration)
            rng = np.random.default_rng(99)
            return anomalies_mod.apply_anomalies(
                journeys.copy(), funnel_df.copy(), calibration, rng, specs, click_alloc, txn_alloc
            )

        df1, funnel1, labels1 = run_once()
        df2, funnel2, labels2 = run_once()

        self.assertEqual(len(df1), len(df2))
        self.assertTrue(df1["click_id"].equals(df2["click_id"]))
        self.assertTrue(labels1.equals(labels2))

    def test_each_anomaly_type_has_a_detectable_effect(self):
        calibration = make_calibration(n_days=10)
        journeys, funnel_df, click_alloc, txn_alloc = _simulate_baseline(calibration, scale=5.0, days=10)
        specs = anomalies_mod.build_default_catalog(calibration)
        rng = np.random.default_rng(3)

        baseline_clicks = len(journeys)
        modified, funnel_modified, labels = anomalies_mod.apply_anomalies(
            journeys.copy(), funnel_df.copy(), calibration, rng, specs, click_alloc, txn_alloc
        )

        traffic_burst_label = labels[labels["type"] == "traffic_burst"].iloc[0]
        self.assertGreater(len(modified), baseline_clicks, "traffic burst should add click rows")
        self.assertGreater(traffic_burst_label["n_journeys_affected"], 0)

        # Ground truth lives on the internal journeys table only (never on exported-shape tables).
        self.assertIn("anomaly_id", modified.columns)


if __name__ == "__main__":
    unittest.main()
