"""Vectorized per-day journey simulator.

One synthetic click == one journey. Click volume, segment mix, intra-day timing, conversion rate,
duration, and payout are all calibrated against the real Oct 2026 data (see calibration.py). The
funnel stages between install and conversion (funnel.py) are the one fully-invented piece.

Performance note: everything here is vectorized per-day (31 chunks for a full run), never a
per-row Python loop — a naive per-row loop over 3.6M rows would be unacceptably slow.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import funnel, surfaces
from .calibration import Calibration
from .config import RunConfig
from .identities import IdAllocator, SYNTH_CLICK_ID_START, SYNTH_TRANSACTION_ID_START

JOURNEY_COLUMNS = [
    "click_id",
    "time",
    "game_placement_id",
    "country",
    "surface",
    "converts",
    "censored",
    "transaction_id",
    "conversion_time",
    "duration_seconds",
    "amount",
    "commission",
    "payout",
    "margin",
    "vertical",
    "category",
    "anomaly_id",
]


def sample_segments(calibration: Calibration, rng: np.random.Generator, n: int):
    draws = rng.random(n)
    idx = np.clip(np.searchsorted(calibration.segment_cum_share, draws, side="right"), 0,
                  len(calibration.segment_cum_share) - 1)
    placements = calibration.segment_game_placement_id[idx]
    countries = calibration.segment_country[idx]
    cvr = calibration.segment_shrunk_cvr[idx]
    return placements, countries, cvr


def _as_utc_midnight(day) -> pd.Timestamp:
    base = pd.Timestamp(day)
    return base.tz_localize("UTC") if base.tzinfo is None else base.tz_convert("UTC")


def sample_timestamps_in_day(calibration: Calibration, rng: np.random.Generator,
                              day: pd.Timestamp, n: int) -> pd.DatetimeIndex:
    draws = rng.random(n)
    hour_idx = np.clip(np.searchsorted(calibration.hour_cum_probs, draws, side="right"), 0, 23)
    secs_in_hour = rng.integers(0, 3600, size=n)
    micros = rng.integers(0, 1_000_000, size=n)
    total_us = (hour_idx.astype(np.int64) * 3600 + secs_in_hour) * 1_000_000 + micros
    base = _as_utc_midnight(day)
    return base + pd.to_timedelta(total_us, unit="us")


def apply_country_nulls(calibration: Calibration, rng: np.random.Generator,
                         countries: np.ndarray) -> np.ndarray:
    countries = countries.astype(object)
    null_mask = rng.random(len(countries)) < calibration.country_null_rate
    countries[null_mask] = None
    return countries


def bootstrap_duration(calibration: Calibration, rng: np.random.Generator, n: int,
                        jitter_sigma: float = 0.05) -> np.ndarray:
    samples = rng.choice(calibration.duration_pool, size=n, replace=True)
    jitter = rng.lognormal(mean=0.0, sigma=jitter_sigma, size=n)
    return samples * jitter


def bootstrap_payout(calibration: Calibration, rng: np.random.Generator,
                      placements: np.ndarray, jitter_sigma: float = 0.0) -> np.ndarray:
    """jitter_sigma defaults to 0: the real Amount/Commission/Payout/Margin values cluster at
    specific repeated magnitudes (fixed-rate commissions), so even small multiplicative jitter
    smears those point masses and visibly hurts distribution-match validation — measured: ECDF
    max-diff goes from ~0.01 (no jitter) to ~0.14 (5% jitter). Unlike duration (3.6M draws from a
    ~3k-row pool), payout is only drawn a few thousand times, so exact-duplicate reuse isn't a
    real concern here and the jitter isn't worth its cost."""
    n = len(placements)
    out = np.empty((n, 4), dtype=np.float64)
    verticals = calibration.placement_meta["vertical"].reindex(placements).to_numpy()
    jitter = rng.lognormal(mean=0.0, sigma=jitter_sigma, size=n)
    for vertical in pd.unique(verticals):
        mask = (verticals == vertical) if pd.notna(vertical) else pd.isna(verticals)
        pool = calibration.payout_pools.get(vertical, calibration.payout_pools["__global__"]) \
            if pd.notna(vertical) else calibration.payout_pools["__global__"]
        k = int(mask.sum())
        if k == 0:
            continue
        rows = rng.integers(0, len(pool), size=k)
        out[mask] = pool[rows] * jitter[mask, None]
    return np.clip(out, 0.0, None)


MONEY_COLUMNS = ["amount", "commission", "payout", "margin"]
# Below this relative miss, a day's best resampled total is kept as-is (exact real pool values);
# above it, the remainder is closed with a uniform per-day scale factor. Scaling is avoided
# where possible for the same reason bootstrap_payout() avoids jitter: it smears the real
# fixed-rate point masses that the ECDF validation check relies on.
FLATTEN_RESIDUAL_TOL = 0.02


def _payout_pool_for(calibration: Calibration, vertical) -> np.ndarray:
    if pd.isna(vertical):
        return calibration.payout_pools["__global__"]
    return calibration.payout_pools.get(vertical, calibration.payout_pools["__global__"])


def flatten_daily_revenue(df: pd.DataFrame, calibration: Calibration, rng: np.random.Generator,
                           noise: float = 0.03, n_candidates: int = 256) -> pd.DataFrame:
    """Pins each conversion day's total Amount near the month's median day (times a small
    `1 + N(0, noise)` wiggle), mutating df in place. Returns a per-day summary for logging.

    Why this exists: ~100 conversions/day with heavy-tailed real amounts give the calibrated
    baseline ~45% day-to-day revenue volatility, which buries any injected anomaly. Instead of
    rescaling amounts, each day's payouts are re-drawn `n_candidates` times from the same
    vertical-stratified real pools and the draw whose total lands closest to the target is kept,
    so values stay exact real pool rows. Only when no candidate gets within FLATTEN_RESIDUAL_TOL
    (typically day 1, which has no prior-month clicks feeding lagged conversions) is the rest
    closed with a uniform scale factor. Censored conversions (realized after month end) are left
    untouched, since they never show up in any October day's total."""
    live = df["converts"].to_numpy(dtype=bool) & ~df["censored"].to_numpy(dtype=bool)
    if not live.any():
        return pd.DataFrame(columns=["day", "before", "target", "after", "scaled"])
    live_idx = df.index[live]
    conv_day = df.loc[live_idx, "conversion_time"].dt.date.to_numpy()
    verticals = calibration.placement_meta["vertical"].reindex(
        df.loc[live_idx, "game_placement_id"].to_numpy()
    ).to_numpy()
    amounts = df.loc[live_idx, "amount"].to_numpy(dtype=float)
    days = sorted(pd.unique(conv_day))
    before = {d: float(amounts[conv_day == d].sum()) for d in days}
    base = float(np.median(list(before.values())))

    summary = []
    for d in days:
        in_day = conv_day == d
        rows = live_idx[in_day]
        day_verticals = verticals[in_day]
        n = len(rows)
        target = base * max(0.0, 1.0 + rng.normal(0.0, noise))
        candidates = np.empty((n_candidates, n, 4), dtype=np.float64)
        for vertical in pd.unique(day_verticals):
            mask = (day_verticals == vertical) if pd.notna(vertical) else pd.isna(day_verticals)
            pool = _payout_pool_for(calibration, vertical)
            picks = rng.integers(0, len(pool), size=(n_candidates, int(mask.sum())))
            candidates[:, mask, :] = pool[picks]
        candidates = np.clip(candidates, 0.0, None)
        totals = candidates[:, :, 0].sum(axis=1)
        best = int(np.argmin(np.abs(totals - target)))
        chosen = candidates[best]
        scaled = False
        if totals[best] > 0 and abs(totals[best] / target - 1.0) > FLATTEN_RESIDUAL_TOL:
            chosen = chosen * (target / totals[best])
            scaled = True
        df.loc[rows, MONEY_COLUMNS] = chosen
        summary.append({"day": d, "before": before[d], "target": target,
                        "after": float(chosen[:, 0].sum()), "scaled": scaled})
    return pd.DataFrame(summary)


def materialize_conversions(df: pd.DataFrame, mask: np.ndarray, calibration: Calibration,
                             rng: np.random.Generator, txn_alloc: IdAllocator) -> None:
    """Fills in duration/conversion/payout fields for rows where mask is True. Mutates df in place."""
    n = int(mask.sum())
    if n == 0:
        return
    idx = df.index[mask]
    durations = bootstrap_duration(calibration, rng, n)
    click_times = df.loc[idx, "time"]
    conversion_times = click_times + pd.to_timedelta(durations, unit="s")
    censored = (conversion_times > calibration.month_end).to_numpy()
    placements = df.loc[idx, "game_placement_id"].to_numpy()
    payout = bootstrap_payout(calibration, rng, placements)
    txn_ids = txn_alloc.next_batch(n)
    meta = calibration.placement_meta.reindex(placements)

    df.loc[idx, "converts"] = True
    df.loc[idx, "duration_seconds"] = durations
    df.loc[idx, "conversion_time"] = conversion_times.to_numpy()
    df.loc[idx, "censored"] = censored
    df.loc[idx, "amount"] = payout[:, 0]
    df.loc[idx, "commission"] = payout[:, 1]
    df.loc[idx, "payout"] = payout[:, 2]
    df.loc[idx, "margin"] = payout[:, 3]
    df.loc[idx, "transaction_id"] = txn_ids
    df.loc[idx, "vertical"] = meta["vertical"].fillna("Other (Unmapped)").to_numpy()
    df.loc[idx, "category"] = meta["category"].fillna("Other (Unmapped)").to_numpy()


def clear_conversion(df: pd.DataFrame, mask: np.ndarray) -> None:
    """Reverts rows where mask is True back to a non-converting state (used by the CVR-shock
    anomaly when a previously-converting row is redrawn as non-converting)."""
    idx = df.index[mask]
    df.loc[idx, "converts"] = False
    df.loc[idx, ["conversion_time"]] = pd.NaT
    for col in ("duration_seconds", "amount", "commission", "payout", "margin", "transaction_id"):
        df.loc[idx, col] = np.nan
    df.loc[idx, "censored"] = False
    df.loc[idx, ["vertical", "category"]] = None


def generate_funnel_events(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Long-format table of funnel stage events for every converting journey in df (including
    censored ones — this internal table is not bound by the real export window)."""
    converting = df[df["converts"]]
    n = len(converting)
    if n == 0:
        return pd.DataFrame(columns=["journey_id", "stage", "stage_time"])

    click_ids = converting["click_id"].to_numpy()
    click_times = converting["time"]
    durations = converting["duration_seconds"].to_numpy()

    frames = [pd.DataFrame({
        "journey_id": click_ids,
        "stage": "click",
        "stage_time": click_times.to_numpy(),
    })]
    fractions = funnel.sample_stage_fractions(rng, n)  # (n, 5)
    for j, stage in enumerate(funnel.STAGES_AFTER_CLICK):
        offsets_seconds = fractions[:, j] * durations
        stage_times = click_times + pd.to_timedelta(offsets_seconds, unit="s")
        frames.append(pd.DataFrame({
            "journey_id": click_ids,
            "stage": stage,
            "stage_time": stage_times.to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True)


def _empty_conversion_columns(n: int) -> dict:
    return {
        "censored": np.zeros(n, dtype=bool),
        "transaction_id": np.full(n, np.nan),
        "conversion_time": pd.Series([pd.NaT] * n, dtype="datetime64[ns, UTC]"),
        "duration_seconds": np.full(n, np.nan),
        "amount": np.full(n, np.nan),
        "commission": np.full(n, np.nan),
        "payout": np.full(n, np.nan),
        "margin": np.full(n, np.nan),
        "vertical": np.array([None] * n, dtype=object),
        "category": np.array([None] * n, dtype=object),
        "anomaly_id": np.array([None] * n, dtype=object),
    }


def simulate_month(calibration: Calibration, run_config: RunConfig,
                    rng: np.random.Generator,
                    click_alloc: Optional[IdAllocator] = None,
                    txn_alloc: Optional[IdAllocator] = None):
    click_alloc = click_alloc or IdAllocator(SYNTH_CLICK_ID_START)
    txn_alloc = txn_alloc or IdAllocator(SYNTH_TRANSACTION_ID_START)

    dates = sorted(calibration.daily_counts.index)[: run_config.scale.days]
    flat = getattr(run_config, "daily_profile", "calibrated") == "flat"
    mean_daily_clicks = float(calibration.daily_counts.mean())
    day_frames = []
    for day in dates:
        if flat:
            # Flat profile: the calibrated monthly mean with a small wiggle, instead of the real
            # day-to-day click shape.
            base_n = mean_daily_clicks * max(0.0, 1.0 + rng.normal(0.0, run_config.flat_noise * 2 / 3))
        else:
            base_n = int(calibration.daily_counts.loc[day])
        n = int(round(base_n * run_config.scale.scale))
        if n <= 0:
            continue
        placements, countries, cvr = sample_segments(calibration, rng, n)
        countries = apply_country_nulls(calibration, rng, countries)
        times = sample_timestamps_in_day(calibration, rng, pd.Timestamp(day), n)
        click_ids = click_alloc.next_batch(n)
        surface, cvr_mult = surfaces.sample_surfaces(rng, n)
        converts = rng.random(n) < np.clip(cvr * cvr_mult, 0.0, 1.0)

        day_df = pd.DataFrame({
            "click_id": click_ids,
            "time": times,
            "game_placement_id": placements,
            "country": countries,
            "surface": surface,
            "converts": converts,
        })
        for col, values in _empty_conversion_columns(n).items():
            day_df[col] = values
        day_frames.append(day_df)

    journeys = pd.concat(day_frames, ignore_index=True)
    materialize_conversions(journeys, journeys["converts"].to_numpy(), calibration, rng, txn_alloc)
    funnel_events = generate_funnel_events(journeys, rng)
    return journeys, funnel_events, click_alloc, txn_alloc
