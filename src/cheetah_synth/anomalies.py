"""Labeled anomaly injection: traffic bursts, CVR shocks, payout shocks, latency blowouts.

Every anomaly is applied on top of an already-simulated, calibration-faithful baseline
(journeys.simulate_month), via the same seeded generator, so toggling anomalies on/off never
changes the baseline's own random draws. Ground truth (which segment, window, type, severity) is
returned as a separate labels table — it is never merged into the exported click/transaction/
performance tables, only attached internally via the journeys_df `anomaly_id` column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from . import journeys as journeys_mod
from .calibration import Calibration
from .identities import IdAllocator

ANOMALY_TYPES = ("traffic_burst", "cvr_shock", "payout_shock", "latency_blowout")


@dataclass
class AnomalySpec:
    anomaly_id: str
    type: str
    game_placement_id: str
    country: Any
    date_start: date
    date_end: date
    severity: float
    params: Dict[str, Any] = field(default_factory=dict)
    description: str = ""


def _top_click_segments(calibration: Calibration, k: int):
    """Top-k segments by raw click volume — the right target for a bot-flood/traffic-burst
    anomaly, regardless of whether the segment actually converts."""
    shares = np.diff(np.concatenate([[0.0], calibration.segment_cum_share]))
    order = np.argsort(-shares)[:k]
    return [
        (calibration.segment_game_placement_id[i], calibration.segment_country[i])
        for i in order
    ]


def _top_converting_segments(calibration: Calibration, k: int, exclude: set = frozenset()):
    """Top-k segments by *expected conversion volume* (clicks * shrunk CVR) — the right target
    for CVR/payout/latency anomalies, which need an existing converting baseline to shock. Picking
    by raw click share instead (as traffic_burst does) would mostly hit high-click, near-zero-CVR
    placements with nothing to shock at dev scale."""
    shares = np.diff(np.concatenate([[0.0], calibration.segment_cum_share]))
    total_clicks_proxy = shares  # relative; absolute scale cancels out in the ranking
    expected_conversions = total_clicks_proxy * calibration.segment_shrunk_cvr
    order = np.argsort(-expected_conversions)
    picked = []
    for i in order:
        key = (calibration.segment_game_placement_id[i], calibration.segment_country[i])
        if key in exclude or key in picked:
            continue
        picked.append(key)
        if len(picked) == k:
            break
    return picked


def build_default_catalog(calibration: Calibration) -> List[AnomalySpec]:
    """A small, hand-authored, inspectable set of anomalies: one per confirmed type. The
    traffic-burst target is picked by raw click volume (bot floods target high-traffic segments
    regardless of CVR); the CVR/payout/latency targets are picked by expected conversion volume
    (they need an existing converting baseline to shock). Each gets its own 3-day window in
    Oct 2026 to keep them separable."""
    burst_segment = _top_click_segments(calibration, 1)[0]
    converting_segments = _top_converting_segments(calibration, 3, exclude={burst_segment})
    segments = [burst_segment, *converting_segments]
    windows = {
        "traffic_burst": (5, 7),
        "cvr_shock": (12, 14),
        "payout_shock": (19, 21),
        "latency_blowout": (25, 27),
    }
    severities = {
        "traffic_burst": 8.0,
        "cvr_shock": 6.0,
        "payout_shock": 3.0,
        "latency_blowout": 10.0,
    }
    month_start = calibration.month_start.date()
    specs = []
    for i, anomaly_type in enumerate(ANOMALY_TYPES):
        placement, country = segments[i]
        start_offset, end_offset = windows[anomaly_type]
        spec = AnomalySpec(
            anomaly_id=f"A-{i + 1:03d}-{anomaly_type}",
            type=anomaly_type,
            game_placement_id=placement,
            country=country,
            date_start=month_start + timedelta(days=start_offset),
            date_end=month_start + timedelta(days=end_offset),
            severity=severities[anomaly_type],
            params={"regular_timing": True} if anomaly_type == "traffic_burst" else {},
            description=f"{anomaly_type} on placement={placement} country={country}",
        )
        specs.append(spec)
    return specs


def _segment_mask(df: pd.DataFrame, spec: AnomalySpec) -> np.ndarray:
    dates = df["time"].dt.date
    return (
        (df["game_placement_id"] == spec.game_placement_id)
        & (df["country"] == spec.country)
        & (dates >= spec.date_start)
        & (dates <= spec.date_end)
    ).to_numpy()


def _lookup_segment_cvr(calibration: Calibration, placement: str, country: Any) -> float:
    match = (calibration.segment_game_placement_id == placement) & (
        calibration.segment_country == country
    )
    if match.any():
        return float(calibration.segment_shrunk_cvr[match][0])
    return float(np.mean(calibration.segment_shrunk_cvr))


def _apply_traffic_burst(df, spec, calibration, rng, click_alloc) -> pd.DataFrame:
    new_rows = []
    day = spec.date_start
    while day <= spec.date_end:
        existing_n = int(
            (
                (df["game_placement_id"] == spec.game_placement_id)
                & (df["country"] == spec.country)
                & (df["time"].dt.date == day)
            ).sum()
        )
        baseline = max(existing_n, 5)
        extra_n = int(round(baseline * (spec.severity - 1)))
        if extra_n > 0:
            if spec.params.get("regular_timing"):
                step_us = int(24 * 3600 * 1_000_000 / extra_n)
                offsets = np.arange(extra_n, dtype=np.int64) * step_us
                base = pd.Timestamp(day, tz="UTC")
                times = base + pd.to_timedelta(offsets, unit="us")
            else:
                times = journeys_mod.sample_timestamps_in_day(calibration, rng, pd.Timestamp(day), extra_n)
            click_ids = click_alloc.next_batch(extra_n)
            row = pd.DataFrame({
                "click_id": click_ids,
                "time": times,
                "game_placement_id": spec.game_placement_id,
                "country": spec.country,
                "converts": False,
            })
            for col, values in journeys_mod._empty_conversion_columns(extra_n).items():
                row[col] = values
            row["anomaly_id"] = spec.anomaly_id
            new_rows.append(row)
        day += timedelta(days=1)
    if not new_rows:
        return df
    return pd.concat([df, *new_rows], ignore_index=True)


def _apply_cvr_shock(df, spec, calibration, rng, txn_alloc) -> None:
    mask = _segment_mask(df, spec)
    if not mask.any():
        return
    base_cvr = _lookup_segment_cvr(calibration, spec.game_placement_id, spec.country)
    new_cvr = float(np.clip(base_cvr * spec.severity, 0.0, 1.0))
    target_converts = rng.random(int(mask.sum())) < new_cvr

    currently_converting = df["converts"].to_numpy()
    mask_idx = np.flatnonzero(mask)
    to_convert = np.zeros(len(df), dtype=bool)
    to_unconvert = np.zeros(len(df), dtype=bool)
    to_convert[mask_idx] = target_converts & ~currently_converting[mask_idx]
    to_unconvert[mask_idx] = ~target_converts & currently_converting[mask_idx]

    journeys_mod.materialize_conversions(df, to_convert, calibration, rng, txn_alloc)
    journeys_mod.clear_conversion(df, to_unconvert)

    unlabeled = mask & df["anomaly_id"].isna().to_numpy()
    df.loc[unlabeled, "anomaly_id"] = spec.anomaly_id


def _apply_payout_shock(df, spec) -> None:
    mask = _segment_mask(df, spec) & df["converts"].to_numpy()
    if not mask.any():
        return
    for col in ("amount", "commission", "payout", "margin"):
        df.loc[mask, col] = np.clip(df.loc[mask, col] * spec.severity, 0.0, None)
    unlabeled = mask & df["anomaly_id"].isna().to_numpy()
    df.loc[unlabeled, "anomaly_id"] = spec.anomaly_id


def _apply_latency_blowout(df, funnel_df, spec, calibration) -> pd.DataFrame:
    mask = _segment_mask(df, spec) & df["converts"].to_numpy()
    if not mask.any():
        return funnel_df
    old_duration = df.loc[mask, "duration_seconds"]
    new_duration = old_duration * spec.severity
    new_conversion_time = df.loc[mask, "time"] + pd.to_timedelta(new_duration, unit="s")
    censored = (new_conversion_time > calibration.month_end).to_numpy()

    affected_click_ids = df.loc[mask, "click_id"]
    click_time_lookup = df.loc[mask, ["click_id", "time"]].set_index("click_id")["time"]

    funnel_mask = funnel_df["journey_id"].isin(affected_click_ids) & (funnel_df["stage"] != "click")
    if funnel_mask.any():
        ct = funnel_df.loc[funnel_mask, "journey_id"].map(click_time_lookup)
        old_stage_time = funnel_df.loc[funnel_mask, "stage_time"]
        funnel_df.loc[funnel_mask, "stage_time"] = ct + (old_stage_time - ct) * spec.severity

    df.loc[mask, "duration_seconds"] = new_duration
    df.loc[mask, "conversion_time"] = new_conversion_time
    df.loc[mask, "censored"] = censored
    unlabeled = mask & df["anomaly_id"].isna().to_numpy()
    df.loc[unlabeled, "anomaly_id"] = spec.anomaly_id
    return funnel_df


def apply_anomalies(df: pd.DataFrame, funnel_df: pd.DataFrame, calibration: Calibration,
                     rng: np.random.Generator, specs: List[AnomalySpec],
                     click_alloc: IdAllocator, txn_alloc: IdAllocator):
    labels = []
    for spec in specs:
        if spec.type == "traffic_burst":
            before = len(df)
            df = _apply_traffic_burst(df, spec, calibration, rng, click_alloc)
            n_affected = len(df) - before
        elif spec.type == "cvr_shock":
            mask_before = _segment_mask(df, spec)
            n_affected = int(mask_before.sum())
            _apply_cvr_shock(df, spec, calibration, rng, txn_alloc)
        elif spec.type == "payout_shock":
            mask = _segment_mask(df, spec) & df["converts"].to_numpy()
            n_affected = int(mask.sum())
            _apply_payout_shock(df, spec)
        elif spec.type == "latency_blowout":
            mask = _segment_mask(df, spec) & df["converts"].to_numpy()
            n_affected = int(mask.sum())
            funnel_df = _apply_latency_blowout(df, funnel_df, spec, calibration)
        else:
            raise ValueError(f"Unknown anomaly type: {spec.type}")

        labels.append({
            "anomaly_id": spec.anomaly_id,
            "type": spec.type,
            "game_placement_id": spec.game_placement_id,
            "country": spec.country,
            "date_start": spec.date_start,
            "date_end": spec.date_end,
            "severity": spec.severity,
            "n_journeys_affected": n_affected,
            "description": spec.description,
        })

    # Re-generate funnel events for any newly-converted journeys (cvr_shock spikes) that don't
    # have funnel events yet, and drop stale events for journeys that got un-converted.
    converting_ids = set(df.loc[df["converts"].to_numpy(), "click_id"])
    funnel_df = funnel_df[funnel_df["journey_id"].isin(converting_ids)]
    has_funnel = set(funnel_df["journey_id"].unique())
    missing_mask = df["converts"].to_numpy() & ~df["click_id"].isin(has_funnel).to_numpy()
    if missing_mask.any():
        new_events = journeys_mod.generate_funnel_events(df[missing_mask], rng)
        funnel_df = pd.concat([funnel_df, new_events], ignore_index=True)

    return df, funnel_df, pd.DataFrame(labels)
