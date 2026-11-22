"""Run configuration for the synthetic journey-data generator.

The real anchor data is October 2026 (31 days) — see data/ at the repo root.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MONTH_START = "2026-10-01"
MONTH_END = "2026-10-31"
MONTH_NUM_DAYS = 31


@dataclass
class PathsConfig:
    data_dir: Path = REPO_ROOT / "data"
    out_dir: Path = REPO_ROOT / "data" / "synthetic"

    @property
    def calibration_cache_dir(self) -> Path:
        return self.out_dir / "_calibration"

    def run_dir(self, run_id: str) -> Path:
        return self.out_dir / run_id


@dataclass
class ScaleConfig:
    # Multiplies calibrated daily click volume. 1.0 = full real-data scale (~3.6M clicks/month).
    scale: float = 1.0
    # Dev-only truncation: simulate only the first N days instead of all 31.
    # The final deliverable must use days=31 (a Performance-Report export is a whole-month
    # aggregate and is not meaningful for a truncated month).
    days: int = MONTH_NUM_DAYS


@dataclass
class ToleranceConfig:
    overall_cvr_rel_tol: float = 0.10
    segment_cvr_rel_tol: float = 0.20
    top_k_segments: int = 20
    # Below this many real conversions, a segment's CVR is too noisy (Poisson sampling error) for
    # a ±20% relative-error check to be meaningful — e.g. 2 real conversions means one extra/fewer
    # conversion swings the rate 50%+. Segments with fewer are skipped, not silently passed.
    min_segment_conversions_for_cvr_check: int = 20
    daily_volume_corr_min: float = 0.98
    ecdf_diff_max: float = 0.10


@dataclass
class RunConfig:
    run_id: str
    seed: int = 42
    anomalies: str = "default"  # "none" | "default" (4 day-level revenue anomalies) | "segment"
    # "flat": daily click volume pinned to the calibrated monthly mean and daily revenue pinned
    # near the month median (only `flat_noise` relative wiggle), so injected anomalies stand out
    # clearly against the curve. "calibrated": reproduce the real day-to-day click shape and the
    # raw (~45% CV) revenue volatility.
    daily_profile: str = "flat"
    flat_noise: float = 0.03
    paths: PathsConfig = field(default_factory=PathsConfig)
    scale: ScaleConfig = field(default_factory=ScaleConfig)
    tolerances: ToleranceConfig = field(default_factory=ToleranceConfig)

    @property
    def run_dir(self) -> Path:
        return self.paths.run_dir(self.run_id)

    @property
    def raw_dir(self) -> Path:
        return self.run_dir / "raw"

    @property
    def clicks_dir(self) -> Path:
        return self.run_dir / "clicks"

    @property
    def tabpfn_dir(self) -> Path:
        return self.run_dir / "tabpfn"

    @property
    def labels_dir(self) -> Path:
        return self.run_dir / "labels"

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.clicks_dir, self.tabpfn_dir, self.labels_dir):
            d.mkdir(parents=True, exist_ok=True)
