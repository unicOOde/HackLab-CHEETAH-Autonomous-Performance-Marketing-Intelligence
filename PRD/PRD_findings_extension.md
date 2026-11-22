# PRD Findings & Extensions

Supplementary findings and decisions reached after `Master_PDR_v2.0`, scoped to this repository's
current implementation state. Does not modify or supersede the master PDR — read alongside it.

## 2026-11-22 — Synthetic data as the official Track A demo dataset

**Decision:** For Track A's simulation/forecast/investigation loop, the existing
`cheetah_synth` synthetic journey dataset is adopted as the **official** demo dataset, rather than
building a separate real-October-2026 ingestion path. Track B's Neo4j graph build
(`freecash_graph.py`) is unaffected and continues to be built from the real CSVs/xlsx in `data/`.

**Rationale:**
- Master PDR §12 (current implementation audit) phrases the open requirement as "**Real
  historical/synthetic** October dataset and proven source coverage" — synthetic was always an
  accepted route, not a fallback.
- §13.2 (open engineering/product inputs) lists the blocking item as "pending full month source
  **or synthetic generation** with 2 negative/1 positive injected event definitions."
- The hackathon brief (`DESCRIPTION.md`) states no dataset is provided and explicitly invites a
  simulator ("create your own mock data or simulator... choose a manageable scope"), and the
  20%-weighted demo criterion requires injecting "a fault and an innocent change" — only possible
  with a simulator where ground truth is controlled, which real October data cannot provide (no
  injectable faults, no ground truth, conversions lag clicks by up to 7+ days making recent days
  incomplete, no prior month for a baseline).
- `cheetah_synth` is already built, calibrated against the real reports, validated, and wired
  end-to-end into the backend via `backend_adapter.py` / `push-to-backend`.

**Scope boundary:** real data stays authoritative for Track B's graph structure (business
relationships, aggregate totals — no anomalies needed there). Synthetic data is authoritative for
Track A's daily revenue simulation, forecast references, and the anomalies that drive
investigation. `source_kind="synthetic"` tagging (already implemented) must remain honest and
visible everywhere this data surfaces, per CLAUDE.md's non-fabrication rule — "official demo
dataset" does not mean presenting it as real.

### Anomaly catalog confirmed against CHEETAH's four target anomaly types

`src/cheetah_synth/anomalies.py` already implements exactly four anomaly types, one-to-one with
the agreed set:

| # | Required anomaly | Implementation | Mechanism |
|---|---|---|---|
| 1 | Click-volume spikes / click-farm bursts, often with abnormal (too-regular) timing | `traffic_burst` (`_apply_traffic_burst`) | Injects extra clicks for a segment-day scaled by `severity`; `params={"regular_timing": True}` spaces them at perfectly even microsecond intervals instead of the normal empirical hour-of-day sampling |
| 2 | Conversion-rate shocks (collapse or spike) | `cvr_shock` (`_apply_cvr_shock`) | Overrides the segment's calibrated CVR as `base_cvr * severity` (clipped to [0,1]); re-draws the conversion decision per row, materializing or clearing conversions — bidirectional |
| 3 | Payout/commission/margin shocks | `payout_shock` (`_apply_payout_shock`) | Scales `amount/commission/payout/margin` by `severity` for converting rows in the window, clipped at 0 — bidirectional |
| 4 | Conversion-latency blowouts (abnormally long or short vs. calibrated distribution) | `latency_blowout` (`_apply_latency_blowout`) | Scales `duration_seconds` by `severity`, shifts conversion time and funnel-stage timestamps proportionally, marks `censored` if pushed past Oct 31 — bidirectional |

`build_default_catalog()` already instantiates one of each against real calibrated segments, each
in its own 3-day window across October.

### Open follow-up

The backend's naive forecast-reference band (`cheetah_synth.backend_adapter.build_naive_references`)
is currently tuned to surface exactly **one** breach (Oct 7), not the PRD §13.2-required 2 negative
+ 1 positive. Still to decide: which 3 of the 4 anomaly types above (plus one genuine "innocent
change" carrying no `anomaly_id`) the band should be tuned to catch for the demo.

## 2026-11-22 — Track B's Neo4j graph stays on real data, not synthetic

**Decision:** Track B's graph build (`freecash_graph.py`) continues to ingest the real CSVs/xlsx
in `data/` and is **not** fed the synthetic dataset, even though `cheetah_synth`'s exports are
schema-identical to the real files (same column names/dtypes, built for ETL testing) and could
likely be ingested with no code changes.

**Rationale:**
- **No time axis yet.** Track B's graph currently only has monthly aggregate edges — the planned
  `(:Day)` nodes and per-day aggregate edges (CLAUDE.md, "Planned extensions") don't exist. The
  four injected anomalies are 3-day windows; without per-day edges, a 3-day shock dissolves into
  the monthly total and becomes statistically invisible. Track B's agents could not see or
  investigate what Track A's day-level deviation detector is built to catch, regardless of data
  source.
- **Judging weight on authentic structure.** The metric graph is 40% of the score, and Track B is
  explicitly scoped (CLAUDE.md) as built from the real data — its value is demonstrating the
  system understands the *actual* marketplace, not a calibrated reconstruction of it.
- **No provenance tagging mechanism.** `graph_store.py`'s schema has no `source_kind`/provenance
  field on aggregate edges the way Track A's DB has `source_kind="synthetic"` on `DailyAmount`
  rows — there would be no honest way to mark a synthetic edge total as such if one were mixed in,
  which runs against CLAUDE.md's non-fabrication rule for this track.

**Conclusion:** this is a scope decision reinforced by one real gap (missing Day nodes), not a hard
engineering wall. If Track B should eventually react to the same injected anomalies, the Day-node
extension needs to land first — then feeding it synthetic data becomes a live option rather than a
blocked one.
