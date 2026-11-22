"""Pushes a cheetah_synth run into the backend's ingestion API as a synthetic demo dataset.

This is NOT real revenue data, and the reference intervals built here are NOT a TabPFN forecast —
TabPFN is explicitly not connected in this backend (see backend/README.md). The "naive trailing
mean" baseline below exists purely so the backend's own deviation-detection mechanics (pause on
breach -> Incident -> blocked-configuration AgentTask) can be exercised end to end without any real
forecasting/AI integration. Every record pushed here carries source_kind="synthetic" /
source="synthetic_naive_baseline" so it is unambiguous in the UI/database that none of this is real
data or a real model output.

Requires the backend already running (docker compose up, or uvicorn+worker) — this module is only
an HTTP client; it adds no dependency beyond what cheetah_synth already uses (stdlib + pandas).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import event_timeline as event_timeline_mod

MONTH_DAYS = [f"2026-10-{i:02d}" for i in range(1, 32)]


def _post(base_url: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} failed ({e.code}): {body}") from e


def load_journeys(run_dir: Path) -> pd.DataFrame:
    return pd.read_parquet(run_dir / "raw" / "journeys.parquet")


def build_daily_amounts(journeys: pd.DataFrame) -> list[dict]:
    """One DailyAmount record per Oct day — summed over conversion day (not click day), since
    that's when the revenue is actually realized."""
    converted = journeys[journeys["converts"] & ~journeys["censored"]].copy()
    converted["day"] = converted["conversion_time"].dt.strftime("%Y-%m-%d")
    totals = converted.groupby("day")[["amount", "commission", "payout", "margin"]].sum()
    totals = totals.reindex(MONTH_DAYS, fill_value=0.0)
    return [
        {
            "day": day,
            "amount": round(float(row["amount"]), 2),
            "commission": round(float(row["commission"]), 2),
            "payout": round(float(row["payout"]), 2),
            "margin": round(float(row["margin"]), 2),
            "source_kind": "synthetic",
            "source_id": None,
        }
        for day, row in totals.iterrows()
    ]


def build_naive_references(daily_amounts: list[dict], dataset_id: str, window: int = 7,
                            band_mult: float = 4.0, min_band_frac: float = 0.15) -> list[dict]:
    """A trivial trailing-median-and-MAD band per day (from day 2 onward — day 1 has no trailing
    history, left unavailable, matching the backend's own handling of missing references).
    Deliberately naive: this is a placeholder so the pipeline has *something* to compare
    against, not a forecasting model.

    Median/MAD rather than mean/std so an anomalous day inside the trailing window neither drags
    the next week's point estimate nor blows up its band — with a mean/std band, one -45% day
    would widen the following seven bands enough to hide the next anomaly.

    Sized for the `flat` daily profile (~3% day-to-day revenue noise, see
    journeys.flatten_daily_revenue): 4 robust sigmas, floored at ±15% of the point, keeps normal
    days inside the band while the four default revenue anomalies (-45%, -40%, +60%, -35%, see
    anomalies.REVENUE_ANOMALY_PLAN) each breach it. A `calibrated`-profile run (~45% CV) would
    flag many ordinary days with this width.

    `id` is scoped by `dataset_id` — `ForecastReference.id` is the backend's sole primary key
    (global across every dataset), so an unscoped id like `SYNREF-2026-10-01` would permanently
    claim that reference for the *first* dataset ever pushed, and every later dataset (even a
    brand-new `--dataset-id`) would 409 trying to post the same id again."""
    amounts = {r["day"]: r["amount"] for r in daily_amounts}
    refs = []
    for i, day in enumerate(MONTH_DAYS):
        if i == 0:
            continue
        history = np.array([amounts[MONTH_DAYS[j]] for j in range(max(0, i - window), i)], dtype=float)
        point = float(np.median(history))
        robust_sigma = 1.4826 * float(np.median(np.abs(history - point))) if len(history) > 1 else 0.0
        band = max(band_mult * robust_sigma, min_band_frac * point)
        refs.append({
            "id": f"SYNREF-{dataset_id}-{day}",
            "reference_day": day,
            "issue_day": MONTH_DAYS[i - 1],
            "point": round(point, 2),
            "lower": round(point - band, 2),
            "upper": round(point + band, 2),
            "model_version": "naive_trailing_median_v1",
            "source": "synthetic_naive_baseline",
        })
    return refs


def _event_dict(row, kind: str, source_id, event_time, amount=None, commission=None) -> dict:
    country = row.country
    # .to_pydatetime() truncates to microsecond precision — pandas' own .isoformat() can emit up
    # to 9 fractional digits (nanosecond resolution), which Python's datetime.fromisoformat()
    # only accepts on 3.11+; the backend's Dockerfile targets 3.12 but this should stay portable.
    return {
        "kind": kind,
        "source_id": str(source_id),
        "event_time_utc": event_time.to_pydatetime().isoformat(),
        "game_placement_id": row.game_placement_id,
        "country": None if pd.isna(country) else country,
        "amount": None if amount is None else round(float(amount), 2),
        "commission": None if commission is None else round(float(commission), 2),
        "extra": {},
    }


def build_source_events(journeys: pd.DataFrame, include_non_converting_sample: int = 0,
                         seed: int = 0) -> list[dict]:
    """Click+install pairs for every converting journey (modest size — a few thousand rows), plus
    an optional bounded random sample of non-converting clicks for volume texture. Deliberately
    does NOT push the full multi-million-row click log — that's not what the backend's per-day
    event batching / SSE feed is meant to carry."""
    converted = journeys[journeys["converts"] & ~journeys["censored"]]
    events = []
    for row in converted.itertuples():
        events.append(_event_dict(row, "click", row.click_id, row.time))
        events.append(_event_dict(row, "install", int(row.transaction_id), row.conversion_time,
                                   amount=row.amount, commission=row.commission))
    if include_non_converting_sample > 0:
        non_converting = journeys[~journeys["converts"]]
        n = min(include_non_converting_sample, len(non_converting))
        for row in non_converting.sample(n=n, random_state=seed).itertuples():
            events.append(_event_dict(row, "click", row.click_id, row.time))
    return events


def _backend_provider(forecast_provider: str) -> str:
    """CLI name -> backend Run.forecast_provider. 'tabpfn' runs get their references from the
    backend worker (backend/forecasting.py); 'naive' runs use the imported placeholder band."""
    if forecast_provider not in ("naive", "tabpfn"):
        raise ValueError(f"unknown forecast_provider {forecast_provider!r}")
    return "tabpfn" if forecast_provider == "tabpfn" else "external"


def _push_naive_references(backend_url: str, dataset_id: str, journeys: pd.DataFrame,
                           forecast_provider: str) -> dict[str, Any]:
    if forecast_provider == "tabpfn":
        return {"references": 0}  # the worker issues TabPFN references itself, day by day
    references = build_naive_references(build_daily_amounts(journeys), dataset_id)
    refs_resp = _post(backend_url, "/api/v1/forecasts/references",
                      {"dataset_id": dataset_id, "references": references})
    return {"references": len(references), "references_response": refs_resp}


def register_live_run(run_dir: Path, backend_url: str, dataset_id: str,
                       create_run: bool = True, start: bool = False,
                       forecast_provider: str = "naive") -> dict[str, Any]:
    """Registers a run for the backend's `live_events` ingestion mode: the worker streams
    `event_timeline.parquet` in live, per event, as the simulated clock advances — see
    `event_timeline.py`. Unlike `push_run_to_backend()`, this does NOT bulk-POST DailyAmount or
    SourceEvent rows; the worker computes each day's DailyAmount itself from the events it has
    actually delivered by the time that day closes. Forecast references still get pre-loaded here
    (they represent an external model's output issued before the day it evaluates, which is
    unrelated to the live/bulk ingestion distinction)."""
    timeline_path = run_dir / "raw" / "event_timeline.parquet"
    if not timeline_path.exists():
        journeys = load_journeys(run_dir)
        event_timeline_mod.build_event_timeline(journeys).to_parquet(timeline_path, index=False)
    # The backend (uvicorn/worker, started via start_backend.sh) runs with its cwd set to
    # backend/, not the repo root this CLI runs from — a relative path here would resolve to the
    # wrong place server-side, so always send an absolute path.
    timeline_path = timeline_path.resolve()

    result = {"dataset_id": dataset_id, "event_source_path": str(timeline_path)}
    result.update(_push_naive_references(backend_url, dataset_id, load_journeys(run_dir), forecast_provider))

    if create_run:
        run_resp = _post(backend_url, "/api/v1/runs", {
            "dataset_id": dataset_id,
            "ingestion_mode": "live_events",
            "event_source_path": str(timeline_path),
            "forecast_provider": _backend_provider(forecast_provider),
        })
        result["run_id"] = run_resp["run_id"]
        if start:
            _post(backend_url, f"/api/v1/runs/{run_resp['run_id']}/commands", {"command": "start"})
            result["started"] = True
    return result


def push_run_to_backend(run_dir: Path, backend_url: str, dataset_id: str,
                         include_non_converting_sample: int = 0, seed: int = 0,
                         create_run: bool = True, start: bool = False,
                         event_batch_size: int = 500, forecast_provider: str = "naive") -> dict[str, Any]:
    journeys = load_journeys(run_dir)

    daily_amounts = build_daily_amounts(journeys)
    daily_resp = _post(backend_url, "/api/v1/datasets/daily",
                        {"dataset_id": dataset_id, "records": daily_amounts})

    events = build_source_events(journeys, include_non_converting_sample, seed)
    for i in range(0, len(events), event_batch_size):
        _post(backend_url, "/api/v1/datasets/events",
              {"dataset_id": dataset_id, "events": events[i:i + event_batch_size]})

    result = {"dataset_id": dataset_id, "daily_amounts": len(daily_amounts),
              "source_events": len(events), "daily_response": daily_resp}
    result.update(_push_naive_references(backend_url, dataset_id, journeys, forecast_provider))

    if create_run:
        run_resp = _post(backend_url, "/api/v1/runs", {
            "dataset_id": dataset_id, "forecast_provider": _backend_provider(forecast_provider)})
        result["run_id"] = run_resp["run_id"]
        if start:
            _post(backend_url, f"/api/v1/runs/{run_resp['run_id']}/commands", {"command": "start"})
            result["started"] = True
    return result
