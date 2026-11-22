"""Invented generic mobile-app funnel between click and conversion.

The real data has zero intermediate-funnel granularity (just a click, and — rarely — an
install/conversion). Everything in this module is fabricated structure, not derived from or
calibrated against real data. The one thing that *is* real is the total click-to-conversion
duration (bootstrapped from the Transaction Report in calibration.py) — each converting journey's
stage timestamps are a Dirichlet-weighted split of that real total, so the invented stages decompose
a real signal rather than inventing an independent one.
"""
from __future__ import annotations

import numpy as np

# Stages after "click". "click" itself is always at offset 0 and is not sampled.
STAGES_AFTER_CLICK = ["install", "app_open", "onboarding_complete", "key_action", "conversion"]
ALL_STAGES = ["click", *STAGES_AFTER_CLICK]

# Dirichlet concentration for the 5 gaps between the 6 stages (click->install,
# install->app_open, app_open->onboarding_complete, onboarding_complete->key_action,
# key_action->conversion). Hand-picked, invented: most of the real click-to-conversion delay sits
# in the pre-install dwell period (gap 0) and in deciding to make the tracked purchase/action after
# onboarding (gap 4); the in-app steps right after install happen comparatively quickly.
GAP_CONCENTRATION = np.array([5.0, 3.0, 1.0, 1.0, 2.0])


def sample_stage_fractions(rng: np.random.Generator, n: int) -> np.ndarray:
    """Returns an (n, 5) array of cumulative fractions of total duration for each
    post-click stage, i.e. column j is the fraction of the way through the journey's total
    duration that stage STAGES_AFTER_CLICK[j] occurs. Monotonically increasing per row, last
    column == 1.0 (conversion happens exactly at the sampled total duration)."""
    gaps = rng.dirichlet(GAP_CONCENTRATION, size=n)  # (n, 5), rows sum to 1
    return np.cumsum(gaps, axis=1)
