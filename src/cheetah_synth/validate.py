"""Validates that synthetic rollups reproduce the real Oct 2026 aggregates within tolerance —
the gate that proves calibration actually worked, before anyone trusts this data for anomaly
detection or TabPFN experiments.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .calibration import Calibration
from .config import ToleranceConfig


def _ecdf_max_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Max absolute difference between two empirical CDFs on a shared grid. Dependency-free
    stand-in for scipy.stats.kstest."""
    grid = np.sort(np.concatenate([a, b]))
    cdf_a = np.searchsorted(np.sort(a), grid, side="right") / len(a)
    cdf_b = np.searchsorted(np.sort(b), grid, side="right") / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


def compare_to_real(
    real_clicks: pd.DataFrame,
    real_tx: pd.DataFrame,
    real_perf: pd.DataFrame,
    synthetic_journeys: pd.DataFrame,
    synthetic_tx: pd.DataFrame,
    synthetic_perf: pd.DataFrame,
    synthetic_funnel: pd.DataFrame,
    calibration: Calibration,
    tolerances: ToleranceConfig,
    excluded_segments: Optional[set] = None,
    flat_daily_profile: bool = False,
) -> Dict[str, Any]:
    excluded_segments = excluded_segments or set()
    checks: List[Dict[str, Any]] = []

    # Anomalies are deliberate distortions, not calibration error — every aggregate check below
    # compares the real data against the *clean* (non-anomalous) slice of the synthetic data, so
    # this validation tests calibration fidelity, not whether anomalies "ruined" the aggregates.
    if "anomaly_id" in synthetic_journeys.columns:
        clean_journeys = synthetic_journeys[synthetic_journeys["anomaly_id"].isna()]
    else:
        clean_journeys = synthetic_journeys
    clean_tx_ids = set(
        clean_journeys.loc[clean_journeys["converts"] & ~clean_journeys["censored"], "transaction_id"]
        .dropna().astype(np.int64)
    )
    clean_tx = synthetic_tx[synthetic_tx["Transaction ID"].isin(clean_tx_ids)] if clean_tx_ids else synthetic_tx.iloc[0:0]

    # 1. Overall CVR
    real_cvr = len(real_tx) / len(real_clicks)
    synth_cvr = clean_journeys["converts"].sum() / len(clean_journeys)
    rel_err = abs(synth_cvr - real_cvr) / real_cvr
    checks.append({
        "name": "overall_cvr",
        "real": real_cvr, "synthetic": synth_cvr, "rel_error": rel_err,
        "tolerance": tolerances.overall_cvr_rel_tol,
        "passed": rel_err <= tolerances.overall_cvr_rel_tol,
    })

    # 2. Per-segment CVR for top-K real segments by click volume, excluding anomaly targets
    real_seg = real_clicks.groupby(["Game Placement ID", "Country"]).size().rename("clicks")
    real_conv = real_tx.groupby(["Game Placement ID", "Country"]).size().rename("conversions")
    real_seg_cvr = (real_conv / real_seg).dropna().sort_values(ascending=False)
    top_segments = real_seg.sort_values(ascending=False).index[: tolerances.top_k_segments]

    synth_seg = synthetic_journeys.groupby(["game_placement_id", "country"], dropna=False).size()
    synth_conv = synthetic_journeys[synthetic_journeys["converts"]].groupby(
        ["game_placement_id", "country"], dropna=False
    ).size()

    seg_failures = []
    seg_skipped_thin = 0
    for placement, country in top_segments:
        if (placement, country) in excluded_segments:
            continue
        r_conversions = real_conv.get((placement, country), 0)
        if r_conversions < tolerances.min_segment_conversions_for_cvr_check:
            seg_skipped_thin += 1
            continue
        r_cvr = r_conversions / real_seg.get((placement, country), 1)
        s_clicks = synth_seg.get((placement, country), 0)
        if s_clicks == 0:
            continue
        s_cvr = synth_conv.get((placement, country), 0) / s_clicks
        if r_cvr == 0:
            continue
        rel = abs(s_cvr - r_cvr) / r_cvr
        if rel > tolerances.segment_cvr_rel_tol:
            seg_failures.append({"placement": placement, "country": country,
                                  "real_cvr": r_cvr, "synthetic_cvr": s_cvr, "rel_error": rel})
    checks.append({
        "name": "per_segment_cvr_top_k",
        "n_checked": len(top_segments) - len(excluded_segments & set(top_segments)) - seg_skipped_thin,
        "n_skipped_thin_sample": seg_skipped_thin,
        "n_failed": len(seg_failures),
        "failures": seg_failures,
        "passed": len(seg_failures) == 0,
    })

    # 3. Daily volume curve correlation (over days actually present in the synthetic run,
    # excluding anomaly-injected volume — a traffic burst is supposed to break this correlation
    # on its own days, so it would otherwise mask whether the *baseline* shape is well-calibrated)
    synth_daily = clean_journeys.groupby(clean_journeys["time"].dt.date).size()
    common_days = sorted(set(synth_daily.index) & set(calibration.daily_counts.index))
    real_series = calibration.daily_counts.loc[common_days].to_numpy(dtype=float)
    synth_series = synth_daily.loc[common_days].to_numpy(dtype=float)
    corr = float(np.corrcoef(real_series, synth_series)[0, 1]) if len(common_days) > 1 else float("nan")
    if flat_daily_profile:
        # The flat profile deliberately replaces the real day-to-day shape with the monthly mean,
        # so correlation against the real curve is meaningless — check flatness instead.
        mean = float(synth_series.mean()) if len(synth_series) else float("nan")
        cv = float(synth_series.std() / mean) if len(synth_series) > 1 and mean > 0 else float("nan")
        checks.append({
            "name": "daily_volume_flat",
            "cv": cv, "n_days": len(common_days),
            "real_mean": float(real_series.mean()) if len(real_series) else float("nan"),
            "synthetic_mean": mean,
            "tolerance": 0.05,
            "passed": bool(cv <= 0.05) if not np.isnan(cv) else False,
        })
    else:
        checks.append({
            "name": "daily_volume_correlation",
            "correlation": corr, "n_days": len(common_days),
            "tolerance": tolerances.daily_volume_corr_min,
            "passed": bool(corr >= tolerances.daily_volume_corr_min) if not np.isnan(corr) else False,
        })

    # 4. Payout/commission/payout/margin/amount distribution comparison
    dist_checks = []
    for col, real_col in (("Amount", "Amount"), ("Commission", "Commission"),
                           ("Payout", "Payout"), ("Margin", "Margin")):
        if len(clean_tx) == 0:
            break
        real_vals = real_tx[real_col].to_numpy(dtype=float)
        synth_vals = clean_tx[col].to_numpy(dtype=float)
        diff = _ecdf_max_diff(real_vals, synth_vals)
        dist_checks.append({
            "column": col, "ecdf_max_diff": diff,
            "real_percentiles": np.percentile(real_vals, [25, 50, 75, 90, 99]).tolist(),
            "synthetic_percentiles": np.percentile(synth_vals, [25, 50, 75, 90, 99]).tolist(),
            "passed": diff <= tolerances.ecdf_diff_max,
        })
    checks.append({
        "name": "payout_distribution",
        "columns": dist_checks,
        "passed": all(c["passed"] for c in dist_checks) if dist_checks else False,
    })

    # 5. Funnel structural invariants
    funnel_ok = True
    funnel_detail = {}
    if len(synthetic_funnel) > 0:
        stage_counts = synthetic_funnel.groupby("stage").size()
        n_converting = int(synthetic_journeys["converts"].sum())
        funnel_detail["stage_counts"] = stage_counts.to_dict()
        funnel_detail["n_converting"] = n_converting
        funnel_ok = bool((stage_counts == n_converting).all())
        wide = synthetic_funnel.pivot_table(index="journey_id", columns="stage", values="stage_time")
        from .funnel import ALL_STAGES
        ordered_cols = [s for s in ALL_STAGES if s in wide.columns]
        diffs = wide[ordered_cols].diff(axis=1).iloc[:, 1:]
        monotonic = bool((diffs.to_numpy(dtype="timedelta64[ns]") >= np.timedelta64(0, "ns")).all())
        funnel_ok = funnel_ok and monotonic
        funnel_detail["monotonic"] = monotonic
    checks.append({"name": "funnel_structural_invariants", "passed": funnel_ok, **funnel_detail})

    # 6. Cross-report consistency
    cross_ok = int(synthetic_perf["Transaction Count"].sum()) == len(synthetic_tx)
    checks.append({
        "name": "performance_transaction_count_matches_transaction_report",
        "performance_sum": int(synthetic_perf["Transaction Count"].sum()),
        "transaction_report_rows": len(synthetic_tx),
        "passed": cross_ok,
    })

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }


def write_report(report: Dict[str, Any], out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "validation_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    lines = [f"# Validation Report\n\nOverall: {'PASS' if report['passed'] else 'FAIL'}\n"]
    for check in report["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        lines.append(f"## {check['name']}: {status}\n")
        for k, v in check.items():
            if k in ("name", "passed"):
                continue
            lines.append(f"- **{k}**: {v}")
        lines.append("")
    with open(out_dir / "validation_report.md", "w") as f:
        f.write("\n".join(lines))
