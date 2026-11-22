import unittest

import numpy as np

from cheetah_synth import event_timeline, journeys as journeys_mod, surfaces
from cheetah_synth.config import RunConfig, ScaleConfig
from cheetah_synth.identities import IdAllocator

from .fixtures import make_calibration


class TestSurfaces(unittest.TestCase):
    def test_cvr_multiplier_preserves_expected_cvr(self):
        shares = np.array([v[0] for v in surfaces.SURFACE_MIX.values()])
        shares = shares / shares.sum()
        self.assertAlmostEqual(float((shares * surfaces.CVR_MULTIPLIER).sum()), 1.0, places=9)

    def test_every_click_and_install_carries_a_surface(self):
        calibration = make_calibration(n_days=3)
        run_config = RunConfig(run_id="t", scale=ScaleConfig(scale=2.0, days=3))
        journeys, *_ = journeys_mod.simulate_month(
            calibration, run_config, np.random.default_rng(1), IdAllocator(1), IdAllocator(1)
        )
        self.assertTrue(journeys["surface"].isin(surfaces.SURFACES).all())
        timeline = event_timeline.build_event_timeline(journeys, calibration.placement_meta)
        self.assertTrue(timeline["surface"].isin(surfaces.SURFACES).all())
        self.assertEqual(set(timeline["kind"]), {"click", "install"})
        # Installs keep their originating click's surface.
        converted = journeys[journeys["converts"] & ~journeys["censored"]]
        installs = timeline[timeline["kind"] == "install"].set_index("source_id")["surface"]
        expected = converted.set_index(converted["transaction_id"].astype("int64").astype(str))["surface"]
        self.assertTrue(installs.sort_index().equals(expected.sort_index().rename("surface")))


if __name__ == "__main__":
    unittest.main()
