"""One pass over the Freecash graph: outage -> pacing watcher -> investigator -> graph overlooker.

    .venv/bin/python run_agents.py                        # full pass
    .venv/bin/python run_agents.py --target Ad:0003WS     # also investigate specific nodes
    .venv/bin/python run_agents.py --skip-outage --target Category:Fashion\\ \\&\\ Apparel
    .venv/bin/python run_agents.py --skip-outage --now 2026-10-14T12:00   # pacing watcher clock

Run standalone, each agent starts the overlooker itself when it finishes. In this pipeline the
agents hand their changes on, and the overlooker runs once at the end over all of them, so it
sees the whole picture and isn't started twice.
"""
from __future__ import annotations

import argparse

from agents.graph_overlooker import GraphOverlooker, format_report
from agents.investigator_agent import InvestigatorAgent, summary as inv_summary
from agents.outage_agent import OutageAgent, summary as outage_summary
from agents.pacing_watcher import PacingWatcher, _parse, format_report as pacing_report
from llm import LLMUnavailable


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", action="append", default=[], help="Label:id to investigate (repeatable)")
    ap.add_argument("--skip-outage", action="store_true")
    ap.add_argument("--max-flagged", type=int, default=3, help="flagged countries/ads to investigate")
    ap.add_argument("--now", default="2026-10-14T12:00", help="simulated clock for the pacing watcher")
    ap.add_argument("--skip-pacing", action="store_true")
    args = ap.parse_args()

    focus: list[str] = []
    targets = [tuple(t.split(":", 1)) for t in args.target]

    if not args.skip_outage:
        outage = OutageAgent(run_overlooker=False)
        impacts = outage.run()
        print("== Outage agent\n" + outage_summary(impacts))
        for u in outage.uncertainties:
            print("  ?", u)
        focus += outage.store.changes
        for i in impacts:
            if i.flagged and i.outage_id:
                targets.append(("Outage", i.outage_id))
                top = sorted(i.ads.items(), key=lambda kv: -kv[1])[: args.max_flagged]
                targets += [("Ad", ad) for ad, _ in top]

    if not args.skip_pacing:
        watcher = PacingWatcher()
        cycle = watcher.run(_parse(args.now))
        print("\n" + pacing_report(cycle))
        focus += watcher.store.changes
        for h in cycle.handoff():
            targets += [t for t in h["targets"] if t[0] == "Ad"]

    if targets:
        inv = InvestigatorAgent(run_overlooker=False)
        results = inv.run(list(dict.fromkeys(targets)))
        print("\n== Investigator\n" + inv_summary(results, inv.store))
        focus += inv.store.changes

    print("\n== Graph overlooker")
    try:
        report = GraphOverlooker().run(focus=focus, trigger="run_agents")
        print(format_report(report))
    except LLMUnavailable as e:
        print(f"not run: {e}")


if __name__ == "__main__":
    main()
