"""`cheetah-synth calibrate|generate|validate|all` — see CLAUDE.md for usage examples."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import anomalies as anomalies_mod
from . import backend_adapter, event_timeline, export, journeys as journeys_mod, real_data, tabpfn_features, validate
from .calibration import Calibration, compute_calibration
from .config import PathsConfig, RunConfig, ScaleConfig
from .identities import IdAllocator, SYNTH_CLICK_ID_START, SYNTH_TRANSACTION_ID_START


def cmd_calibrate(args: argparse.Namespace) -> int:
    paths = PathsConfig(data_dir=Path(args.data_dir), out_dir=Path(args.out_dir))
    cache_dir = paths.calibration_cache_dir
    if cache_dir.exists() and not args.force:
        print(f"Calibration cache already exists at {cache_dir} (use --force to recompute).")
        return 0
    print(f"Computing calibration from {paths.data_dir} ...")
    calibration = compute_calibration(paths.data_dir)
    calibration.save(cache_dir)
    print(f"Calibration cached to {cache_dir}")
    return 0


def _load_or_compute_calibration(paths: PathsConfig) -> Calibration:
    cache_dir = paths.calibration_cache_dir
    if cache_dir.exists():
        return Calibration.load(cache_dir)
    calibration = compute_calibration(paths.data_dir)
    calibration.save(cache_dir)
    return calibration


def cmd_generate(args: argparse.Namespace) -> int:
    paths = PathsConfig(data_dir=Path(args.data_dir), out_dir=Path(args.out_dir))
    run_config = RunConfig(
        run_id=args.run_id,
        seed=args.seed,
        anomalies=args.anomalies,
        paths=paths,
        scale=ScaleConfig(scale=args.scale, days=args.days),
    )
    if args.days < 31:
        print(f"WARNING: --days {args.days} < 31 — this run is a dev-only truncation. "
              f"The Performance Report export is a whole-month aggregate and is not "
              f"representative of a full month at this truncation.", file=sys.stderr)

    run_config.ensure_dirs()
    calibration = _load_or_compute_calibration(paths)
    rng = np.random.default_rng(run_config.seed)

    print(f"Simulating {run_config.scale.days} day(s) at scale={run_config.scale.scale} ...")
    journeys, funnel_df, click_alloc, txn_alloc = journeys_mod.simulate_month(
        calibration, run_config, rng,
        click_alloc=IdAllocator(SYNTH_CLICK_ID_START),
        txn_alloc=IdAllocator(SYNTH_TRANSACTION_ID_START),
    )
    print(f"Baseline: {len(journeys)} clicks, {int(journeys['converts'].sum())} conversions.")

    specs = []
    if run_config.anomalies == "default":
        specs = anomalies_mod.build_default_catalog(calibration)
        journeys, funnel_df, labels_df = anomalies_mod.apply_anomalies(
            journeys, funnel_df, calibration, rng, specs, click_alloc, txn_alloc
        )
        print(f"After anomalies: {len(journeys)} clicks, {int(journeys['converts'].sum())} conversions.")
    else:
        labels_df = pd.DataFrame(columns=["anomaly_id", "type", "game_placement_id", "country",
                                           "date_start", "date_end", "severity",
                                           "n_journeys_affected", "description"])

    journeys.to_parquet(run_config.raw_dir / "journeys.parquet", index=False)
    funnel_df.to_parquet(run_config.raw_dir / "funnel_events.parquet", index=False)
    labels_df.to_csv(run_config.labels_dir / "anomalies.csv", index=False)

    timeline = event_timeline.build_event_timeline(journeys)
    timeline.to_parquet(run_config.raw_dir / "event_timeline.parquet", index=False)

    click_paths = export.to_click_csvs(journeys, run_config.clicks_dir)
    tx = export.to_transaction_report(journeys, run_config.run_dir / "OCT26_Transaction Report.xlsx")
    perf = export.to_performance_report(
        journeys, run_config.run_dir / "OCT26_Performance-Report.xlsx", calibration.month_start
    )
    tabpfn_table = tabpfn_features.build_daily_segment_table(journeys, calibration)
    tabpfn_table.to_parquet(run_config.tabpfn_dir / "daily_segment_features.parquet", index=False)

    manifest = {
        "run_id": run_config.run_id,
        "seed": run_config.seed,
        "scale": run_config.scale.scale,
        "days": run_config.scale.days,
        "anomalies": run_config.anomalies,
        "n_clicks": len(journeys),
        "n_conversions": int(journeys["converts"].sum()),
        "n_click_files": len(click_paths),
        "n_timeline_events": len(timeline),
    }
    with open(run_config.run_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote outputs to {run_config.run_dir}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    paths = PathsConfig(data_dir=Path(args.data_dir), out_dir=Path(args.out_dir))
    run_dir = paths.run_dir(args.run_id)
    calibration = _load_or_compute_calibration(paths)

    journeys = pd.read_parquet(run_dir / "raw" / "journeys.parquet")
    funnel_df = pd.read_parquet(run_dir / "raw" / "funnel_events.parquet")
    synthetic_tx = pd.ExcelFile(run_dir / "OCT26_Transaction Report.xlsx").parse("Sheet")
    synthetic_perf = pd.ExcelFile(run_dir / "OCT26_Performance-Report.xlsx").parse("Sheet")
    labels = pd.read_csv(run_dir / "labels" / "anomalies.csv")

    real_clicks = real_data.load_clicks(paths.data_dir)
    real_tx = real_data.load_transactions(paths.data_dir)
    real_perf = real_data.load_performance(paths.data_dir)

    excluded = {
        (row["game_placement_id"], row["country"]) for _, row in labels.iterrows()
    } if len(labels) else set()

    report = validate.compare_to_real(
        real_clicks, real_tx, real_perf,
        journeys, synthetic_tx, synthetic_perf, funnel_df,
        calibration, RunConfig(run_id=args.run_id).tolerances,
        excluded_segments=excluded,
    )
    validate.write_report(report, run_dir)
    print(f"Validation {'PASSED' if report['passed'] else 'FAILED'} — see {run_dir / 'validation_report.md'}")
    return 0 if report["passed"] else 1


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_calibrate(args)
    if rc != 0:
        return rc
    rc = cmd_generate(args)
    if rc != 0:
        return rc
    return cmd_validate(args)


def cmd_build_timeline(args: argparse.Namespace) -> int:
    paths = PathsConfig(data_dir=Path(args.data_dir), out_dir=Path(args.out_dir))
    run_config = RunConfig(run_id=args.run_id, paths=paths)
    journeys = pd.read_parquet(run_config.raw_dir / "journeys.parquet")
    timeline = event_timeline.build_event_timeline(journeys)
    out_path = run_config.raw_dir / "event_timeline.parquet"
    timeline.to_parquet(out_path, index=False)
    print(f"Wrote {len(timeline)} events ({int((timeline['kind'] == 'click').sum())} click, "
          f"{int((timeline['kind'] == 'install').sum())} install) to {out_path}")
    return 0


def cmd_push_to_backend(args: argparse.Namespace) -> int:
    paths = PathsConfig(data_dir=Path(args.data_dir), out_dir=Path(args.out_dir))
    run_dir = paths.run_dir(args.run_id)
    print(f"Pushing {run_dir} to {args.backend_url} as dataset_id={args.dataset_id!r} "
          f"(synthetic demo data, naive placeholder reference baseline — not a real forecast)...")
    if args.ingestion_mode == "live_events":
        result = backend_adapter.register_live_run(
            run_dir, args.backend_url, args.dataset_id,
            create_run=not args.no_create_run, start=args.start,
        )
        print(f"Registered live event timeline at {result['event_source_path']}, "
              f"{result['references']} naive reference intervals.")
    else:
        result = backend_adapter.push_run_to_backend(
            run_dir, args.backend_url, args.dataset_id,
            include_non_converting_sample=args.sample_clicks, seed=args.seed,
            create_run=not args.no_create_run, start=args.start,
        )
        print(f"Pushed {result['daily_amounts']} daily amounts, {result['source_events']} source "
              f"events, {result['references']} naive reference intervals.")
    if "run_id" in result:
        print(f"Created run {result['run_id']}" + (" and sent 'start'." if result.get("started") else
              " — send a 'start' command (or use --start next time) to begin playback."))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cheetah-synth")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--data-dir", default="data")
        p.add_argument("--out-dir", default="data/synthetic")

    p_cal = sub.add_parser("calibrate")
    add_common(p_cal)
    p_cal.add_argument("--force", action="store_true")
    p_cal.set_defaults(func=cmd_calibrate)

    p_gen = sub.add_parser("generate")
    add_common(p_gen)
    p_gen.add_argument("--run-id", required=True)
    p_gen.add_argument("--scale", type=float, default=1.0)
    p_gen.add_argument("--days", type=int, default=31)
    p_gen.add_argument("--seed", type=int, default=42)
    p_gen.add_argument("--anomalies", choices=["none", "default"], default="default")
    p_gen.set_defaults(func=cmd_generate)

    p_val = sub.add_parser("validate")
    add_common(p_val)
    p_val.add_argument("--run-id", required=True)
    p_val.set_defaults(func=cmd_validate)

    p_all = sub.add_parser("all")
    add_common(p_all)
    p_all.add_argument("--run-id", required=True)
    p_all.add_argument("--scale", type=float, default=1.0)
    p_all.add_argument("--days", type=int, default=31)
    p_all.add_argument("--seed", type=int, default=42)
    p_all.add_argument("--anomalies", choices=["none", "default"], default="default")
    p_all.add_argument("--force", action="store_true")
    p_all.set_defaults(func=cmd_all)

    p_timeline = sub.add_parser("build-timeline")
    add_common(p_timeline)
    p_timeline.add_argument("--run-id", required=True)
    p_timeline.set_defaults(func=cmd_build_timeline)

    p_push = sub.add_parser("push-to-backend")
    add_common(p_push)
    p_push.add_argument("--run-id", required=True)
    p_push.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p_push.add_argument("--dataset-id", required=True)
    p_push.add_argument("--sample-clicks", type=int, default=0,
                         help="also push this many randomly-sampled non-converting clicks per run, for volume texture (preloaded_daily mode only)")
    p_push.add_argument("--seed", type=int, default=0)
    p_push.add_argument("--no-create-run", action="store_true")
    p_push.add_argument("--start", action="store_true")
    p_push.add_argument("--ingestion-mode", choices=["preloaded_daily", "live_events"], default="preloaded_daily",
                         help="preloaded_daily: bulk-POST everything up front (today's demo path). "
                              "live_events: register the run's event_timeline.parquet and let the backend "
                              "worker stream it in live, per event, as the run plays.")
    p_push.set_defaults(func=cmd_push_to_backend)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
