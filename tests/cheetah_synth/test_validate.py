import unittest

import numpy as np
import pandas as pd

from cheetah_synth.config import ToleranceConfig
from cheetah_synth.validate import compare_to_real

from .fixtures import make_calibration


def _make_frames(cvr=0.01, n_clicks=10_000, seed=0):
    rng = np.random.default_rng(seed)
    clicks = pd.DataFrame({
        "Game Placement ID": ["P1"] * n_clicks,
        "Country": ["DE"] * n_clicks,
        "Time": pd.date_range("2026-10-01", periods=n_clicks, freq="s", tz="UTC"),
    })
    n_conv = int(n_clicks * cvr)
    tx = pd.DataFrame({
        "Transaction ID": np.arange(n_conv),
        "Game Placement ID": ["P1"] * n_conv,
        "Country": ["DE"] * n_conv,
        "Amount": rng.uniform(10, 100, n_conv),
        "Commission": rng.uniform(1, 10, n_conv),
        "Payout": rng.uniform(0.5, 5, n_conv),
        "Margin": rng.uniform(0.2, 3, n_conv),
    })
    return clicks, tx


class TestCompareToReal(unittest.TestCase):
    def test_passes_when_synthetic_matches_real_closely(self):
        calibration = make_calibration()
        real_clicks, real_tx = _make_frames(cvr=0.01, seed=1)
        synth_clicks, synth_tx = _make_frames(cvr=0.0101, seed=2)  # same shape, tiny noise

        synthetic_journeys = pd.DataFrame({
            "game_placement_id": synth_clicks["Game Placement ID"],
            "country": synth_clicks["Country"],
            "time": synth_clicks["Time"],
            "converts": [True] * len(synth_tx) + [False] * (len(synth_clicks) - len(synth_tx)),
            "transaction_id": list(range(len(synth_tx))) + [np.nan] * (len(synth_clicks) - len(synth_tx)),
            "censored": False,
            "anomaly_id": None,
        })
        calibration.daily_counts = real_clicks["Time"].dt.date.value_counts().sort_index()

        report = compare_to_real(
            real_clicks, real_tx, pd.DataFrame(), synthetic_journeys, synth_tx,
            pd.DataFrame({"Transaction Count": [len(synth_tx)]}), pd.DataFrame(),
            calibration, ToleranceConfig(),
        )
        overall_cvr_check = next(c for c in report["checks"] if c["name"] == "overall_cvr")
        self.assertTrue(overall_cvr_check["passed"])

    def test_fails_when_synthetic_cvr_way_off(self):
        calibration = make_calibration()
        real_clicks, real_tx = _make_frames(cvr=0.01, seed=1)
        synth_clicks, synth_tx = _make_frames(cvr=0.05, seed=2)  # 5x too high

        synthetic_journeys = pd.DataFrame({
            "game_placement_id": synth_clicks["Game Placement ID"],
            "country": synth_clicks["Country"],
            "time": synth_clicks["Time"],
            "converts": [True] * len(synth_tx) + [False] * (len(synth_clicks) - len(synth_tx)),
            "transaction_id": list(range(len(synth_tx))) + [np.nan] * (len(synth_clicks) - len(synth_tx)),
            "censored": False,
            "anomaly_id": None,
        })
        calibration.daily_counts = real_clicks["Time"].dt.date.value_counts().sort_index()

        report = compare_to_real(
            real_clicks, real_tx, pd.DataFrame(), synthetic_journeys, synth_tx,
            pd.DataFrame({"Transaction Count": [len(synth_tx)]}), pd.DataFrame(),
            calibration, ToleranceConfig(),
        )
        self.assertFalse(report["passed"])
        overall_cvr_check = next(c for c in report["checks"] if c["name"] == "overall_cvr")
        self.assertFalse(overall_cvr_check["passed"])


if __name__ == "__main__":
    unittest.main()
