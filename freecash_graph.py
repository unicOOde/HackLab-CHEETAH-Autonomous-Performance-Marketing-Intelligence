"""Build the Freecash entity graph from the OCT26 CSVs.

Raw rows are never loaded as nodes. The transaction report is streamed once and
summed into (ad x entity) edges, so graph size depends on #ads x #buckets, not on
#rows: millions of transactions still give a few hundred nodes.

Output: Cypher with UNWIND batches (idempotent MERGE).

    python3 freecash_graph.py > freecash_graph.cypher
    cypher-shell -f freecash_graph.cypher
"""
import csv
import json
import sys
from collections import defaultdict

TX_CSV = "OCT26_Transaction Report.xlsx - Sheet.csv"
PERF_CSV = "OCT26_Performance-Report.xlsx - Sheet.csv"
BATCH = 1000
MONEY = ("amount", "commission", "payout", "margin")
LAG_BUCKETS = [(60, "<1m"), (3600, "<1h"), (86400, "<1d"), (604800, "<7d")]


def num(s):
    return float(s.replace(",", "")) if s else 0.0


def lag_bucket(seconds):
    for limit, name in LAG_BUCKETS:
        if seconds < limit:
            return name
    return ">7d"


def new_agg():
    return {"tx": 0, **{m: 0.0 for m in MONEY}}


def add(agg, row):
    agg["tx"] += 1
    for m in MONEY:
        agg[m] += row[m]


def aggregate(tx_path):
    """One streaming pass; memory is O(#edges), independent of row count."""
    edges = {k: defaultdict(new_agg) for k in ("RUNS", "HAS_TYPE", "CONVERTS_IN", "CLICKED_AT", "CONVERTS_AFTER")}
    perf_key = defaultdict(new_agg)  # placement x country x type, only for the check
    with open(tx_path, newline="") as f:
        for r in csv.DictReader(f):
            ad = r["Game Placement ID"]
            row = {"amount": num(r["Amount"]), "commission": num(r["Commission"]),
                   "payout": num(r["Payout"]), "margin": num(r["Margin"])}
            add(perf_key[(ad, r["Country"], r["Transaction Type"])], row)
            add(edges["RUNS"][ad], row)
            add(edges["HAS_TYPE"][(ad, r["Transaction Type"])], row)
            add(edges["CONVERTS_IN"][(ad, r["Country"])], row)
            add(edges["CLICKED_AT"][(ad, int(r["Click Time"][11:13]))], row)
            add(edges["CONVERTS_AFTER"][(ad, lag_bucket(num(r["Click to Conversion Duration"])))], row)
    return edges, perf_key


def ad_labels(perf_path):
    labels = {}
    with open(perf_path, newline="") as f:
        for r in csv.DictReader(f):
            labels[r["Game Placement ID"]] = {"vertical": r["Vertical Name"], "category": r["Category Name"],
                                              "currency": r["Currency"], "month": r["Month"][:7],
                                              "publisher": r["Freecash"]}
    return labels


def rounded(agg):
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in agg.items()}


def lit(v):
    """Python value -> Cypher literal (Cypher maps take unquoted keys, not JSON)."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {lit(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(lit(x) for x in v) + "]"
    return json.dumps(v)


def unwind(rows, body):
    for i in range(0, len(rows), BATCH):
        print(f"UNWIND {lit(rows[i:i + BATCH])} AS r\n{body};\n")


def emit(edges, labels):
    pubs = {l["publisher"] for l in labels.values()}
    print("CREATE CONSTRAINT IF NOT EXISTS FOR (n:Freecash) REQUIRE n.id IS UNIQUE;")
    for label in ("Ad", "TransactionType", "Country", "ClickHour", "LagBucket",
                  "Vertical", "Category", "Currency", "Month"):
        print(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE;")
    print()

    unwind([{"id": p} for p in sorted(pubs)], "MERGE (:Freecash {id: r.id})")
    unwind([{"id": ad, **l, "agg": rounded(edges["RUNS"][ad])} for ad, l in sorted(labels.items())],
           "MATCH (f:Freecash {id: r.publisher})\n"
           "MERGE (a:Ad {id: r.id})\n"
           "MERGE (f)-[e:RUNS]->(a) SET e += r.agg\n"
           "MERGE (v:Vertical {id: r.vertical}) MERGE (a)-[:IN_VERTICAL]->(v)\n"
           "MERGE (c:Category {id: r.category}) MERGE (a)-[:IN_CATEGORY]->(c)\n"
           "MERGE (cur:Currency {id: r.currency}) MERGE (a)-[:PAID_IN]->(cur)\n"
           "MERGE (m:Month {id: r.month}) MERGE (a)-[am:ACTIVE_IN]->(m) SET am += r.agg")

    targets = {"HAS_TYPE": "TransactionType", "CONVERTS_IN": "Country",
               "CLICKED_AT": "ClickHour", "CONVERTS_AFTER": "LagBucket"}
    for rel, label in targets.items():
        rows = [{"ad": ad, "to": to, "agg": rounded(agg)} for (ad, to), agg in sorted(edges[rel].items())]
        unwind(rows, f"MATCH (a:Ad {{id: r.ad}})\n"
                     f"MERGE (t:{label} {{id: r.to}})\n"
                     f"MERGE (a)-[e:{rel}]->(t) SET e += r.agg")


def check_against_performance(perf_key, perf_path):
    """Performance report must equal tx summed per placement x country x type."""
    bad = 0
    with open(perf_path, newline="") as f:
        for r in csv.DictReader(f):
            agg = perf_key.get((r["Game Placement ID"], r["Country"], r["Transaction Type"]), new_agg())
            if agg["tx"] != int(r["Transaction Count"]) or abs(agg["commission"] - num(r["Commission"])) > 0.05:
                bad += 1
    print(f"// check vs performance report: {bad} mismatching rows", file=sys.stderr)


if __name__ == "__main__":
    edges, perf_key = aggregate(TX_CSV)
    labels = ad_labels(PERF_CSV)
    check_against_performance(perf_key, PERF_CSV)
    emit(edges, labels)
    n_edges = sum(len(v) for v in edges.values()) + 4 * len(labels)  # + vertical/category/currency/month
    print(f"// {len(labels)} ads, {n_edges} edges", file=sys.stderr)
