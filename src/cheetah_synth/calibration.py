"""Calibrate generator parameters from the three real Oct 2026 files.

Every distribution here is empirical (reused directly from real data) or a bootstrap pool, except
the per-segment conversion rate, which uses closed-form Beta-Binomial shrinkage (method-of-moments,
no scipy needed) to avoid overfitting to the many low-click segments. This keeps the generator
dependency-light: no scipy, no distribution-fitting library.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from . import real_data

# Below this many real transactions, a vertical's own payout pool is considered too thin and the
# global pool is used instead.
MIN_VERTICAL_POOL_SIZE = 15

# Non-converting segments still get a small non-zero shrunk CVR (via the Beta-Binomial prior)
# rather than a hard 0.0 floor. Judgment call: this gives the CVR-shock anomaly type something to
# spike *from* on segments that never convert in the real data. Flip to True for strict fidelity
# (exactly reproduce "this segment never converts") at the cost of making CVR-spike anomalies
# meaningless on those segments.
STRICT_ZERO_CVR_FLOOR = False

# When STRICT_ZERO_CVR_FLOOR is False, non-converting segments' total expected contribution to
# overall conversions is rescaled to this fraction of the real total (see compute_calibration).
NONCONVERTING_FLOOR_SHARE = 0.02

PAYOUT_COLUMNS = ["Amount", "Commission", "Payout", "Margin"]


@dataclass
class Calibration:
    month_start: pd.Timestamp
    month_end: pd.Timestamp
    daily_counts: pd.Series  # index: date (python date), value: int click count
    segment_game_placement_id: np.ndarray
    segment_country: np.ndarray
    segment_cum_share: np.ndarray
    segment_shrunk_cvr: np.ndarray
    hour_cum_probs: np.ndarray  # length 24
    duration_pool: np.ndarray  # seconds, from real Transaction Report
    payout_pools: Dict[str, np.ndarray]  # vertical name (or "__global__") -> (n, 4) array
    placement_meta: pd.DataFrame  # indexed by game_placement_id: vertical, category
    country_null_rate: float
    beta_alpha: float
    beta_beta: float

    def save(self, cache_dir: Path) -> None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / "calibration.pkl", "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(cache_dir: Path) -> "Calibration":
        with open(Path(cache_dir) / "calibration.pkl", "rb") as f:
            return pickle.load(f)


def _fit_beta_prior(raw_cvr: np.ndarray) -> tuple[float, float]:
    """Method-of-moments Beta(alpha, beta) fit, closed-form, no scipy."""
    mean_p = float(np.mean(raw_cvr))
    var_p = float(np.var(raw_cvr))
    max_var = mean_p * (1 - mean_p)
    if var_p <= 0 or var_p >= max_var:
        # Degenerate sample variance (common with few, tightly-clustered observations) — fall back
        # to a weakly-informative prior centered on the observed mean.
        common = 10.0
    else:
        common = mean_p * (1 - mean_p) / var_p - 1
        if common <= 0:
            common = 10.0
    alpha = max(mean_p * common, 1e-3)
    beta = max((1 - mean_p) * common, 1e-3)
    return alpha, beta


def compute_calibration(data_dir: Path) -> Calibration:
    clicks = real_data.load_clicks(data_dir)
    tx = real_data.load_transactions(data_dir)
    perf = real_data.load_performance(data_dir)

    month_start = pd.Timestamp(clicks["Time"].dt.date.min(), tz="UTC")
    month_end = pd.Timestamp(clicks["Time"].dt.date.max(), tz="UTC") + pd.Timedelta(
        hours=23, minutes=59, seconds=59, microseconds=999999
    )

    # --- daily volume shape ---
    daily_counts = clicks["Time"].dt.date.value_counts().sort_index()

    # --- segment traffic + conversion rate (with shrinkage) ---
    seg_clicks = (
        clicks.groupby(["Game Placement ID", "Country"]).size().rename("clicks").reset_index()
    )
    seg_conversions = (
        tx.groupby(["Game Placement ID", "Country"]).size().rename("conversions").reset_index()
    )
    segments = seg_clicks.merge(seg_conversions, on=["Game Placement ID", "Country"], how="left")
    segments["conversions"] = segments["conversions"].fillna(0).astype(np.int64)

    converting = segments[segments["conversions"] > 0]
    raw_cvr = (converting["conversions"] / converting["clicks"]).to_numpy()
    alpha, beta = _fit_beta_prior(raw_cvr)

    shrunk = ((alpha + segments["conversions"]) / (alpha + beta + segments["clicks"])).to_numpy()

    if STRICT_ZERO_CVR_FLOOR:
        shrunk = np.where(segments["conversions"] > 0, shrunk, 0.0)
    else:
        # The raw Beta-Binomial posterior, applied uniformly, gives real-zero-conversion segments
        # (there are ~20,000 of them, holding the bulk of total click volume) a floor rate that in
        # aggregate adds ~40% more expected conversions than the real total (measured: ~4,180 vs.
        # 2,979) — the actually-converting segments alone already reproduce the real total closely
        # (~2,965 vs. 2,960). Rescale *only* the non-converting segments' floor down so their total
        # expected contribution is a small, deliberately non-zero fraction of the real total — kept
        # non-zero so the CVR-shock anomaly has something to spike from, but small enough that the
        # overall/per-segment CVR calibration checks aren't dominated by invented floor noise.
        is_nonconverting = (segments["conversions"] == 0).to_numpy()
        nonconverting_expected = float((segments.loc[is_nonconverting, "clicks"] * shrunk[is_nonconverting]).sum())
        real_total = float(segments["conversions"].sum())
        target_nonconverting_total = NONCONVERTING_FLOOR_SHARE * real_total
        if nonconverting_expected > 0:
            floor_scale = target_nonconverting_total / nonconverting_expected
            shrunk[is_nonconverting] *= floor_scale
    segments["shrunk_cvr"] = shrunk
    segments["share"] = segments["clicks"] / segments["clicks"].sum()
    segments = segments.sort_values(["Game Placement ID", "Country"]).reset_index(drop=True)
    cum_share = np.cumsum(segments["share"].to_numpy())
    cum_share[-1] = 1.0  # guard against float drift

    # --- intra-day timing shape ---
    hour_counts = clicks["Time"].dt.hour.value_counts().reindex(range(24), fill_value=0)
    hour_probs = hour_counts.to_numpy() / hour_counts.sum()
    hour_cum_probs = np.cumsum(hour_probs)
    hour_cum_probs[-1] = 1.0

    # --- duration bootstrap pool ---
    duration_pool = tx["Click to Conversion Duration"].to_numpy(dtype=np.float64)

    # --- placement -> (vertical, category), from the 102 converting placements ---
    placement_meta = (
        perf.groupby("Game Placement ID")[["Vertical Name", "Category Name"]]
        .first()
        .rename(columns={"Vertical Name": "vertical", "Category Name": "category"})
    )

    # --- payout/commission/margin/amount bootstrap pools, stratified by vertical ---
    tx_with_vertical = tx.merge(
        placement_meta, left_on="Game Placement ID", right_index=True, how="left"
    )
    payout_pools: Dict[str, np.ndarray] = {
        "__global__": tx[PAYOUT_COLUMNS].to_numpy(dtype=np.float64)
    }
    for vertical, group in tx_with_vertical.groupby("vertical"):
        if len(group) >= MIN_VERTICAL_POOL_SIZE:
            payout_pools[vertical] = group[PAYOUT_COLUMNS].to_numpy(dtype=np.float64)

    country_null_rate = float(clicks["Country"].isna().mean())

    return Calibration(
        month_start=month_start,
        month_end=month_end,
        daily_counts=daily_counts,
        segment_game_placement_id=segments["Game Placement ID"].to_numpy(),
        segment_country=segments["Country"].to_numpy(),
        segment_cum_share=cum_share,
        segment_shrunk_cvr=segments["shrunk_cvr"].to_numpy(),
        hour_cum_probs=hour_cum_probs,
        duration_pool=duration_pool,
        payout_pools=payout_pools,
        placement_meta=placement_meta,
        country_null_rate=country_null_rate,
        beta_alpha=alpha,
        beta_beta=beta,
    )
