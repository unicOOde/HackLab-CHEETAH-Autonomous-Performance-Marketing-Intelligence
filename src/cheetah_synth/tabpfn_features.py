"""Daily segment-level tabular rollup with lag/calendar/static features, shaped for a TabPFN-style
tabular forecaster (TabPFN itself is a few-shot tabular predictor — time-series forecasting with it
means converting the series into a tabular regression problem with lag + calendar features, which
is exactly what this module produces). This module does not depend on or run `tabpfn` itself.
"""
from __future__ import annotations

import pandas as pd

from .calibration import Calibration

LAGS = (1, 7)
ROLLING_WINDOW = 7


def build_daily_segment_table(journeys: pd.DataFrame, calibration: Calibration) -> pd.DataFrame:
    df = journeys.copy()
    df["date"] = df["time"].dt.date

    daily = df.groupby(["game_placement_id", "country", "date"], dropna=False).agg(
        clicks=("click_id", "count"),
        conversions=("converts", "sum"),
    ).reset_index()

    converted = df[df["converts"] & ~df["censored"]]
    revenue = converted.groupby(["game_placement_id", "country", "date"], dropna=False).agg(
        payout=("payout", "sum"),
        revenue=("amount", "sum"),
    ).reset_index()
    daily = daily.merge(revenue, on=["game_placement_id", "country", "date"], how="left")
    daily[["payout", "revenue"]] = daily[["payout", "revenue"]].fillna(0.0)

    # Reindex every segment to the full date range so gaps become explicit zero-rows — required
    # for lag features to mean what they say (lag1 == "yesterday", not "last day this segment
    # happened to have traffic").
    all_dates = pd.date_range(calibration.month_start, calibration.month_end, freq="D").date
    segments = daily[["game_placement_id", "country"]].drop_duplicates()
    full_index = segments.merge(pd.DataFrame({"date": all_dates}), how="cross")
    daily = full_index.merge(daily, on=["game_placement_id", "country", "date"], how="left")
    daily[["clicks", "conversions", "payout", "revenue"]] = (
        daily[["clicks", "conversions", "payout", "revenue"]].fillna(0.0)
    )

    daily = daily.sort_values(["game_placement_id", "country", "date"]).reset_index(drop=True)
    grp = daily.groupby(["game_placement_id", "country"], dropna=False)
    for lag in LAGS:
        for col in ("clicks", "conversions", "payout", "revenue"):
            daily[f"{col}_lag{lag}"] = grp[col].shift(lag)
    for col in ("clicks", "conversions", "payout", "revenue"):
        # shift(1) before rolling so the window never includes the current day (no leakage).
        daily[f"{col}_rolling_mean_{ROLLING_WINDOW}"] = (
            grp[col].transform(lambda s: s.shift(1).rolling(ROLLING_WINDOW, min_periods=1).mean())
        )

    dates = pd.to_datetime(daily["date"])
    daily["day_of_week"] = dates.dt.dayofweek
    daily["is_weekend"] = daily["day_of_week"].isin([5, 6])
    daily["day_of_month"] = dates.dt.day

    meta = calibration.placement_meta.reindex(daily["game_placement_id"])
    daily["vertical"] = meta["vertical"].fillna("Other (Unmapped)").to_numpy()
    daily["category"] = meta["category"].fillna("Other (Unmapped)").to_numpy()

    return daily
