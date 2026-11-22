"""Invented marketplace surface (the page a click came from) for every synthetic journey.

The real click logs carry no page/surface information, so — like funnel.py — everything here is
fabricated structure, not calibrated against real data. Each click gets one surface; a conversion
inherits the surface of the click it belongs to (last-click attribution).

Surfaces differ in purchase intent (search converts best, the passive home feed worst). The CVR
multipliers are normalized by the click-share-weighted mean, so the expected overall and
per-segment conversion rates stay exactly as calibrated — the surface only redistributes
conversions between surfaces, it never adds or removes them in expectation.
"""
from __future__ import annotations

import numpy as np

# surface -> (share of clicks, relative conversion intent)
SURFACE_MIX = {
    "search": (0.30, 1.40),
    "for_you": (0.22, 0.90),
    "recommendations": (0.15, 1.00),
    "category_browse": (0.13, 0.90),
    "home_feed": (0.12, 0.65),
    "sponsored_ads": (0.08, 0.80),
}
SURFACES = list(SURFACE_MIX)
_SHARES = np.array([v[0] for v in SURFACE_MIX.values()])
_SHARES = _SHARES / _SHARES.sum()
_CUM_SHARES = np.cumsum(_SHARES)
_CUM_SHARES[-1] = 1.0
_INTENT = np.array([v[1] for v in SURFACE_MIX.values()])
# Normalized so E[multiplier] == 1 under the click mix.
CVR_MULTIPLIER = _INTENT / float((_SHARES * _INTENT).sum())


def sample_surfaces(rng: np.random.Generator, n: int):
    """Returns (surface names as an object array, per-click CVR multipliers)."""
    idx = np.clip(np.searchsorted(_CUM_SHARES, rng.random(n), side="right"), 0, len(SURFACES) - 1)
    return np.array(SURFACES, dtype=object)[idx], CVR_MULTIPLIER[idx]
