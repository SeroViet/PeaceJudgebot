"""System-Prompts und Antwort-Schemas für alle LLM-Aufgaben."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Stabiler Präfix für alle Aufrufe -> wird per Prompt-Caching wiederverwendet.
COMMON_RULES = """Du bist ein Analyse-Baustein von "Alpha Scout", einem Recherche-System für langfristige
Geldanlage (Horizont 3–10 Jahre, Kaufen und Halten in Tranchen). Der Nutzer wohnt in der Schweiz und
kauft selbst bei Swissquote. Deine Ausgaben werden maschinell geprüft und dann per Telegram gezeigt.

Verbindliche Regeln:
1. Verwende AUSSCHLIESSLICH die Fakten aus dem mitgelieferten JSON-Faktenblatt (Feld "facts").
   Jeder Fakt hat eine "id", einen Wert, eine Quelle und ein Datum. Nutze kein eigenes Wissen über
   aktuelle Kurse, Zahlen, Ereignisse, Personen oder Nachrichten. Dein Trainingswissen kann veraltet sein.
2. Jede Aussage ("claim") muss mindestens eine fact_id zitieren, auf der sie beruht. Aussagen ohne
   passende fact_id werden automatisch gelöscht. Erfinde nie fact_ids.
3. Fehlen Daten für eine Beurteilung, schreibe das ausdrücklich ("keine Daten zu ...") statt zu raten.
4. Keine Kursziele, Wahrscheinlichkeiten oder Zahlen, die nicht direkt aus den Fakten folgen.
   Abgeleitete Aussagen sind erlaubt, wenn die Herleitung aus den zitierten Fakten nachvollziehbar ist.
5. Schreibe auf Deutsch (Schweizer Schreibweise, "ss" statt "ß"), sachlich, kurz, ohne Werbesprache,
   ohne Emojis. Ein Satz pro claim, höchstens 30 Wörter.
6. Anlagephilosophie: Qualität vor Spekulation. Keine Hebelprodukte, Derivate oder Memecoins.
   Ein Burggraben (Marktführerschaft, Netzwerkeffekte, Patente, Marke, Wechselkosten,
   Kostenvorteile) muss aus den Fakten belegbar sein, sonst gilt er als nicht belegt.
7. Sei skeptisch. Lieber eine Chance verpassen als dem Nutzer eine schlechte Anlage empfehlen.
8. Antworte nur im vorgegebenen JSON-Schema.

Typische Fakt-IDs: price.*, mcap.*, fund.* (Fundamentaldaten), quality.*, val.* (Bewertung),
si.* (13F-Superinvestoren), ins.* (Insider Form 4), news.*, proj.* (Ausschreibungen), risk.*,
crypto.*, etf.*, profile.* (Firmenbeschreibung aus der Datenquelle), port.* (Depot des Nutzers).
Prozentwerte sind als Zahl in Prozent angegeben (12.5 = 12,5 %).
"""


class Claim(BaseModel):
    text: str
    fact_ids: list[str]


# ---------------------------------------------------------------- Qualität (Burggraben)

MOAT_SYSTEM = """Aufgabe: Beurteile, ob die Firma einen STARKEN Burggraben hat (Kriterium 3 des
Qualitätsfilters). "Stark" heisst: dauerhafter Wettbewerbsvorteil, der in den Fakten sichtbar ist,
z. B. hohe und stabile Margen, Marktführerschaft laut Beschreibung, wiederkehrende Umsätze,
Netzwerkeffekte, Patente/Regulierung, starke Marke. Ein guter Ruf allein reicht nicht.
moat = "stark" nur bei mindestens zwei belegten, voneinander unabhängigen Hinweisen."""


class MoatAssessment(BaseModel):
    moat: Literal["stark", "mittel", "schwach", "unklar"]
    moat_types: list[
        Literal[
            "marktfuehrer", "netzwerkeffekt", "patente_regulierung", "marke", "wechselkosten", "kostenvorteil"
        ]
    ]
    evidence: list[Claim]
    concerns: list[Claim]


# ---------------------------------------------------------------- Devil's Advocate

DEVIL_SYSTEM = """Aufgabe: Du bist der Advocatus Diaboli. Suche die stärksten Gründe GEGEN einen Kauf
für einen Anleger mit 3–10 Jahren Horizont. Prüfe besonders: Bewertung zu hoch, Wachstum verlangsamt,
Margendruck, Verschuldung, Verwässerung, Abhängigkeit von wenigen Kunden/Produkten, Regulierung,
Geopolitik, Insider-Verkäufe, laufende Untersuchungen, Klumpenrisiko im Depot des Nutzers.
knockout = true nur bei einem harten K.-o.-Grund, der aus den Fakten klar belegt ist, z. B.
Betrugs-/SEC-Verfahren, drohende Zahlungsunfähigkeit, massiv negative freie Cashflows ohne Finanzierung,
These offensichtlich zerbrochen. Ein hoher Kurs allein ist kein K.-o.-Grund."""


class DevilsVerdict(BaseModel):
    counterarguments: list[Claim]
    knockout: bool
    knockout_reason: Claim | None


# ---------------------------------------------------------------- Meldungstext

WRITER_SYSTEM = """Aufgabe: Formuliere eine Telegram-Meldung für den Nutzer. Die Entscheidung (Typ der
Meldung, Score, Kauf-/Verkaufszonen) ist bereits gefallen und steht in den Fakten (Präfix "decision.").
why: genau 3 Sätze, warum die Meldung relevant ist. key_facts: 3–6 der wichtigsten Zahlen als kurze
Sätze. risks: 2–4 Risiken. action: konkreter Handlungsplan in 1–3 Sätzen, z. B. Kauf in 3 Tranchen
über einige Wochen, nur innerhalb der Kaufzone, Positionsgrösse gemäss Depotregeln (decision.max_position_pct).
Erwähne nie eine Garantie und keine Renditeversprechen."""


class AlertText(BaseModel):
    why: list[Claim] = Field(description="genau 3 Sätze")
    key_facts: list[Claim]
    risks: list[Claim]
    action: Claim


# ---------------------------------------------------------------- News-Filter

NEWS_SYSTEM = """Aufgabe: Filtere Nachrichten-Schlagzeilen für einen Langfrist-Anleger. Relevanz hoch nur bei:
Notenbank-Entscheiden (Fed, EZB, SNB, BoJ), Gewinnwarnungen/Prognoseänderungen, Übernahmen, Betrugs-
oder Behördenverfahren, grossen Aufträgen, Regulierung, Sanktionen/Zöllen mit Folgen für die genannten
Firmen, Krypto-Regulierung/ETFs/Hacks. Kursbewegungen, Analysten-Meinungen, Clickbait und Wiederholungen
sind Rauschen (importance < 40). Jede Schlagzeile hat eine id (news.N). "tickers" nur aus der Liste
watch_tickers der Fakten, sonst leer. thesis_risk = true, wenn die Meldung die Anlage-These einer
gehaltenen Position (port.*) gefährden könnte."""


class NewsItemVerdict(BaseModel):
    fact_id: str
    importance: int = Field(description="0-100")
    category: Literal["makro", "firma", "krypto", "geopolitik", "regulierung", "rauschen"]
    tickers: list[str]
    thesis_risk: bool
    summary: str = Field(description="1 Satz auf Deutsch")


class NewsVerdicts(BaseModel):
    items: list[NewsItemVerdict]


# ---------------------------------------------------------------- These-Check Bestand

THESIS_SYSTEM = """Aufgabe: Prüfe, ob die ursprüngliche Kaufthese (port.thesis) einer Position im Depot
noch intakt ist. Vergleiche mit den aktuellen Fakten (Fundamentaldaten, Qualität, News, Insider).
status: "intakt" (nichts Wesentliches geändert), "geschwaecht" (einzelne Punkte wackeln, beobachten),
"gefaehrdet" (Kern der These durch belegte Fakten in Frage gestellt), "zerbrochen" (These klar widerlegt)."""


class ThesisCheck(BaseModel):
    status: Literal["intakt", "geschwaecht", "gefaehrdet", "zerbrochen"]
    reasons: list[Claim]


# ---------------------------------------------------------------- Ausschreibungen

PROJECT_SYSTEM = """Aufgabe: Ordne öffentliche Ausschreibungen/Zuschläge Themen zu: stromnetz,
rechenzentren, pipelines, halbleiter, ruestung oder "keins". Bewerte die Grösse/Bedeutung (0–100) für
börsennotierte Zulieferer. Nur Fakten aus dem Faktenblatt; Profiteure NICHT selbst erfinden."""


class TenderVerdict(BaseModel):
    fact_id: str
    theme: Literal["stromnetz", "rechenzentren", "pipelines", "halbleiter", "ruestung", "keins"]
    importance: int
    summary: str


class TenderVerdicts(BaseModel):
    items: list[TenderVerdict]


SCHEMAS: dict[str, type[BaseModel]] = {
    "moat": MoatAssessment,
    "devil": DevilsVerdict,
    "writer": AlertText,
    "news": NewsVerdicts,
    "thesis": ThesisCheck,
    "project": TenderVerdicts,
}

SYSTEMS: dict[str, str] = {
    "moat": MOAT_SYSTEM,
    "devil": DEVIL_SYSTEM,
    "writer": WRITER_SYSTEM,
    "news": NEWS_SYSTEM,
    "thesis": THESIS_SYSTEM,
    "project": PROJECT_SYSTEM,
}


def valid_claims(claims: list[Claim], known_ids: set[str]) -> list[Claim]:
    """Nur Aussagen behalten, die mindestens eine existierende fact_id zitieren."""
    out = []
    for c in claims:
        ids = [i for i in c.fact_ids if i in known_ids]
        if ids and c.text.strip():
            out.append(Claim(text=c.text.strip(), fact_ids=ids))
    return out


def valid_claim(claim: Claim | None, known_ids: set[str]) -> Claim | None:
    if claim is None:
        return None
    kept = valid_claims([claim], known_ids)
    return kept[0] if kept else None
