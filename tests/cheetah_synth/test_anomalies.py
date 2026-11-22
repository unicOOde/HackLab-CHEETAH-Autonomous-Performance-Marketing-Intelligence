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
        specs = anomalies_mod.build_segment_catalog(calibration)
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


def _daily_amounts(journeys):
    live = journeys[journeys["converts"] & ~journeys["censored"]]
    return live.groupby(live["conversion_time"].dt.date)["amount"].sum()


class TestFlatProfileAndRevenueAnomalies(unittest.TestCase):
    N_DAYS = 31

    def _flat_baseline(self, calibration):
        journeys, funnel_df, click_alloc, txn_alloc = _simulate_baseline(
            calibration, seed=11, scale=5.0, days=self.N_DAYS
        )
        rng = np.random.default_rng(5)
        journeys_mod.flatten_daily_revenue(journeys, calibration, rng, noise=0.03)
        return journeys, funnel_df, click_alloc, txn_alloc

    def test_flatten_makes_daily_revenue_flat(self):
        calibration = make_calibration(n_days=self.N_DAYS)
        journeys, *_ = _simulate_baseline(calibration, seed=11, scale=5.0, days=self.N_DAYS)
        before = _daily_amounts(journeys)
        journeys_mod.flatten_daily_revenue(journeys, calibration, np.random.default_rng(5), noise=0.03)
        after = _daily_amounts(journeys)
        cv_before = before.std() / before.mean()
        cv_after = after.std() / after.mean()
        self.assertLess(cv_after, 0.06)
        self.assertLess(cv_after, cv_before)

    def test_default_catalog_is_three_below_one_above(self):
        specs = anomalies_mod.build_default_catalog(make_calibration(n_days=self.N_DAYS))
        self.assertEqual(len(specs), 4)
        self.assertEqual(sum(s.severity < 0 for s in specs), 3)
        self.assertEqual(sum(s.severity > 0 for s in specs), 1)
        self.assertEqual(len({s.date_start for s in specs}), 4, "each anomaly gets its own day")

    def test_each_revenue_anomaly_moves_its_day_in_the_right_direction(self):
        calibration = make_calibration(n_days=self.N_DAYS)
        journeys, funnel_df, click_alloc, txn_alloc = self._flat_baseline(calibration)
        before = _daily_amounts(journeys)
        specs = anomalies_mod.build_default_catalog(calibration)
        modified, funnel_modified, labels = anomalies_mod.apply_anomalies(
            journeys.copy(), funnel_df.copy(), calibration, np.random.default_rng(3),
            specs, click_alloc, txn_alloc,
        )
        after = _daily_amounts(modified)
        for spec in specs:
            change = after[spec.date_start] / before[spec.date_start] - 1.0
            # Within a few points of the planned effect, and clearly outside a ±15% band.
            self.assertAlmostEqual(change, spec.severity, delta=0.08, msg=spec.anomaly_id)
            self.assertGreater(abs(change), 0.25, msg=spec.anomaly_id)
        untouched = [d for d in before.index if d not in {s.date_start for s in specs}]
        np.testing.assert_allclose(after[untouched].to_numpy(), before[untouched].to_numpy())
        self.assertTrue((labels["n_journeys_affected"] > 0).all())
        # Funnel stays consistent with the converting set (new promo conversions get events,
        # dropped ones lose theirs).
        converting = set(modified.loc[modified["converts"], "click_id"])
        self.assertEqual(set(funnel_modified["journey_id"]), converting)

    def test_naive_reference_band_flags_exactly_the_anomaly_days(self):
        from cheetah_synth import backend_adapter

        calibration = make_calibration(n_days=self.N_DAYS)
        journeys, funnel_df, click_alloc, txn_alloc = self._flat_baseline(calibration)
        specs = anomalies_mod.build_default_catalog(calibration)
        modified, _, _ = anomalies_mod.apply_anomalies(
            journeys, funnel_df, calibration, np.random.default_rng(3), specs, click_alloc, txn_alloc
        )
        daily = backend_adapter.build_daily_amounts(modified)
        refs = backend_adapter.build_naive_references(daily, "t")
        amounts = {r["day"]: r["amount"] for r in daily}
        flagged = {
            r["reference_day"]: ("below" if amounts[r["reference_day"]] < r["lower"] else "above")
            for r in refs
            if not (r["lower"] <= amounts[r["reference_day"]] <= r["upper"])
        }
        expected = {s.date_start.isoformat(): s.params["direction"] for s in specs}
        self.assertEqual(flagged, expected)


if __name__ == "__main__":
    unittest.main()
