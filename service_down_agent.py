"""
Service-Down-Agent

Prüft per Internet-Recherche (Gemini + Google Search Grounding), ob externe Services
(Cloud, CDN, Payment) gerade ausgefallen sind, mappt die Störung über den Kausal-Graphen
auf Revenue und entscheidet pro Kampagne, ob sie
  - im betroffenen Land pausiert,
  - wegen eines ausgefallenen Payment-Providers pausiert,
  - gedrosselt oder
  - weiterlaufen gelassen wird.

Grundsätze:
  * Nur LAUFENDE Störungen zählen (keine historischen Incidents aus Suchergebnissen).
  * Nur Services, von denen der Shop / die Kampagne wirklich abhängt, lösen Aktionen aus.
  * Kann ein Service nicht geprüft werden (kein API-Key, Fehler, unparsbare Antwort),
    ist der Status "unknown" -> niemals Pause, nur Hinweis in `uncertainties`.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import networkx as nx


# --------------------------------------------------------------------------- #
# Datenmodelle
# --------------------------------------------------------------------------- #

@dataclass
class OutageFinding:
    service_name: str
    category: str                 # "cloud_infra", "payment", "edge_cdn"
    status: str                   # "operational", "degraded", "major_outage", "unknown"
    is_down: bool                 # True nur bei laufender, bestätigter Störung
    is_global: bool
    affected_countries: List[str]  # ISO-3166 alpha-2, z. B. ["DE", "FR"]
    affected_regions: List[str]
    severity: str                 # "none", "partial", "major", "unknown"
    confidence: float             # 0..1, Einschätzung der Recherche
    started_at: Optional[str]
    sources: List[str]
    summary: str

    def affects(self, country: str) -> bool:
        return self.is_down and (self.is_global or country.upper() in self.affected_countries)


@dataclass
class Campaign:
    country: str                  # ISO-2
    channel: str
    daily_spend_eur: float
    spend_already_burned_pct: float
    campaign_id: Optional[str] = None
    # Payment-Provider, die in diesem Land im Checkout angeboten werden.
    # None -> aus Graph / Defaults abgeleitet.
    payment_methods: Optional[List[str]] = None
    # Kampagne bewirbt explizit eine Zahlungsart (z. B. "Jetzt mit Klarna zahlen").
    promoted_payment_method: Optional[str] = None


@dataclass
class CampaignDecision:
    country: str
    channel: str
    action: str                   # "PAUSE_CAMPAIGN", "REDUCE_BID_OR_CAP", "KEEP_RUNNING"
    scope: str                    # "country", "payment_method", "none"
    confidence: float
    triggering_services: List[str]
    impacted_graph_nodes: List[str]
    graph_path_to_revenue: List[str]
    reasoning: str
    uncertainties: List[str]
    sources: List[str] = field(default_factory=list)
    campaign_id: Optional[str] = None


# --------------------------------------------------------------------------- #
# Hilfsdaten
# --------------------------------------------------------------------------- #

COUNTRY_ALIASES: Dict[str, str] = {
    "germany": "DE", "deutschland": "DE", "france": "FR", "frankreich": "FR",
    "austria": "AT", "österreich": "AT", "switzerland": "CH", "schweiz": "CH",
    "netherlands": "NL", "niederlande": "NL", "belgium": "BE", "spain": "ES",
    "spanien": "ES", "italy": "IT", "italien": "IT", "poland": "PL", "polen": "PL",
    "sweden": "SE", "schweden": "SE", "denmark": "DK", "norway": "NO", "finland": "FI",
    "ireland": "IE", "portugal": "PT", "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "united states": "US", "usa": "US", "canada": "CA", "japan": "JP", "india": "IN",
    "australia": "AU", "brazil": "BR",
}

# Cloud-Regionen -> Land (ergänzt die Länderangaben der Recherche)
REGION_TO_COUNTRY: Dict[str, str] = {
    "eu-central-1": "DE", "eu-central-2": "CH", "eu-west-1": "IE", "eu-west-2": "GB",
    "eu-west-3": "FR", "eu-south-1": "IT", "eu-south-2": "ES", "eu-north-1": "SE",
    "us-east-1": "US", "us-east-2": "US", "us-west-1": "US", "us-west-2": "US",
    "germanywestcentral": "DE", "germanynorth": "DE", "westeurope": "NL", "northeurope": "IE",
    "francecentral": "FR", "uksouth": "GB", "switzerlandnorth": "CH",
    "europe-west3": "DE", "europe-west1": "BE", "europe-west4": "NL", "europe-west9": "FR",
    "europe-west2": "GB",
}

STATUS_TO_SEVERITY = {
    "operational": "none",
    "degraded": "partial",
    "major_outage": "major",
    "unknown": "unknown",
}


def normalize_country(value: str) -> Optional[str]:
    v = value.strip()
    if not v:
        return None
    low = v.lower()
    if low in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[low]
    if len(v) == 2 and v.isalpha():
        return "GB" if v.upper() == "UK" else v.upper()
    return None


def _extract_json(text: str) -> Optional[dict]:
    """Holt das erste JSON-Objekt aus einer LLM-Antwort (auch in ```json-Fences)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #

class ExternalOutageCampaignAgent:
    """
    Erkennt externe Services (AWS, Azure, GCP, Cloudflare, Payment, ...), recherchiert
    laufende Ausfälle im Internet, traversiert den Graphen Richtung Revenue und bewertet
    Kampagnenentscheidungen pro Land bzw. pro Zahlungsart.
    """

    DEFAULT_SERVICES: Dict[str, str] = {
        "AWS": "cloud_infra",
        "Microsoft Azure": "cloud_infra",
        "Google Cloud": "cloud_infra",
        "Cloudflare": "edge_cdn",
        "Akamai": "edge_cdn",
        "Stripe": "payment",
        "Adyen": "payment",
        "PayPal": "payment",
        "Klarna": "payment",
    }

    # Begriffe, über die Services in Knotennamen/-definitionen gefunden werden (Wortgrenzen!)
    SERVICE_ALIASES: Dict[str, List[str]] = {
        "AWS": ["aws", "amazon web services"],
        "Microsoft Azure": ["azure", "microsoft azure", "entra", "azure front door"],
        "Google Cloud": ["google cloud", "gcp"],
        "Cloudflare": ["cloudflare"],
        "Akamai": ["akamai"],
        "Stripe": ["stripe"],
        "Adyen": ["adyen"],
        "PayPal": ["paypal"],
        "Klarna": ["klarna"],
    }

    CATEGORY_TO_GRAPH_NODES: Dict[str, List[str]] = {
        "cloud_infra": [
            "Data Freshness & Tracking Health",
            "Search & Rec Relevance",
            "Product Page & Cart Conversion",
            "Payment Gateway Health",
        ],
        "edge_cdn": [
            "External Macro & ISP Stability",
            "Ad Impressions",
            "Product Page & Cart Conversion",
        ],
        "payment": [
            "Payment Gateway Health",
        ],
    }

    # Unter dieser Recherche-Konfidenz wird nie pausiert, höchstens gedrosselt.
    MIN_CONFIDENCE_TO_PAUSE = 0.6

    def __init__(
        self,
        graph: nx.DiGraph,
        search_fn: Optional[Callable[[str], str]] = None,
        services: Optional[Dict[str, str]] = None,
        max_workers: int = 6,
    ):
        """
        graph:      Kausal-Graph (Knoten "Revenue" als Ziel).
        search_fn:  prompt -> Antworttext (soll JSON enthalten). Default: Gemini + Google Search.
        services:   explizite Abhängigkeiten {Service: Kategorie}. Sonst aus dem Graph,
                    und falls dort nichts steht, DEFAULT_SERVICES.
        """
        self.g = graph
        self.search_fn = search_fn or self._default_gemini_search
        self.max_workers = max_workers
        self._explicit_services = services
        self._used_defaults = False

    # ------------------------------------------------------------------ #
    # Service-Erkennung
    # ------------------------------------------------------------------ #

    def discover_external_services(self) -> Dict[str, str]:
        """Services, von denen der Shop abhängt: explizit > Graph-Attribute/-Text > Defaults."""
        if self._explicit_services:
            return dict(self._explicit_services)

        discovered: Dict[str, str] = {}
        for node, attrs in self.g.nodes(data=True):
            for svc in attrs.get("external_services", []):
                discovered[svc] = (
                    attrs.get("service_category")
                    or self.DEFAULT_SERVICES.get(svc)
                    or "cloud_infra"
                )
            text = f"{node} {attrs.get('definition', '')}".lower()
            for svc, aliases in self.SERVICE_ALIASES.items():
                if any(re.search(rf"\b{re.escape(a)}\b", text) for a in aliases):
                    discovered.setdefault(svc, self.DEFAULT_SERVICES[svc])

        self._used_defaults = not discovered
        return discovered or dict(self.DEFAULT_SERVICES)

    # ------------------------------------------------------------------ #
    # Internet-Recherche
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_prompt(service: str, countries: List[str], now: datetime) -> str:
        return f"""Aktuelles Datum/Uhrzeit (UTC): {now:%Y-%m-%d %H:%M}.
Recherchiere im Internet (offizielle Statusseite von {service}, Downdetector, News, X/Reddit),
ob es bei "{service}" JETZT eine LAUFENDE Störung oder einen Ausfall gibt.

Regeln:
- Nur Störungen, die in den letzten 24 Stunden begonnen haben UND noch nicht behoben sind, zählen.
- Vergangene / bereits behobene Incidents => status "operational", ongoing false.
- Wenn du keine verlässliche Information findest => status "unknown".
- Besonders relevant sind diese Länder: {", ".join(countries)}.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, ohne weiteren Text:
{{
  "status": "operational" | "degraded" | "major_outage" | "unknown",
  "ongoing": true | false,
  "is_global": true | false,
  "affected_countries": ["ISO-3166 alpha-2 Codes, z. B. DE"],
  "affected_regions": ["Cloud-Regionen, z. B. eu-central-1"],
  "affected_products": ["z. B. EC2, Checkout API, Card Payments"],
  "started_at": "ISO-8601 oder null",
  "confidence": 0.0-1.0,
  "sources": ["URLs"],
  "summary": "max. 2 Sätze auf Deutsch"
}}"""

    @staticmethod
    def _default_gemini_search(prompt: str) -> str:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return json.dumps({"status": "unknown", "summary": "Kein GEMINI_API_KEY gesetzt – keine Live-Suche."})

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
            ),
        )
        text = response.text or ""

        # Grounding-Quellen anhängen, damit Entscheidungen nachvollziehbar sind
        try:
            chunks = response.candidates[0].grounding_metadata.grounding_chunks or []
            urls = [c.web.uri for c in chunks if getattr(c, "web", None) and c.web.uri]
            data = _extract_json(text)
            if data is not None and urls:
                data["sources"] = list(dict.fromkeys((data.get("sources") or []) + urls))
                return json.dumps(data)
        except (AttributeError, IndexError, TypeError):
            pass
        return text

    def _check_service(self, svc: str, cat: str, countries: List[str], now: datetime) -> OutageFinding:
        try:
            raw = self.search_fn(self.build_prompt(svc, countries, now))
            data = _extract_json(raw)
        except Exception as e:  # Netzwerk/API-Fehler dürfen den Agenten nicht crashen
            raw, data = f"Recherche fehlgeschlagen: {e}", None

        if data is None:
            return OutageFinding(svc, cat, "unknown", False, False, [], [], "unknown", 0.0,
                                 None, [], f"Keine auswertbare Antwort: {raw.strip()[:200]}")

        status = str(data.get("status", "unknown")).lower()
        if status not in STATUS_TO_SEVERITY:
            status = "unknown"
        ongoing = bool(data.get("ongoing", status in ("degraded", "major_outage")))
        is_down = status in ("degraded", "major_outage") and ongoing

        regions = [str(r).lower() for r in data.get("affected_regions") or []]
        countries_found = {c for c in (normalize_country(str(x)) for x in data.get("affected_countries") or []) if c}
        countries_found |= {REGION_TO_COUNTRY[r] for r in regions if r in REGION_TO_COUNTRY}
        is_global = bool(data.get("is_global")) or "global" in regions

        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        sources = [str(s) for s in data.get("sources") or []]
        if not sources:
            confidence = min(confidence, 0.5)  # ohne Quelle nicht vertrauen

        products = data.get("affected_products") or []
        summary = str(data.get("summary", "")).strip()
        if products:
            summary += f" (Betroffen: {', '.join(map(str, products))})"

        return OutageFinding(
            service_name=svc,
            category=cat,
            status=status,
            is_down=is_down,
            is_global=is_global and is_down,
            affected_countries=sorted(countries_found) if is_down else [],
            affected_regions=regions,
            severity=STATUS_TO_SEVERITY[status] if (is_down or status == "unknown") else "none",
            confidence=confidence,
            started_at=data.get("started_at"),
            sources=sources,
            summary=summary[:400],
        )

    def search_outages(self, countries: List[str]) -> List[OutageFinding]:
        """Recherchiert alle relevanten Services parallel (ein Suchaufruf pro Service)."""
        services = self.discover_external_services()
        countries = [normalize_country(c) or c.upper() for c in countries]
        now = datetime.now(timezone.utc)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(self._check_service, s, c, countries, now) for s, c in services.items()]
            return [f.result() for f in futures]

    # ------------------------------------------------------------------ #
    # Graph
    # ------------------------------------------------------------------ #

    def _shortest_path_to_revenue(self, start_node: str) -> List[str]:
        if start_node not in self.g or "Revenue" not in self.g:
            return []
        try:
            return nx.shortest_path(self.g, source=start_node, target="Revenue")
        except nx.NetworkXNoPath:
            return []

    def _graph_impact(self, findings: List[OutageFinding]):
        nodes: List[str] = []
        for f in findings:
            for n in self.CATEGORY_TO_GRAPH_NODES.get(f.category, []):
                if n in self.g and n not in nodes:
                    nodes.append(n)
        # Kürzester Weg zu Revenue = direkteste Wirkung des Ausfalls
        paths = [p for p in (self._shortest_path_to_revenue(n) for n in nodes) if p]
        return nodes, (min(paths, key=len) if paths else [])

    # ------------------------------------------------------------------ #
    # Entscheidung
    # ------------------------------------------------------------------ #

    def evaluate_campaign_in_country(
        self,
        country: str,
        channel: str,
        daily_spend_eur: float,
        spend_already_burned_pct: float,
        findings: List[OutageFinding],
        has_backup_payment_method: Optional[bool] = None,
        payment_methods: Optional[List[str]] = None,
        promoted_payment_method: Optional[str] = None,
        campaign_id: Optional[str] = None,
    ) -> CampaignDecision:
        country = normalize_country(country) or country.upper()
        relevant_services = self.discover_external_services()

        # Payment-Provider dieses Landes: explizit > alle Payment-Abhängigkeiten
        if payment_methods is None:
            payment_methods = [s for s, c in relevant_services.items() if c == "payment"]
        payment_set = set(payment_methods)

        def relevant(f: OutageFinding) -> bool:
            if f.category == "payment":
                return f.service_name in payment_set or f.service_name == promoted_payment_method
            return f.service_name in relevant_services

        active = [f for f in findings if relevant(f) and f.affects(country)]
        unknown = [f for f in findings if relevant(f) and f.status == "unknown"]

        uncertainties = [
            "Statusseiten melden oft zeitverzögert – eigene Error-Rate & Checkout-Metriken im Graph gegenchecken.",
        ]
        if unknown:
            uncertainties.append(
                "Nicht prüfbar (Status unbekannt): " + ", ".join(f.service_name for f in unknown)
            )
        if self._used_defaults and not self._explicit_services:
            uncertainties.append(
                "Keine Service-Abhängigkeiten im Graph gefunden – Default-Liste verwendet; "
                "evtl. werden Services geprüft, die der Shop gar nicht nutzt."
            )

        sources = list(dict.fromkeys(s for f in active for s in f.sources))
        impacted_nodes, path = self._graph_impact(active)
        path_txt = " -> ".join(path) or "kein Pfad im Graph"

        def decision(action, scope, conf, trig, reasoning):
            return CampaignDecision(
                country=country, channel=channel, action=action, scope=scope,
                confidence=round(conf, 2), triggering_services=[f.service_name for f in trig],
                impacted_graph_nodes=impacted_nodes, graph_path_to_revenue=path,
                reasoning=reasoning, uncertainties=uncertainties, sources=sources,
                campaign_id=campaign_id,
            )

        if not active:
            conf = 0.82 if not unknown else 0.6
            return decision("KEEP_RUNNING", "none", conf, [],
                            f"Keine laufenden Ausfälle relevanter Cloud-, Edge- oder Payment-Services in {country} erkannt.")

        budget_txt = f"{channel} ({daily_spend_eur:.0f} EUR/Tag, {spend_already_burned_pct*100:.0f}% verbraucht)"
        confident = lambda fs: [f for f in fs if f.confidence >= self.MIN_CONFIDENCE_TO_PAUSE]
        svc_txt = lambda fs: ", ".join(f"{f.service_name} ({f.severity}, conf {f.confidence:.2f})" for f in fs)

        # 1) Kritischer Infrastruktur-Ausfall -> Land pausieren
        infra_major = confident([f for f in active if f.category in ("cloud_infra", "edge_cdn") and f.severity == "major"])
        if infra_major and spend_already_burned_pct < 0.90:
            conf = min(0.92, max(f.confidence for f in infra_major))
            return decision("PAUSE_CAMPAIGN", "country", conf, infra_major,
                            f"Laufender Infrastruktur-Ausfall {svc_txt(infra_major)} betrifft {country}. "
                            f"Graph: {path_txt}. Shop/Tracking sind beeinträchtigt – {budget_txt} pausieren, "
                            f"um Traffic ohne Conversion zu vermeiden.")

        # 2) Payment-Ausfälle
        payment_down = [f for f in active if f.category == "payment"]
        payment_major = confident([f for f in payment_down if f.severity == "major"])
        down_names = {f.service_name for f in payment_down}

        # 2a) Kampagne bewirbt explizit die ausgefallene Zahlungsart
        promo_hit = [f for f in payment_major if f.service_name == promoted_payment_method]
        if promo_hit:
            return decision("PAUSE_CAMPAIGN", "payment_method", min(0.9, promo_hit[0].confidence), promo_hit,
                            f"Die Kampagne bewirbt {promoted_payment_method}, das in {country} ausgefallen ist "
                            f"({svc_txt(promo_hit)}). Graph: {path_txt}. Kampagne pausieren, bis der Provider wieder läuft.")

        # 2b) Alle Zahlungsarten des Landes down -> keine Conversions möglich
        if has_backup_payment_method is None:
            has_backup_payment_method = bool(payment_set - down_names)
        if payment_major and not has_backup_payment_method and spend_already_burned_pct < 0.85:
            conf = min(0.9, max(f.confidence for f in payment_major))
            return decision("PAUSE_CAMPAIGN", "country", conf, payment_major,
                            f"Payment-Ausfall {svc_txt(payment_major)} in {country} ohne funktionierende Alternative. "
                            f"Graph: {path_txt}. {budget_txt} würde Budget ohne Orders verbrennen.")

        # 3) Alles andere: teilweise / abgefangene / unsichere Störung -> drosseln
        backup_txt = (f" Alternative Zahlungsarten verfügbar: {', '.join(sorted(payment_set - down_names))}."
                      if payment_down and payment_set - down_names else "")
        conf = min(0.75, max(f.confidence for f in active))
        return decision("REDUCE_BID_OR_CAP", "country", conf, active,
                        f"Teilweise, abgefangene oder unsichere Störung {svc_txt(active)} in {country}. "
                        f"Graph: {path_txt}. Wirkung vorhanden, rechtfertigt aber eher Drosselung als Komplettstopp."
                        f"{backup_txt}")

    def evaluate_campaigns(self, campaigns: List[Campaign]) -> List[CampaignDecision]:
        """End-to-end: einmal recherchieren, dann jede Kampagne bewerten."""
        countries = sorted({c.country for c in campaigns})
        findings = self.search_outages(countries)
        return [
            self.evaluate_campaign_in_country(
                country=c.country, channel=c.channel, daily_spend_eur=c.daily_spend_eur,
                spend_already_burned_pct=c.spend_already_burned_pct, findings=findings,
                payment_methods=c.payment_methods, promoted_payment_method=c.promoted_payment_method,
                campaign_id=c.campaign_id,
            )
            for c in campaigns
        ]


if __name__ == "__main__":
    g = nx.DiGraph()
    g.add_node("Payment Gateway Health", external_services=["Stripe", "PayPal", "Klarna"], service_category="payment")
    g.add_node("Product Page & Cart Conversion", definition="Shop läuft auf AWS eu-central-1 hinter Cloudflare")
    g.add_edges_from([
        ("Payment Gateway Health", "Orders"),
        ("Product Page & Cart Conversion", "Orders"),
        ("Orders", "Revenue"),
    ])
    agent = ExternalOutageCampaignAgent(g)
    print("Abhängigkeiten:", agent.discover_external_services())
    for d in agent.evaluate_campaigns([
        Campaign("DE", "google_ads", 500, 0.3, campaign_id="de-search"),
        Campaign("DE", "meta", 200, 0.2, campaign_id="de-klarna-promo", promoted_payment_method="Klarna"),
        Campaign("FR", "google_ads", 300, 0.5, campaign_id="fr-search", payment_methods=["Stripe"]),
    ]):
        print(f"[{d.campaign_id}] {d.action} ({d.scope}, conf {d.confidence}): {d.reasoning}")
        for u in d.uncertainties:
            print("   ?", u)
