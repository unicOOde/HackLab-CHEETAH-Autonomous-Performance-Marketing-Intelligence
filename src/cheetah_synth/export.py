"""Exporters producing drop-in-shape-compatible copies of the three real report files, for
pipeline/ETL testing. Column names/order/dtypes match the real files exactly; no synthetic-only
columns (e.g. anomaly_id) ever appear in these outputs — anomaly ground truth stays in
labels/anomalies.csv only.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FREECASH_ID = "00006s"
FREECASH_ADSPACE_ID = 141
CURRENCY = "EUR"


def _format_iso(ts: pd.Series) -> pd.Series:
    # Matches the real Click CSVs' "...T23:59:58.129000Z" format exactly.
    return ts.dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def to_click_csvs(journeys: pd.DataFrame, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for day, day_df in journeys.groupby(journeys["time"].dt.date):
        day_df = day_df.sort_values("time", ascending=False).reset_index(drop=True)
        out = pd.DataFrame({
            "row": np.arange(len(day_df)),
            "Click ID": day_df["click_id"],
            "Time": _format_iso(day_df["time"]),
            "Freecash ID": FREECASH_ID,
            "Freecash AdSpace ID": FREECASH_ADSPACE_ID,
            "Game Placement ID": day_df["game_placement_id"],
            "Country": day_df["country"],
        })
        path = out_dir / f"OCT.{day.day:02d}.26_Click-Report.csv"
        out.to_csv(path, index=False)
        written.append(path)
    return written


def to_transaction_report(journeys: pd.DataFrame, out_path: Path) -> pd.DataFrame:
    converted = journeys[journeys["converts"] & ~journeys["censored"]].sort_values("conversion_time")
    out = pd.DataFrame({
        "Transaction ID": converted["transaction_id"].astype(np.int64),
        "Click Time": converted["time"].dt.tz_localize(None),
        "Time": converted["conversion_time"].dt.tz_localize(None),
        "Click to Conversion Duration": converted["duration_seconds"],
        "Freecash ID": FREECASH_ID,
        "Freecash AdSpace ID": FREECASH_ADSPACE_ID,
        "Game Placement ID": converted["game_placement_id"],
        "Transaction Type": "install",
        "Country": converted["country"],
        "Amount": converted["amount"],
        "Commission": converted["commission"],
        "Payout": converted["payout"],
        "Margin": converted["margin"],
    })
    out.to_excel(out_path, sheet_name="Sheet", index=False, engine="openpyxl")
    return out


def to_performance_report(journeys: pd.DataFrame, out_path: Path,
                           month_start: pd.Timestamp,
                           populate_derived_metrics: bool = False) -> pd.DataFrame:
    converted = journeys[journeys["converts"] & ~journeys["censored"]]
    grouped = converted.groupby(["game_placement_id", "country"], dropna=False).agg(
        Amount=("amount", "sum"),
        Commission=("commission", "sum"),
        Payout=("payout", "sum"),
        Margin=("margin", "sum"),
        **{"Transaction Count": ("transaction_id", "count")},
        vertical=("vertical", "first"),
        category=("category", "first"),
    ).reset_index()

    out = pd.DataFrame({
        "Freecash ID": FREECASH_ID,
        "Freecash AdSpace ID": FREECASH_ADSPACE_ID,
        "Game Placement ID": grouped["game_placement_id"],
        "Currency": CURRENCY,
        "Country": grouped["country"],
        "Transaction Type": "install",
        "Month": month_start.tz_localize(None) if month_start.tzinfo else month_start,
        "Amount": grouped["Amount"],
        "Commission": grouped["Commission"],
        "Payout": grouped["Payout"],
        "Margin": grouped["Margin"],
        "Transaction Count": grouped["Transaction Count"],
        "Vertical Name": grouped["vertical"],
        "Category Name": grouped["category"],
        "Conversion Rate": np.nan,
        "ECPC": np.nan,
        "ECPM": np.nan,
        "CTR": np.nan,
        "Roas": np.nan,
    })

    if populate_derived_metrics:
        # CTR and ECPM need impression counts, which aren't modeled here (clicks-only data) —
        # left as NaN even in this mode. Only click-and-conversion-derived metrics are filled in.
        clicks_per_segment = (
            journeys.groupby(["game_placement_id", "country"], dropna=False).size()
        )
        key = list(zip(out["Game Placement ID"], out["Country"]))
        clicks = pd.Series(key).map(clicks_per_segment.to_dict()).to_numpy()
        out["Conversion Rate"] = out["Transaction Count"] / clicks
        out["ECPC"] = out["Commission"] / clicks
        out["Roas"] = out["Amount"] / out["Commission"].replace(0, np.nan)

    out.to_excel(out_path, sheet_name="Sheet", index=False, engine="openpyxl")
    return out
