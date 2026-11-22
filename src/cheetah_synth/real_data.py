"""Loaders for the three real Oct 2026 data sources, used only for calibration."""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

CLICK_COLUMNS = [
    "Click ID",
    "Time",
    "Freecash ID",
    "Freecash AdSpace ID",
    "Game Placement ID",
    "Country",
]


def load_clicks(data_dir: Path) -> pd.DataFrame:
    files = sorted(glob.glob(str(Path(data_dir) / "Clicks" / "*.csv")))
    if not files:
        raise FileNotFoundError(f"No click CSVs found under {Path(data_dir) / 'Clicks'}")
    frames = [pd.read_csv(f, usecols=CLICK_COLUMNS) for f in files]
    clicks = pd.concat(frames, ignore_index=True)
    clicks["Time"] = pd.to_datetime(clicks["Time"], format="ISO8601", utc=True)
    return clicks


def load_transactions(data_dir: Path) -> pd.DataFrame:
    path = Path(data_dir) / "OCT26_Transaction Report.xlsx"
    tx = pd.ExcelFile(path).parse("Sheet")
    tx["Click Time"] = pd.to_datetime(tx["Click Time"], utc=True)
    tx["Time"] = pd.to_datetime(tx["Time"], utc=True)
    return tx


def load_performance(data_dir: Path) -> pd.DataFrame:
    path = Path(data_dir) / "OCT26_Performance-Report.xlsx"
    return pd.ExcelFile(path).parse("Sheet")
