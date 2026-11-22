"""A small, hand-built Calibration fixture for fast unit tests — no real data I/O."""
from __future__ import annotations

import numpy as np
import pandas as pd

from cheetah_synth.calibration import Calibration

SEGMENTS = [
    ("P1", "DE", 0.40, 0.01),   # placement, country, share, shrunk_cvr
    ("P2", "US", 0.30, 0.02),
    ("P3", "FR", 0.20, 0.005),
    ("P4", "GB", 0.06, 0.0001),  # non-converting-in-"real" placeholder
    ("P5", "PL", 0.04, 0.0001),
]


def make_calibration(n_days: int = 5) -> Calibration:
    placements = np.array([s[0] for s in SEGMENTS], dtype=object)
    countries = np.array([s[1] for s in SEGMENTS], dtype=object)
    shares = np.array([s[2] for s in SEGMENTS])
    cvr = np.array([s[3] for s in SEGMENTS])
    cum_share = np.cumsum(shares)
    cum_share[-1] = 1.0

    hour_probs = np.full(24, 1 / 24)
    hour_cum_probs = np.cumsum(hour_probs)
    hour_cum_probs[-1] = 1.0

    rng = np.random.default_rng(0)
    duration_pool = rng.uniform(3600, 86400 * 5, size=200)  # 1h .. 5d

    payout_pool = np.column_stack([
        rng.uniform(10, 500, size=200),   # Amount
        rng.uniform(1, 50, size=200),     # Commission
        rng.uniform(0.5, 30, size=200),   # Payout
        rng.uniform(0.2, 20, size=200),   # Margin
    ])
    payout_pools = {"__global__": payout_pool, "Retail": payout_pool}

    placement_meta = pd.DataFrame(
        {"vertical": ["Retail", "Retail", "Retail"], "category": ["Cat1", "Cat1", "Cat2"]},
        index=pd.Index(["P1", "P2", "P3"], name="Game Placement ID"),
    )

    month_start = pd.Timestamp("2026-10-01", tz="UTC")
    daily_counts = pd.Series(
        [1000] * n_days,
        index=[(month_start + pd.Timedelta(days=i)).date() for i in range(n_days)],
    )
    month_end = pd.Timestamp(daily_counts.index[-1], tz="UTC") + pd.Timedelta(
        hours=23, minutes=59, seconds=59, microseconds=999999
    )

    return Calibration(
        month_start=month_start,
        month_end=month_end,
        daily_counts=daily_counts,
        segment_game_placement_id=placements,
        segment_country=countries,
        segment_cum_share=cum_share,
        segment_shrunk_cvr=cvr,
        hour_cum_probs=hour_cum_probs,
        duration_pool=duration_pool,
        payout_pools=payout_pools,
        placement_meta=placement_meta,
        country_null_rate=0.0,
        beta_alpha=0.1,
        beta_beta=1.0,
    )
