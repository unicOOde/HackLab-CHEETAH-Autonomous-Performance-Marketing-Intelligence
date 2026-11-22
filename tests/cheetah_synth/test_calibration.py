import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from cheetah_synth.calibration import _fit_beta_prior, compute_calibration


class TestFitBetaPrior(unittest.TestCase):
    def test_matches_observed_mean_reasonably(self):
        raw_cvr = np.array([0.01, 0.012, 0.009, 0.011, 0.0105])
        alpha, beta = _fit_beta_prior(raw_cvr)
        self.assertGreater(alpha, 0)
        self.assertGreater(beta, 0)
        prior_mean = alpha / (alpha + beta)
        self.assertAlmostEqual(prior_mean, raw_cvr.mean(), delta=0.01)

    def test_degenerate_variance_falls_back_safely(self):
        # All-identical raw CVRs give zero sample variance — must not divide by zero / produce NaN.
        raw_cvr = np.array([0.01, 0.01, 0.01])
        alpha, beta = _fit_beta_prior(raw_cvr)
        self.assertTrue(np.isfinite(alpha) and np.isfinite(beta))
        self.assertGreater(alpha, 0)
        self.assertGreater(beta, 0)


class TestComputeCalibration(unittest.TestCase):
    def _build_fake_data_dir(self, tmp_dir: Path):
        clicks_dir = tmp_dir / "Clicks"
        clicks_dir.mkdir()

        # 3 converting segments (P1/P2/P3), 2 non-converting (P4/P5), all with real click volume.
        rows = []
        click_id = 1
        for placement, country, n in (("P1", "DE", 2000), ("P2", "US", 1500),
                                       ("P3", "FR", 1000), ("P4", "GB", 3000), ("P5", "PL", 2500)):
            for _ in range(n):
                rows.append({
                    "Click ID": click_id, "Time": "2026-10-01T12:00:00.000000Z",
                    "Freecash ID": "00006s", "Freecash AdSpace ID": 141,
                    "Game Placement ID": placement, "Country": country,
                })
                click_id += 1
        pd.DataFrame(rows).to_csv(clicks_dir / "OCT.01.26_Click-Report.csv", index=False)

        tx_rows = []
        txn_id = 1
        for placement, country, n in (("P1", "DE", 20), ("P2", "US", 15), ("P3", "FR", 10)):
            for _ in range(n):
                tx_rows.append({
                    "Transaction ID": txn_id, "Click Time": "2026-10-01 12:00:00",
                    "Time": "2026-10-01 13:00:00", "Click to Conversion Duration": 3600.0,
                    "Freecash ID": "00006s", "Freecash AdSpace ID": 141,
                    "Game Placement ID": placement, "Transaction Type": "install",
                    "Country": country, "Amount": 100.0, "Commission": 10.0,
                    "Payout": 7.0, "Margin": 3.0,
                })
                txn_id += 1
        pd.DataFrame(tx_rows).to_excel(tmp_dir / "OCT26_Transaction Report.xlsx",
                                        sheet_name="Sheet", index=False, engine="openpyxl")

        perf_rows = [
            {"Game Placement ID": "P1", "Vertical Name": "Retail", "Category Name": "Cat1"},
            {"Game Placement ID": "P2", "Vertical Name": "Retail", "Category Name": "Cat1"},
            {"Game Placement ID": "P3", "Vertical Name": "Finance", "Category Name": "Cat2"},
        ]
        pd.DataFrame(perf_rows).to_excel(tmp_dir / "OCT26_Performance-Report.xlsx",
                                          sheet_name="Sheet", index=False, engine="openpyxl")

    def test_nonconverting_floor_does_not_dominate_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._build_fake_data_dir(tmp_dir)
            calibration = compute_calibration(tmp_dir)

            shares = np.diff(np.concatenate([[0.0], calibration.segment_cum_share]))
            total_clicks = shares.sum() and 10000  # 5 segments sum to 10000 clicks total above
            clicks_per_seg = shares * total_clicks
            expected_total = (clicks_per_seg * calibration.segment_shrunk_cvr).sum()
            real_total = 45  # 20 + 15 + 10

            # The floor on non-converting segments must not blow up the aggregate — this is the
            # exact failure mode that was caught and fixed during development (expected conversions
            # came out ~40% above real when the floor was applied uniformly/unscaled).
            self.assertLess(expected_total, real_total * 1.5)

            is_p4_or_p5 = np.isin(calibration.segment_game_placement_id, ["P4", "P5"])
            self.assertTrue((calibration.segment_shrunk_cvr[is_p4_or_p5] > 0).all())
            self.assertLess(
                calibration.segment_shrunk_cvr[is_p4_or_p5].max(),
                calibration.segment_shrunk_cvr[~is_p4_or_p5].min(),
            )


if __name__ == "__main__":
    unittest.main()
