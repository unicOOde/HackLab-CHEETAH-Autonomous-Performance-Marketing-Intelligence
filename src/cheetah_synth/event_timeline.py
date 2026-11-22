"""Builds a single chronologically-sorted click+install event timeline for live backend ingestion.

Unlike `backend_adapter.build_source_events()` (which deliberately downsamples to a few thousand
rows for the bulk-preload demo path — 2 events per converting journey plus an optional small
random click sample), this keeps every real click, including non-converting and censored-
conversion journeys (both of which `build_source_events` silently drops entirely), plus one
install row per non-censored conversion carrying full `amount`/`commission`/`payout`/`margin`
(today's bulk-preload events only carry `amount`/`commission`). Output is sorted by
`event_time_utc` so a worker can stream it forward in true chronological order without re-sorting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EVENT_TIMELINE_COLUMNS = [
    "event_time_utc", "kind", "source_id", "game_placement_id", "country",
    "amount", "commission", "payout", "margin", "vertical", "category", "surface", "event_day",
]


def _meta_cols(placement_meta, placement_ids):
    """vertical/category are placement-level attributes (knowable for any click regardless of
    whether it converts, via calibration.placement_meta) — not reused from journeys' own
    vertical/category columns, which are only filled in for converting rows."""
    n = len(placement_ids)
    if placement_meta is None:
        return np.array([None] * n, dtype=object), np.array([None] * n, dtype=object)
    meta = placement_meta.reindex(np.asarray(placement_ids))
    return meta["vertical"].to_numpy(), meta["category"].to_numpy()


def _surface(df):
    """The page the click came from (search, for_you, ...; see surfaces.py). Installs carry the
    surface of their originating click. None for journeys generated before surfaces existed."""
    if "surface" in df.columns:
        return df["surface"].to_numpy()
    return np.array([None] * len(df), dtype=object)


def build_event_timeline(journeys: pd.DataFrame, placement_meta: pd.DataFrame = None) -> pd.DataFrame:
    n = len(journeys)
    click_vertical, click_category = _meta_cols(placement_meta, journeys["game_placement_id"].to_numpy())
    clicks = pd.DataFrame({
        "event_time_utc": journeys["time"].to_numpy(),
        "kind": "click",
        "source_id": journeys["click_id"].astype(str).to_numpy(),
        "game_placement_id": journeys["game_placement_id"].to_numpy(),
        "country": journeys["country"].to_numpy(),
        "amount": np.full(n, np.nan),
        "commission": np.full(n, np.nan),
        "payout": np.full(n, np.nan),
        "margin": np.full(n, np.nan),
        "vertical": click_vertical,
        "category": click_category,
        "surface": _surface(journeys),
    })

    converted = journeys[journeys["converts"] & ~journeys["censored"]]
    m = len(converted)
    install_vertical, install_category = _meta_cols(placement_meta, converted["game_placement_id"].to_numpy())
    installs = pd.DataFrame({
        "event_time_utc": converted["conversion_time"].to_numpy(),
        "kind": "install",
        "source_id": converted["transaction_id"].astype("int64").astype(str).to_numpy(),
        "game_placement_id": converted["game_placement_id"].to_numpy(),
        "country": converted["country"].to_numpy(),
        "amount": converted["amount"].to_numpy(),
        "commission": converted["commission"].to_numpy(),
        "payout": converted["payout"].to_numpy(),
        "margin": converted["margin"].to_numpy(),
        "vertical": install_vertical,
        "category": install_category,
        "surface": _surface(converted),
    }) if m else pd.DataFrame(columns=clicks.columns)

    timeline = pd.concat([clicks, installs], ignore_index=True)
    timeline["event_time_utc"] = pd.to_datetime(timeline["event_time_utc"], utc=True)
    timeline["event_day"] = timeline["event_time_utc"].dt.strftime("%Y-%m-%d")
    timeline = timeline.sort_values(
        ["event_time_utc", "kind", "source_id"], kind="mergesort"
    ).reset_index(drop=True)
    return timeline[EVENT_TIMELINE_COLUMNS]
