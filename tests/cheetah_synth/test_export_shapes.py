import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from cheetah_synth import export, journeys as journeys_mod
from cheetah_synth.config import RunConfig, ScaleConfig
from cheetah_synth.identities import IdAllocator

from .fixtures import make_calibration

REAL_CLICK_COLUMNS = ["row", "Click ID", "Time", "Freecash ID", "Freecash AdSpace ID",
                      "Game Placement ID", "Country"]
REAL_TX_COLUMNS = ["Transaction ID", "Click Time", "Time", "Click to Conversion Duration",
                    "Freecash ID", "Freecash AdSpace ID", "Game Placement ID",
                    "Transaction Type", "Country", "Amount", "Commission", "Payout", "Margin"]
REAL_PERF_COLUMNS = ["Freecash ID", "Freecash AdSpace ID", "Game Placement ID", "Currency",
                      "Country", "Transaction Type", "Month", "Amount", "Commission", "Payout",
                      "Margin", "Transaction Count", "Vertical Name", "Category Name",
                      "Conversion Rate", "ECPC", "ECPM", "CTR", "Roas"]


def _simulate_small():
    calibration = make_calibration(n_days=3)
    rng = np.random.default_rng(5)
    run_config = RunConfig(run_id="t", scale=ScaleConfig(scale=5.0, days=3))
    journeys, funnel_df, _, _ = journeys_mod.simulate_month(
        calibration, run_config, rng, click_alloc=IdAllocator(1), txn_alloc=IdAllocator(1),
    )
    return calibration, journeys


class TestExportShapes(unittest.TestCase):
    def test_click_csv_columns_match_real_exactly(self):
        calibration, journeys = _simulate_small()
        with tempfile.TemporaryDirectory() as tmp:
            paths = export.to_click_csvs(journeys, Path(tmp))
            self.assertTrue(paths)
            out = pd.read_csv(paths[0])
            self.assertEqual(list(out.columns), REAL_CLICK_COLUMNS)

    def test_transaction_report_columns_match_real_exactly(self):
        calibration, journeys = _simulate_small()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "tx.xlsx"
            out = export.to_transaction_report(journeys, out_path)
            self.assertEqual(list(out.columns), REAL_TX_COLUMNS)
            reread = pd.ExcelFile(out_path).parse("Sheet")
            self.assertEqual(list(reread.columns), REAL_TX_COLUMNS)

    def test_performance_report_columns_match_real_exactly(self):
        calibration, journeys = _simulate_small()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "perf.xlsx"
            out = export.to_performance_report(journeys, out_path, calibration.month_start)
            self.assertEqual(list(out.columns), REAL_PERF_COLUMNS)
            self.assertTrue(out["Conversion Rate"].isna().all())  # matches real export (always NaN)

    def test_no_anomaly_columns_leak_into_exports(self):
        calibration, journeys = _simulate_small()
        journeys["anomaly_id"] = "A-001-test"  # pretend everything is anomaly-tagged
        with tempfile.TemporaryDirectory() as tmp:
            tx = export.to_transaction_report(journeys, Path(tmp) / "tx.xlsx")
            perf = export.to_performance_report(journeys, Path(tmp) / "perf.xlsx", calibration.month_start)
        self.assertNotIn("anomaly_id", tx.columns)
        self.assertNotIn("anomaly_id", perf.columns)


if __name__ == "__main__":
    unittest.main()
