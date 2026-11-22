"""Labeled anomaly injection.

Two catalogs:
- `build_default_catalog`: four whole-day, aggregate-visible revenue anomalies (three below the
  daily revenue curve, one above) — the demo catalog. Each is defined by its effect on one
  conversion day's total Amount, and its scope (which countries / verticals / placements) is
  resolved at apply time from the day's actual revenue mix, so the effect size is deterministic.
- `build_segment_catalog`: the original four single-segment anomalies (traffic burst, CVR shock,
  payout shock, latency blowout) — realistic but too small to move the aggregate curve.

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
from . import surfaces as surfaces_mod
from .calibration import Calibration
from .identities import IdAllocator

ANOMALY_TYPES = ("traffic_burst", "cvr_shock", "payout_shock", "latency_blowout")
REVENUE_ANOMALY_TYPES = ("postback_outage", "advertiser_rate_cut", "promo_surge", "tracking_break")


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


def build_segment_catalog(calibration: Calibration) -> List[AnomalySpec]:
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


# (type, position in month as a fraction of the simulated span, signed impact on that day's total
# Amount, scope dimension). Positions land on Oct 7 / 14 / 20 / 27 for a full 31-day month.
REVENUE_ANOMALY_PLAN = (
    ("postback_outage", 0.20, -0.45, "country"),
    ("advertiser_rate_cut", 0.43, -0.40, "vertical"),
    ("promo_surge", 0.63, +0.60, "game_placement_id"),
    ("tracking_break", 0.87, -0.35, "game_placement_id"),
)
REVENUE_ANOMALY_DESCRIPTIONS = {
    "postback_outage": "conversion postbacks lost for the top-revenue country/countries",
    "advertiser_rate_cut": "advertiser cuts the per-conversion rate across the top vertical(s)",
    "promo_surge": "promotion drives a burst of extra conversions on the top placement(s)",
    "tracking_break": "conversion tracking breaks on the top placement(s)",
}
# Drop-type anomalies only remove up to this fraction of the scoped conversions (so the scope
# still shows some activity, like a partial outage); rate cuts never go below this multiplier.
MAX_DROP_FRACTION = 0.9
MIN_RATE_MULTIPLIER = 0.4


def build_default_catalog(calibration: Calibration) -> List[AnomalySpec]:
    """Four single-day revenue anomalies: three below the daily Amount curve, one above. Each
    `severity` is the signed relative change of that day's total Amount (e.g. -0.45 = 45% below
    what the day would otherwise have been); the affected countries/verticals/placements are
    picked at apply time as the smallest top-revenue set large enough to carry that change."""
    month_start = calibration.month_start.date()
    span = len(calibration.daily_counts) - 1
    specs = []
    for i, (anomaly_type, position, impact, scope) in enumerate(REVENUE_ANOMALY_PLAN):
        day = month_start + timedelta(days=int(round(position * span)))
        specs.append(AnomalySpec(
            anomaly_id=f"A-{i + 1:03d}-{anomaly_type}",
            type=anomaly_type,
            game_placement_id="*",
            country="*",
            date_start=day,
            date_end=day,
            severity=impact,
            params={"scope": scope, "direction": "above" if impact > 0 else "below"},
            description=REVENUE_ANOMALY_DESCRIPTIONS[anomaly_type],
        ))
    return specs


def _day_conversions(df: pd.DataFrame, day: date) -> np.ndarray:
    """Positional indices of untouched, non-censored conversions realized on `day` — the rows
    that make up that day's total Amount in the backend."""
    live = (
        df["converts"].to_numpy(dtype=bool)
        & ~df["censored"].to_numpy(dtype=bool)
        & df["anomaly_id"].isna().to_numpy()
    )
    on_day = (df["conversion_time"].dt.date == day).to_numpy()
    return np.flatnonzero(live & on_day)


def _scope_values(df: pd.DataFrame, rows: np.ndarray, scope: str, calibration: Calibration):
    if scope == "vertical":
        return calibration.placement_meta["vertical"].reindex(
            df["game_placement_id"].to_numpy()[rows]
        ).fillna("Other (Unmapped)").to_numpy()
    return df[scope].to_numpy()[rows]


def _pick_top_scope(values: np.ndarray, amounts: np.ndarray, min_share: float):
    """Smallest set of top-revenue scope values whose combined share of the day's Amount is at
    least `min_share` (or every value, if even that isn't enough). Null keys are never picked."""
    ranked = (
        pd.Series(amounts).groupby(pd.Series(values, dtype=object)).sum()
        .sort_values(ascending=False)
    )
    total = float(amounts.sum())
    picked, share = [], 0.0
    for key, value in ranked.items():
        if key is None or (isinstance(key, float) and np.isnan(key)):
            continue
        picked.append(key)
        share += float(value) / total if total > 0 else 0.0
        if share >= min_share:
            break
    return picked, share


def _resolve_scope(df, spec, calibration, min_share):
    rows = _day_conversions(df, spec.date_start)
    if len(rows) == 0:
        return rows, np.zeros(0, dtype=bool), [], 0.0, 0.0
    amounts = df["amount"].to_numpy(dtype=float)[rows]
    values = _scope_values(df, rows, spec.params["scope"], calibration)
    picked, share = _pick_top_scope(values, amounts, min_share)
    in_scope = pd.Series(values, dtype=object).isin(picked).to_numpy()
    return rows, in_scope, picked, share, float(amounts.sum())


def _label_resolved_scope(spec: AnomalySpec, picked) -> None:
    joined = ";".join(str(p) for p in picked)
    if spec.params["scope"] == "country":
        spec.country = joined
    elif spec.params["scope"] == "game_placement_id":
        spec.game_placement_id = joined
    spec.params["scope_values"] = list(picked)
    spec.description = f"{spec.description} ({spec.params['scope']}={joined or 'none'})"


def _apply_conversion_drop(df, spec, calibration, rng) -> int:
    """postback_outage / tracking_break: the clicks happened, but a revenue-weighted share of the
    scoped conversions realized that day never arrive. Removes conversions in random order until
    the day's total has dropped by |severity|."""
    impact = abs(spec.severity)
    rows, in_scope, picked, _, day_total = _resolve_scope(
        df, spec, calibration, min_share=impact / MAX_DROP_FRACTION
    )
    _label_resolved_scope(spec, picked)
    scoped = rows[in_scope]
    if len(scoped) == 0:
        return 0
    scoped = scoped[rng.permutation(len(scoped))]
    amounts = df["amount"].to_numpy(dtype=float)[scoped]
    target = impact * day_total
    n_drop = int(np.searchsorted(np.cumsum(amounts), target, side="left")) + 1
    n_drop = min(n_drop, int(np.floor(MAX_DROP_FRACTION * len(scoped))) or 1)
    dropped = scoped[:n_drop]
    mask = np.zeros(len(df), dtype=bool)
    mask[dropped] = True
    journeys_mod.clear_conversion(df, mask)
    df.loc[mask, "anomaly_id"] = spec.anomaly_id
    return int(n_drop)


def _apply_rate_cut(df, spec, calibration) -> int:
    """advertiser_rate_cut: every scoped conversion that day pays out at a reduced rate, sized so
    the day's total Amount falls by |severity|."""
    impact = abs(spec.severity)
    rows, in_scope, picked, share, _ = _resolve_scope(
        df, spec, calibration, min_share=impact / (1.0 - MIN_RATE_MULTIPLIER)
    )
    _label_resolved_scope(spec, picked)
    if not in_scope.any() or share <= 0:
        return 0
    multiplier = max(MIN_RATE_MULTIPLIER, 1.0 - impact / share)
    spec.params["rate_multiplier"] = round(multiplier, 4)
    mask = np.zeros(len(df), dtype=bool)
    mask[rows[in_scope]] = True
    for col in journeys_mod.MONEY_COLUMNS:
        df.loc[mask, col] = df.loc[mask, col] * multiplier
    df.loc[mask, "anomaly_id"] = spec.anomaly_id
    return int(mask.sum())


def _apply_promo_surge(df, spec, calibration, rng, click_alloc, txn_alloc) -> pd.DataFrame:
    """promo_surge: extra fast-converting journeys (click and conversion on the same day) on the
    top-revenue placement(s), until the day's total Amount is up by `severity`."""
    rows, in_scope, picked, _, day_total = _resolve_scope(df, spec, calibration, min_share=0.25)
    _label_resolved_scope(spec, picked)
    if not in_scope.any() or day_total <= 0:
        return df
    target = spec.severity * day_total
    scoped = rows[in_scope]
    # Draw a generous batch of candidate conversions mirroring the scoped placement/country mix,
    # then keep the shortest prefix whose revenue reaches the target.
    mean_amount = max(float(df["amount"].to_numpy(dtype=float)[scoped].mean()), 1e-6)
    n_draw = int(np.ceil(3 * target / mean_amount)) + 10
    template = scoped[rng.integers(0, len(scoped), size=n_draw)]
    placements = df["game_placement_id"].to_numpy()[template]
    payouts = journeys_mod.bootstrap_payout(calibration, rng, placements)
    n_keep = min(int(np.searchsorted(np.cumsum(payouts[:, 0]), target, side="left")) + 1, n_draw)
    template, placements, payouts = template[:n_keep], placements[:n_keep], payouts[:n_keep]

    day_start = pd.Timestamp(spec.date_start, tz="UTC")
    click_offsets_s = rng.uniform(0, 20 * 3600, size=n_keep)
    seconds_left = 24 * 3600 - 1 - click_offsets_s
    durations = rng.uniform(60, np.minimum(4 * 3600, seconds_left))
    click_times = day_start + pd.to_timedelta(click_offsets_s, unit="s")
    meta = calibration.placement_meta.reindex(placements)

    new = pd.DataFrame({
        "click_id": click_alloc.next_batch(n_keep),
        "time": click_times,
        "game_placement_id": placements,
        "country": df["country"].to_numpy()[template],
        # The promotion is pushed through the personalised feed.
        "surface": "for_you",
        "converts": True,
    })
    for col, values in journeys_mod._empty_conversion_columns(n_keep).items():
        new[col] = values
    new["conversion_time"] = click_times + pd.to_timedelta(durations, unit="s")
    new["duration_seconds"] = durations
    for j, col in enumerate(journeys_mod.MONEY_COLUMNS):
        new[col] = payouts[:, j]
    new["transaction_id"] = txn_alloc.next_batch(n_keep)
    new["vertical"] = meta["vertical"].fillna("Other (Unmapped)").to_numpy()
    new["category"] = meta["category"].fillna("Other (Unmapped)").to_numpy()
    new["anomaly_id"] = spec.anomaly_id
    return pd.concat([df, new[df.columns]], ignore_index=True)


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
                "surface": surfaces_mod.sample_surfaces(rng, extra_n)[0],
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
        elif spec.type in ("postback_outage", "tracking_break"):
            n_affected = _apply_conversion_drop(df, spec, calibration, rng)
        elif spec.type == "advertiser_rate_cut":
            n_affected = _apply_rate_cut(df, spec, calibration)
        elif spec.type == "promo_surge":
            before = len(df)
            df = _apply_promo_surge(df, spec, calibration, rng, click_alloc, txn_alloc)
            n_affected = len(df) - before
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
