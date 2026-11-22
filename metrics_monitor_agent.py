from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple
import networkx as nx


@dataclass
class NewMetricRequest:
    name: str
    definition: str
    calculation: str
    unit: str = "ratio"
    dimensions: List[str] = None
    controllable: bool = True

    def __post_init__(self):
        if self.dimensions is None:
            self.dimensions = ["country", "device_os", "channel", "hour_of_day"]


@dataclass
class LLMReasoningResult:
    accepted: bool
    proposed_definition: str
    edges: List[Tuple[str, str, str, float]]
    reasoning_steps: List[str]
    uncertainties: List[str]
    used_external_ai: bool


class ExternalAIGraphAgent:
    """
    Nutzt ein externes LLM zum Nachdenken über den Graphen:
    - wandert durch Knoten/Nachbarn Richtung Revenue,
    - schlägt Definition, Relationen & Gewichte für neue Metriken vor,
    - schlägt Gewicht-/Relationsänderungen vor,
    - validiert alles streng gegen DAG & Revenue-Pfad.
    """

    VALID_RELATIONS = {
        "formula",
        "formula_input",
        "causal",
        "causal_prior",
        "cannibalization",
        "cost_efficiency",
        "data_validity",
    }

    def __init__(
        self,
        graph: nx.DiGraph,
        llm_json_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    ):
        self.g = graph
        self.llm_json_fn = llm_json_fn or self._default_gemini_json_reasoner
        self.decision_log: List[Dict[str, Any]] = []

    @staticmethod
    def _default_gemini_json_reasoner(prompt: str) -> Dict[str, Any]:
        """
        Ruft Gemini mit JSON-Ausgabe auf. Benötigt GEMINI_API_KEY in der Umgebung.
        """
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY nicht gesetzt; nutze Fallback.")

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        return json.loads(response.text or "{}")

    def _graph_context_snapshot(self) -> Dict[str, Any]:
        nodes_info = []
        for n, attrs in self.g.nodes(data=True):
            hops = (
                0 if n == "Revenue"
                else (nx.shortest_path_length(self.g, n, "Revenue") if "Revenue" in self.g and nx.has_path(self.g, n, "Revenue") else None)
            )
            nodes_info.append({
                "node": n,
                "hops_to_revenue": hops,
                "controllable": attrs.get("controllable", True),
                "out_neighbors": list(self.g.successors(n)),
                "in_neighbors": list(self.g.predecessors(n)),
            })

        edges_info = [
            {
                "from": u,
                "to": v,
                "relation": d.get("relation"),
                "weight": d.get("weight"),
            }
            for u, v, d in self.g.edges(data=True)
        ]
        return {"nodes": nodes_info, "edges": edges_info}

    def _validate_candidate_edges(
        self, new_node: Optional[str], new_node_attrs: Optional[Dict[str, Any]], edges: List[Tuple[str, str, str, float]]
    ) -> Tuple[bool, str]:
        trial = self.g.copy()
        if new_node:
            trial.add_node(new_node, **(new_node_attrs or {}))

        for u, v, rel, w in edges:
            if rel not in self.VALID_RELATIONS:
                return False, f"Ungültige Relation '{rel}'."
            if not isinstance(w, (int, float)) or not (-1.0 <= float(w) <= 1.0):
                return False, f"Gewicht {w} außerhalb [-1.0, 1.0]."
            if u not in trial or v not in trial:
                return False, f"Unbekannter Knoten in Kante ({u} -> {v})."
            trial.add_edge(u, v, relation=rel, weight=float(w))

        if not nx.is_directed_acyclic_graph(trial):
            return False, "Kanten erzeugen einen Zyklus (kein DAG)."

        check_nodes = [new_node] if new_node else [u for u, _, _, _ in edges]
        for cn in check_nodes:
            if cn and cn != "Revenue" and "Revenue" in trial and not nx.has_path(trial, cn, "Revenue"):
                return False, f"Knoten '{cn}' hat keinen Pfad mehr zu 'Revenue'."

        return True, "OK (DAG & Revenue-Pfad gültig)."

    def _heuristic_fallback_edges(self, req: NewMetricRequest) -> Tuple[List[Tuple[str, str, str, float]], List[str], List[str]]:
        text = f"{req.name} {req.definition} {req.calculation}".lower()
        steps = ["Fallback-Reasoning gestartet: durchsuche Funnel- und Friction-Knoten Richtung Revenue."]
        if any(k in text for k in ["payment", "checkout", "pay", "decline"]):
            target, rel, w = "Payment Gateway Health", "causal", -0.85
        elif any(k in text for k in ["cart", "pdp", "load", "lag", "slow", "device"]):
            target, rel, w = "Product Page & Cart Conversion", "causal", -0.80
        elif any(k in text for k in ["stock", "inventory", "oos"]):
            target, rel, w = "In-Stock Availability", "causal", 0.85
        else:
            target, rel, w = "Product Page & Cart Conversion", "causal", 0.70

        steps.append(f"Wählte Zielknoten '{target}' mit Relation '{rel}' und Gewicht {w:+.2f}.")
        uncertainties = ["Externe KI war nicht aktiv/verfügbar; Gewicht und Kante basieren auf Heuristik."]
        return [(req.name, target, rel, w)], steps, uncertainties

    def reason_and_add_metric(self, req: NewMetricRequest) -> LLMReasoningResult:
        snapshot = self._graph_context_snapshot()
        prompt = f"""
Du bist ein autonomer Graph-Manager-Agent für ein E-Commerce / Performance-Marketing-System.
Deine Aufgabe:
1. Wandere gedanklich durch den aktuellen Graphen (von Revenue rückwärts über Treiber, Funnel, Pacing, Releases und externe/Device-Faktoren).
2. Formuliere eine klare Business-Definition für die neue Metrik.
3. Schlage sinnvolle gerichtete Kanten vor (nur aus den erlaubten Relationstypen: {sorted(self.VALID_RELATIONS)}, Gewichte zwischen -1.0 und 1.0), sodass ein klarer Pfad zu "Revenue" entsteht und keine Zyklen entstehen.
4. Nenne explizit deine Reasoning-Schritte und Unsicherheiten.

Aktueller Graph:
{json.dumps(snapshot, indent=2)}

Neue Metrik:
- Name: {req.name}
- Beschreibung: {req.definition}
- Berechnung/Query: {req.calculation}
- Unit: {req.unit}
- Controllable: {req.controllable}
- Dimensionen: {req.dimensions}

Antworte AUSSCHLIESSLICH als JSON mit diesem Schema:
{{
  "proposed_definition": "...",
  "reasoning_steps": ["Schritt 1...", "Schritt 2..."],
  "edges": [
    {{"source": "{req.name}", "target": "<existierender Knoten>", "relation": "causal", "weight": -0.8}}
  ],
  "uncertainties": ["Unsicherheit 1..."]
}}
"""
        used_ai = False
        try:
            data = self.llm_json_fn(prompt)
            used_ai = True
            prop_def = data.get("proposed_definition") or req.definition
            steps = list(data.get("reasoning_steps") or ["LLM-Reasoning ohne detaillierte Schritte."])
            uncertainties = list(data.get("uncertainties") or [])
            raw_edges = data.get("edges") or []
            edges = [
                (
                    str(e["source"]),
                    str(e["target"]),
                    str(e["relation"]),
                    float(e["weight"]),
                )
                for e in raw_edges
            ]
        except Exception as exc:
            prop_def = f"{req.definition} (Auto-Definition)"
            edges, steps, uncertainties = self._heuristic_fallback_edges(req)
            uncertainties.append(f"LLM-Aufruf Hinweis: {exc}")

        node_attrs = {
            "definition": prop_def,
            "calculation": req.calculation,
            "unit": req.unit,
            "dimensions": req.dimensions,
            "controllable": req.controllable,
        }

        ok, msg = self._validate_candidate_edges(req.name, node_attrs, edges)
        if not ok:
            # Falls das LLM einen ungültigen Vorschlag macht, sauber auf Heuristik zurückfallen
            fb_edges, fb_steps, fb_unc = self._heuristic_fallback_edges(req)
            ok_fb, msg_fb = self._validate_candidate_edges(req.name, node_attrs, fb_edges)
            if ok_fb:
                edges = fb_edges
                steps = steps + [f"LLM-Vorschlag verworfen ({msg}). Fallback genutzt:"] + fb_steps
                uncertainties = uncertainties + fb_unc
                ok = True
                msg = msg_fb

        if ok:
            self.g.add_node(req.name, **node_attrs)
            for u, v, rel, w in edges:
                self.g.add_edge(u, v, relation=rel, weight=w)
            self.decision_log.append({
                "action": "add_metric",
                "metric": req.name,
                "used_ai": used_ai,
                "edges": edges,
                "validation": msg,
            })

        return LLMReasoningResult(
            accepted=ok,
            proposed_definition=prop_def,
            edges=edges,
            reasoning_steps=steps,
            uncertainties=uncertainties,
            used_external_ai=used_ai,
        )

    def reason_and_update_edge(self, source: str, target: str, evidence_summary: str) -> Tuple[bool, str, List[str]]:
        """
        Lässt das externe LLM anhand neuer Evidenz beurteilen, ob Relation oder Gewicht
        einer bestehenden Kante angepasst werden sollte, und prüft das Ergebnis am Graphen.
        """
        if not self.g.has_edge(source, target):
            return False, f"Kante ({source} -> {target}) existiert nicht.", []

        current = self.g[source][target]
        prompt = f"""
Du bist ein Graph-Manager-Agent. Prüfe anhand neuer Evidenz, ob Relation oder Gewicht
der Kante "{source}" -> "{target}" angepasst werden soll.
Aktuell: relation="{current.get('relation')}", weight={current.get('weight')}.
Erlaubte Relationen: {sorted(self.VALID_RELATIONS)}. Gewichte in [-1.0, 1.0].

Evidenz:
{evidence_summary}

Antworte NUR als JSON:
{{
  "new_relation": "{current.get('relation')}",
  "new_weight": {current.get('weight')},
  "reasoning_steps": ["..."],
  "summary": "..."
}}
"""
        try:
            res = self.llm_json_fn(prompt)
            new_rel = str(res.get("new_relation", current.get("relation")))
            new_w = float(res.get("new_weight", current.get("weight")))
            steps = list(res.get("reasoning_steps") or [])
            summary = str(res.get("summary") or "Gewicht/Relation aktualisiert.")
        except Exception as exc:
            # Einfacher deterministischer Fallback bei Evidenz für starken Effekt
            new_rel = current.get("relation")
            new_w = round(max(-1.0, min(1.0, float(current.get("weight", 0.5)) + 0.05)), 2)
            steps = [f"Fallback genutzt ({exc}): Gewicht leicht Richtung +0.05 angepasst."]
            summary = f"Fallback-Update für {source} -> {target}."

        # Temporär setzen und DAG / Revenue-Pfad prüfen
        old_rel, old_w = current.get("relation"), current.get("weight")
        self.g[source][target]["relation"] = new_rel
        self.g[source][target]["weight"] = new_w

        trial = self.g.copy()
        if not nx.is_directed_acyclic_graph(trial) or (
            source != "Revenue" and "Revenue" in trial and not nx.has_path(trial, source, "Revenue")
        ):
            self.g[source][target]["relation"] = old_rel
            self.g[source][target]["weight"] = old_w
            return False, "Änderung abgelehnt (Zyklus oder Revenue-Pfad verletzt).", steps

        self.decision_log.append({
            "action": "update_edge",
            "edge": (source, target),
            "old": (old_rel, old_w),
            "new": (new_rel, new_w),
        })
        return True, summary, steps
