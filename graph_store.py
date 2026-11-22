"""Single write path for agents into the Freecash graph.

Rules enforced here (agents never write Cypher themselves):
  * Base nodes (Ad, Country, ...): only properties prefixed `ag_`. The build only MERGEs nodes
    and SETs edge totals, so `ag_*` survives a rebuild.
  * Aggregate edges (RUNS, CONVERTS_IN, ...): totals are facts from the data and stay read-only.
    Agents may set `ag_*` on them (e.g. ag_weight); "removing" one sets ag_disabled = true.
  * Agent edges (any other relationship type): free to add / reweight / delete.
  * Agent nodes (Outage, Hypothesis, Signal): free to create / update / change status / delete.
  * Every write creates a (:Change) node -> audit trail, `undo(change_id)`.

Backend: Neo4j Python driver if NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD are set (env or .env),
otherwise the `neo4j-cli` binary with a stored credential (default "test").
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE_LABELS = {"Freecash", "Ad", "Country", "Category", "Vertical", "ClickHour",
               "LagBucket", "TransactionType", "Currency", "Month",
               "Metric", "Day", "Hour", "PlannedEvent"}          # paid_sim.py / paid_metrics.py
AGENT_LABELS = {"Outage", "Hypothesis", "Signal"}
ALL_LABELS = BASE_LABELS | AGENT_LABELS
AGGREGATE_RELS = {"RUNS", "HAS_TYPE", "CONVERTS_IN", "CLICKED_AT", "CONVERTS_AFTER",
                  "IN_VERTICAL", "IN_CATEGORY", "PAID_IN", "ACTIVE_IN",
                  "PAID_IN_HOUR", "IN_DAY", "FORMULA", "INFLUENCES", "APPLIES_TO"}
RESERVED_RELS = {"CHANGED"}
RESERVED_EDGE_PROPS = {"agent", "created"}
STATUSES = {"open", "resolved", "confirmed", "rejected"}
PROP_PREFIX = "ag_"
METRIC_UNITS = {"ratio", "count", "EUR", "seconds", "x"}
CLI_RETRIES = 10   # neo4j-cli opens a new connection per call; Aura TLS setup fails intermittently

NodeRef = Tuple[str, Any]   # (label, id)

_WRITE_CYPHER = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL\s*\{|IN\s+TRANSACTIONS)\b"
    r"|\bCALL\s+(apoc\.(create|merge|refactor|periodic|do|nodes\.delete)|db\.create|dbms\.)",
    re.IGNORECASE)


class GraphWriteError(ValueError):
    pass


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _label(label: str, allowed=ALL_LABELS) -> str:
    if label not in allowed:
        raise GraphWriteError(f"label {label!r} not allowed (allowed: {sorted(allowed)})")
    return label


def _rel(rel: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", rel or "") or rel in RESERVED_RELS:
        raise GraphWriteError(f"bad relationship type {rel!r} (UPPER_SNAKE_CASE, not {sorted(RESERVED_RELS)})")
    return rel


def _check_values(props: Dict[str, Any]) -> None:
    for k, v in props.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise GraphWriteError(f"bad property name {k!r}")
        if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v)):
            raise GraphWriteError(f"property {k}: Neo4j properties can't be maps/nested lists")


def _node_id(label: str, raw: Any) -> Any:
    """ClickHour ids are ints; everything else is a string."""
    return int(raw) if label == "ClickHour" else str(raw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_read_only(cypher: str) -> bool:
    return not _WRITE_CYPHER.search(cypher)


class GraphStore:
    def __init__(self, agent: str, credential: str = "test"):
        _load_dotenv()
        self.agent = agent
        self.credential = credential
        self.changes: List[str] = []   # Change ids written by this instance
        self._driver = None
        if os.getenv("NEO4J_URI") and os.getenv("NEO4J_PASSWORD"):
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                os.environ["NEO4J_URI"],
                auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.environ["NEO4J_PASSWORD"]),
            )
            self._db = os.getenv("NEO4J_DATABASE") or None

    # ------------------------------------------------------------------ backend
    def _run(self, cypher: str, params: Dict[str, Any], write: bool) -> List[Dict[str, Any]]:
        if self._driver is not None:
            records, _, _ = self._driver.execute_query(
                cypher, params, database_=self._db, routing_="w" if write else "r")
            return [r.data() for r in records]
        # the CLI truncates to 100 rows / 100-item arrays by default; agents need everything
        cmd = ["neo4j-cli", "query", "--credential", self.credential, "--format", "json",
               "--max-rows", "0", "--truncate-arrays-over", "0"]
        if write:
            cmd.append("--rw")   # without it, neo4j-cli blocks writes via an EXPLAIN preflight
        for k, v in params.items():
            cmd += ["--param", f"{k}={json.dumps(v)}"]
        cmd.append(cypher)
        for attempt in range(CLI_RETRIES):
            out = subprocess.run(cmd, capture_output=True, text=True)
            # Aura sometimes drops the connection before the query is sent (routing table): safe to retry
            if out.returncode == 0 or "ConnectivityError" not in (out.stderr + out.stdout):
                break
            time.sleep(min(0.5 * (attempt + 1), 3))
        if out.returncode != 0:
            raise RuntimeError(f"neo4j-cli failed: {out.stderr.strip() or out.stdout.strip()}")
        return json.loads(out.stdout).get("rows", [])

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    # ------------------------------------------------------------------ reads
    def read(self, cypher: str, **params) -> List[Dict[str, Any]]:
        """Read-only query. Return scalars/maps (e.g. properties(n)), not node objects."""
        if not is_read_only(cypher):
            raise GraphWriteError("read() only accepts read-only Cypher")
        return self._run(cypher, params, write=False)

    def get(self, label: str, node_id: Any) -> Optional[Dict[str, Any]]:
        rows = self.read(f"MATCH (n:{_label(label)} {{id: $id}}) RETURN properties(n) AS p",
                         id=_node_id(label, node_id))
        return rows[0]["p"] if rows else None

    def exists(self, label: str, node_id: Any) -> bool:
        return label in ALL_LABELS and self.get(label, node_id) is not None

    def _edge_q(self, src: NodeRef, rel: str, dst: NodeRef) -> Tuple[str, Dict[str, Any]]:
        q = (f"MATCH (a:{_label(src[0])} {{id: $a}})-[r:{_rel(rel)}]->(b:{_label(dst[0])} {{id: $b}})")
        return q, {"a": _node_id(*src), "b": _node_id(*dst)}

    def get_edge(self, src: NodeRef, rel: str, dst: NodeRef) -> Optional[Dict[str, Any]]:
        q, p = self._edge_q(src, rel, dst)
        rows = self.read(q + " RETURN properties(r) AS p LIMIT 1", **p)
        return rows[0]["p"] if rows else None

    # ------------------------------------------------------------------ change log
    def _change(self, op: str, target_label: str, target_id: Any, before: Any, after: Any,
                reason: str) -> Dict[str, Any]:
        chg = {"id": f"chg_{uuid.uuid4().hex[:12]}", "agent": self.agent, "ts": _now(), "op": op,
               "target_label": target_label, "target_id": json.dumps(target_id, default=str),
               "before": json.dumps(before, default=str), "after": json.dumps(after, default=str),
               "reason": reason, "undone": False}
        self.changes.append(chg["id"])
        return chg

    _LOG_NODE = "CREATE (c:Change) SET c = $chg CREATE (c)-[:CHANGED]->(n)"
    _LOG_EDGE = "CREATE (c:Change) SET c = $chg CREATE (c)-[:CHANGED]->(a) CREATE (c)-[:CHANGED]->(b)"

    # ------------------------------------------------------------------ node writes
    def set_props(self, label: str, node_id: Any, props: Dict[str, Any], reason: str = "") -> str:
        """Set `ag_*` properties on an existing node. Value None removes the property."""
        _label(label)
        if not props:
            raise GraphWriteError("no properties given")
        bad = [k for k in props if not k.startswith(PROP_PREFIX)]
        if bad:
            raise GraphWriteError(f"only '{PROP_PREFIX}*' properties may be written on nodes, got {bad}")
        _check_values(props)
        current = self.get(label, node_id)
        if current is None:
            raise GraphWriteError(f"{label} {node_id!r} does not exist")
        before = {k: current.get(k) for k in props}
        if before == props:
            return ""
        chg = self._change("set_props", label, node_id, before, props, reason)
        self._run(f"MATCH (n:{label} {{id: $id}}) SET n += $props " + self._LOG_NODE,
                  {"id": _node_id(label, node_id), "props": props, "chg": chg}, write=True)
        return chg["id"]

    def _create(self, label: str, props: Dict[str, Any], links: Iterable[Tuple[str, str, Any]],
                reason: str, node_id: Optional[str] = None) -> Tuple[str, str]:
        """Create an agent node + outgoing relationships to existing nodes, plus its Change."""
        _label(label, AGENT_LABELS)
        _check_values(props)
        links = list(links)
        for rel, tlabel, tid in links:
            _rel(rel)
            if not self.exists(tlabel, tid):
                raise GraphWriteError(f"link target {tlabel} {tid!r} does not exist")
        node_id = node_id or f"{label.lower()}_{uuid.uuid4().hex[:12]}"
        props = {"agent": self.agent, "created": _now(), **props, "id": node_id}
        chg = self._change("create", label, node_id, None, props, reason)
        lines, params = [], {"props": props, "chg": chg}
        for i, (rel, tlabel, tid) in enumerate(links):
            lines.append(f"MATCH (t{i}:{tlabel} {{id: $t{i}}})")
            params[f"t{i}"] = _node_id(tlabel, tid)
        lines.append(f"CREATE (n:{label}) SET n = $props")
        lines += [f"CREATE (n)-[:{rel}]->(t{i})" for i, (rel, _, _) in enumerate(links)]
        lines.append(self._LOG_NODE)
        self._run("\n".join(lines), params, write=True)
        return node_id, chg["id"]

    def add_outage(self, service: str, category: str, severity: str, confidence: float,
                   countries: List[str], sources: List[str], summary: str,
                   started_at: Optional[str] = None) -> str:
        if not 0.0 <= confidence <= 1.0:
            raise GraphWriteError("confidence must be in [0,1]")
        countries = [c for c in countries if self.exists("Country", c)]
        if not countries:
            raise GraphWriteError("outage affects none of the countries in the graph")
        node_id, _ = self._create(
            "Outage",
            {"service": service, "category": category, "severity": severity, "confidence": confidence,
             "sources": sources, "summary": summary, "started_at": started_at or "", "status": "open"},
            [("AFFECTS", "Country", c) for c in countries],
            reason=f"{service} {severity} in {','.join(countries)}")
        return node_id

    def add_hypothesis(self, text: str, confidence: float, evidence: List[str],
                       about: List[NodeRef]) -> str:
        if not 0.0 <= confidence <= 1.0:
            raise GraphWriteError("confidence must be in [0,1]")
        if not about:
            raise GraphWriteError("hypothesis must be ABOUT at least one node")
        node_id, _ = self._create(
            "Hypothesis",
            {"text": text, "confidence": confidence, "evidence": evidence, "status": "open"},
            [("ABOUT", lbl, nid) for lbl, nid in about],
            reason=text[:120])
        return node_id

    def add_signal(self, props: Dict[str, Any], links: List[Tuple[str, str, Any]]) -> str:
        """Monitoring signal; `props` must carry issue_key, metric, severity, state."""
        missing = {"issue_key", "metric", "severity", "state"} - set(props)
        if missing:
            raise GraphWriteError(f"signal needs {sorted(missing)}")
        node_id, _ = self._create("Signal", {**props, "status": "open"}, links,
                                  reason=f"{props['issue_key']} {props['severity']}/{props['state']}")
        return node_id

    def update_agent_node(self, label: str, node_id: str, props: Dict[str, Any], reason: str = "") -> str:
        """Change properties of an agent node (not id/agent/created/status; use set_status for that)."""
        _label(label, AGENT_LABELS)
        bad = set(props) & {"id", "agent", "created", "status"}
        if bad:
            raise GraphWriteError(f"{sorted(bad)} can't be changed here")
        _check_values(props)
        current = self.get(label, node_id)
        if current is None:
            raise GraphWriteError(f"{label} {node_id!r} does not exist")
        before = {k: current.get(k) for k in props}
        if before == props:
            return ""
        chg = self._change("set_props", label, node_id, before, props, reason)   # undo: SET n += before
        self._run(f"MATCH (n:{label} {{id: $id}}) SET n += $props " + self._LOG_NODE,
                  {"id": node_id, "props": props, "chg": chg}, write=True)
        return chg["id"]

    def set_status(self, label: str, node_id: str, status: str, reason: str = "") -> str:
        _label(label, AGENT_LABELS)
        if status not in STATUSES:
            raise GraphWriteError(f"status must be one of {sorted(STATUSES)}")
        current = self.get(label, node_id)
        if current is None:
            raise GraphWriteError(f"{label} {node_id!r} does not exist")
        if current.get("status") == status:
            return ""
        chg = self._change("set_status", label, node_id, {"status": current.get("status")},
                           {"status": status}, reason)
        self._run(f"MATCH (n:{label} {{id: $id}}) SET n.status = $status " + self._LOG_NODE,
                  {"id": node_id, "status": status, "chg": chg}, write=True)
        return chg["id"]

    def delete_node(self, label: str, node_id: str, reason: str = "") -> str:
        """Delete an agent node (Outage/Hypothesis). Its props + links are kept in the Change for undo."""
        _label(label, AGENT_LABELS)
        current = self.get(label, node_id)
        if current is None:
            raise GraphWriteError(f"{label} {node_id!r} does not exist")
        links = self.read(
            f"MATCH (n:{label} {{id: $id}})-[r]->(t) WHERE NOT t:Change "
            "RETURN type(r) AS rel, labels(t)[0] AS label, t.id AS id", id=node_id)
        before = {"props": current, "links": links}
        chg = self._change("delete_node", label, node_id, before, None, reason)
        # Change can't point at a deleted node: log first, then delete
        self._run(f"MATCH (n:{label} {{id: $id}}) CREATE (c:Change) SET c = $chg "
                  "WITH n DETACH DELETE n", {"id": node_id, "chg": chg}, write=True)
        return chg["id"]

    # ------------------------------------------------------------------ edge writes
    def add_edge(self, src: NodeRef, rel: str, dst: NodeRef, weight: Optional[float] = None,
                 props: Optional[Dict[str, Any]] = None, reason: str = "") -> str:
        """New agent relationship between two existing nodes (one per src/type/dst)."""
        if _rel(rel) in AGGREGATE_RELS:
            raise GraphWriteError(f"{rel} is a data edge owned by the build; it can't be added by agents")
        props = dict(props or {})
        if set(props) & RESERVED_EDGE_PROPS:
            raise GraphWriteError(f"{sorted(RESERVED_EDGE_PROPS)} are set by the store")
        if weight is not None:
            if not -1.0 <= float(weight) <= 1.0:
                raise GraphWriteError("weight must be in [-1, 1]")
            props["weight"] = float(weight)
        _check_values(props)
        for ref in (src, dst):
            if not self.exists(*ref):
                raise GraphWriteError(f"{ref[0]} {ref[1]!r} does not exist")
        if self.get_edge(src, rel, dst) is not None:
            raise GraphWriteError(f"edge already exists; reweight or change it instead")
        props = {**props, "agent": self.agent, "created": _now(), "reason": reason}
        chg = self._change("add_edge", rel, [src[0], src[1], dst[0], dst[1]], None, props, reason)
        self._run(f"MATCH (a:{src[0]} {{id: $a}}), (b:{dst[0]} {{id: $b}}) "
                  f"CREATE (a)-[r:{rel}]->(b) SET r = $props " + self._LOG_EDGE,
                  {"a": _node_id(*src), "b": _node_id(*dst), "props": props, "chg": chg}, write=True)
        return chg["id"]

    def set_edge_props(self, src: NodeRef, rel: str, dst: NodeRef, props: Dict[str, Any],
                       reason: str = "") -> str:
        """Change edge properties. Data edges: only ag_* keys. Agent edges: anything but agent/created."""
        current = self.get_edge(src, rel, dst)
        if current is None:
            raise GraphWriteError(f"edge ({src})-[:{rel}]->({dst}) does not exist")
        if rel in AGGREGATE_RELS:
            bad = [k for k in props if not k.startswith(PROP_PREFIX)]
            if bad:
                raise GraphWriteError(f"data edge {rel}: totals are read-only, only ag_* allowed, got {bad}")
        elif set(props) & RESERVED_EDGE_PROPS:
            raise GraphWriteError(f"{sorted(RESERVED_EDGE_PROPS)} can't be changed")
        for k in ("weight", "ag_weight"):
            if props.get(k) is not None and not -1.0 <= float(props[k]) <= 1.0:
                raise GraphWriteError(f"{k} must be in [-1, 1]")
        _check_values(props)
        before = {k: current.get(k) for k in props}
        if before == props:
            return ""
        chg = self._change("set_edge_props", rel, [src[0], src[1], dst[0], dst[1]], before, props, reason)
        q, p = self._edge_q(src, rel, dst)
        self._run(q + " SET r += $props " + self._LOG_EDGE, {**p, "props": props, "chg": chg}, write=True)
        return chg["id"]

    def reweight_edge(self, src: NodeRef, rel: str, dst: NodeRef, weight: float, reason: str = "") -> str:
        key = "ag_weight" if rel in AGGREGATE_RELS else "weight"
        return self.set_edge_props(src, rel, dst, {key: float(weight)}, reason)

    def remove_edge(self, src: NodeRef, rel: str, dst: NodeRef, reason: str = "") -> str:
        """Agent edge: deleted. Data edge: soft-disabled (ag_disabled = true), totals stay."""
        if rel in AGGREGATE_RELS:
            return self.set_edge_props(src, rel, dst, {"ag_disabled": True, "ag_disabled_reason": reason},
                                       reason)
        current = self.get_edge(src, rel, dst)
        if current is None:
            raise GraphWriteError(f"edge ({src})-[:{rel}]->({dst}) does not exist")
        chg = self._change("remove_edge", rel, [src[0], src[1], dst[0], dst[1]], current, None, reason)
        q, p = self._edge_q(src, rel, dst)
        self._run(q + " DELETE r " + self._LOG_EDGE, {**p, "chg": chg}, write=True)
        return chg["id"]

    # ------------------------------------------------------------------ metric onboarding
    def add_metric(self, metric_id: str, definition: str, unit: str, cypher_value: str,
                   source_query: Optional[str], domain: str, config: Dict[str, Any], reason: str = "") -> str:
        """Create an onboarded Metric (origin='onboarding'). Build-owned metrics are never redefined.

        The caller (graph engineer) has already evaluated source_query/cypher_value; this only
        enforces the structural rules: unique id, read-only source_query, flat property values.
        """
        if not metric_id or not str(metric_id).strip():
            raise GraphWriteError("metric id is required")
        if unit not in METRIC_UNITS:
            raise GraphWriteError(f"unit must be one of {sorted(METRIC_UNITS)}")
        if source_query and not is_read_only(source_query):
            raise GraphWriteError("source_query must be read-only Cypher")
        if self.get("Metric", metric_id) is not None:
            raise GraphWriteError(f"Metric {metric_id!r} already exists; metrics are never redefined by agents")
        props = {**config, "definition": definition, "unit": unit, "cypher_value": cypher_value,
                 "domain": domain, "origin": "onboarding", "agent": self.agent, "created": _now(),
                 "id": metric_id}
        if source_query:
            props["source_query"] = source_query
        _check_values(props)
        chg = self._change("create", "Metric", metric_id, None, props, reason)
        self._run("CREATE (n:Metric) SET n = $props " + self._LOG_NODE, {"props": props, "chg": chg}, write=True)
        return chg["id"]

    def link_metrics(self, src_id: str, rel: str, dst_id: str, props: Dict[str, Any],
                     evidence: List[str], reason: str = "") -> str:
        """FORMULA / INFLUENCES edge between two existing metrics (no placeholders, no duplicates).

        FORMULA needs props.formula and props.residual <= 1e-6. INFLUENCES always starts as
        status='hypothesis'; promotion goes through set_edge_props({'ag_status': 'validated'}).
        """
        if rel not in ("FORMULA", "INFLUENCES"):
            raise GraphWriteError("rel must be FORMULA or INFLUENCES")
        if not evidence:
            raise GraphWriteError("a metric edge needs evidence")
        props = dict(props)
        if rel == "FORMULA":
            if not props.get("formula") or props.get("residual") is None or float(props["residual"]) > 1e-6:
                raise GraphWriteError("FORMULA needs a formula and a residual <= 1e-6")
            props.update(status="validated", weight=1.0)
        else:
            props["status"] = "hypothesis"
        for mid in (src_id, dst_id):
            if self.get("Metric", mid) is None:
                raise GraphWriteError(f"Metric {mid!r} does not exist")
        src, dst = ("Metric", src_id), ("Metric", dst_id)
        if self.get_edge(src, rel, dst) is not None:
            raise GraphWriteError("edge already exists; change it instead")
        props = {**props, "evidence": [str(e) for e in evidence], "origin": "onboarding",
                 "agent": self.agent, "created": _now(), "reason": reason}
        _check_values(props)
        chg = self._change("add_edge", rel, ["Metric", src_id, "Metric", dst_id], None, props, reason)
        self._run(f"MATCH (a:Metric {{id: $a}}), (b:Metric {{id: $b}}) CREATE (a)-[r:{rel}]->(b) SET r = $props "
                  + self._LOG_EDGE, {"a": src_id, "b": dst_id, "props": props, "chg": chg}, write=True)
        return chg["id"]

    # ------------------------------------------------------------------ undo
    def undo(self, change_id: str) -> None:
        rows = self.read("MATCH (c:Change {id: $id}) RETURN properties(c) AS c", id=change_id)
        if not rows:
            raise GraphWriteError(f"change {change_id} not found")
        c = rows[0]["c"]
        if c.get("undone"):
            return
        op, target = c["op"], json.loads(c["target_id"])
        before = json.loads(c["before"])
        mark = "WITH 1 AS _ MATCH (c:Change {id: $cid}) SET c.undone = true"
        params: Dict[str, Any] = {"cid": change_id}

        if op in ("set_props", "set_status", "create", "delete_node"):
            label = _label(c["target_label"])
            params["id"] = _node_id(label, target)
            if op == "create":
                cypher = f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n " + mark
            elif op == "delete_node":
                lines = [f"MATCH (t{i}:{_label(l['label'])} {{id: $t{i}}})" for i, l in enumerate(before["links"])]
                for i, l in enumerate(before["links"]):
                    params[f"t{i}"] = _node_id(l["label"], l["id"])
                lines.append(f"CREATE (n:{label}) SET n = $props")
                lines += [f"CREATE (n)-[:{_rel(l['rel'])}]->(t{i})" for i, l in enumerate(before["links"])]
                cypher = "\n".join(lines) + " " + mark
                params["props"] = before["props"]
            else:
                cypher = f"MATCH (n:{label} {{id: $id}}) SET n += $before " + mark
                params["before"] = before
        else:
            rel = _rel(c["target_label"])
            src, dst = (target[0], target[1]), (target[2], target[3])
            if op == "remove_edge":
                cypher = (f"MATCH (a:{_label(src[0])} {{id: $a}}), (b:{_label(dst[0])} {{id: $b}}) "
                          f"CREATE (a)-[r:{rel}]->(b) SET r = $before " + mark)
                params.update(a=_node_id(*src), b=_node_id(*dst), before=before)
            else:
                q, p = self._edge_q(src, rel, dst)
                params.update(p)
                if op == "add_edge":
                    cypher = q + " DELETE r " + mark
                else:  # set_edge_props
                    cypher = q + " SET r += $before " + mark
                    params["before"] = before
        self._run(cypher, params, write=True)
