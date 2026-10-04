"""Scout-Agent (Claude + Websuche): Aufstellung, Verletzungen, Sperren, Karten, Müdigkeit.

Grundregel aus der Spezifikation: Der Agent sammelt und prüft Fakten und bewertet das
Risiko eines Tipps (bestätigt / vorsicht / streichen). Wahrscheinlichkeiten berechnet
weiterhin nur das statistische Modell.

Ablauf pro Spiel:
1. Recherche mit Websuche (Landessprache der Teams, offizielle Aufstellung falls schon da)
2. Strukturierte Auswertung in ein festes Schema (Pydantic, validiert)
Kosten werden pro Lauf erfasst; ein Tagesbudget begrenzt die Ausgaben.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "claude-opus-5-5")
# USD pro 1M Tokens (Eingabe, Ausgabe) und pro Websuche; für die Budget-Kontrolle
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
WEB_SEARCH_USD = 0.01
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class MissingPlayer(BaseModel):
    name: str
    position: str = Field(description="Torwart, Abwehr, Mittelfeld oder Sturm")
    status: Literal["fällt aus", "fraglich", "gesperrt"]
    reason: str = Field(description="z. B. Muskelverletzung, Rote Karte, Gelbsperre, Krankheit")
    key_player: bool = Field(description="Stammspieler bzw. wichtige Rolle (Torjäger, Stammtorhüter, Kapitän)")


class TeamIntel(BaseModel):
    team: str
    missing: list[MissingPlayer]
    yellow_card_risk: list[str] = Field(description="Spieler, die mit der nächsten Gelben Karte gesperrt wären")
    lineup_confirmed: bool = Field(description="True nur, wenn die offizielle Aufstellung schon veröffentlicht ist")
    expected_lineup: list[str] = Field(description="Voraussichtliche oder offizielle Startelf (bis zu 11 Namen)")
    formation: str | None
    goalkeeper: str = Field(default="", description="Wer steht im Tor: Stammtorhüter oder Ersatz (mit Namen); "
                                                    "fehlt der Stammtorhüter, unbedingt erwähnen")
    top_players: str = Field(default="", description="Leistungsträger auf dem Platz: wie viele der wichtigsten "
                                                     "Spieler (Torjäger, Spielmacher, Abwehrchef) spielen, wer fehlt")
    fatigue: str = Field(description="Belastung: Spiele in den letzten Tagen, Reisen, Europapokal, Rotation")
    motivation: str = Field(description="Tabellensituation, Bedeutung des Spiels, Trainerlage")


class MatchIntel(BaseModel):
    home: TeamIntel
    away: TeamIntel
    goal_trend: str = Field(default="", description="Tore: Schnitt der letzten Spiele beider Teams (erzielt/kassiert), "
                                                    "xG falls bekannt, direkte Duelle, Spielstil (offensiv/defensiv)")
    tip_assessment: Literal["bestätigt", "vorsicht", "streichen"]
    tip_reason: str = Field(description="Kurze Begründung, warum der Tipp bestätigt, riskant oder zu streichen ist")
    summary: str = Field(description="2-3 Sätze Zusammenfassung auf Deutsch")
    sources: list[str] = Field(description="URLs der wichtigsten Quellen")
    best_tip: str | None = Field(default=None, description="Aus der Liste der möglichen Tipps genau der Text des "
                                                            "Tipps, den die Fakten am besten stützen; sonst null")


@dataclass
class ScoutResult:
    intel: MatchIntel
    cost_usd: float
    searches: int
    model: str


SYSTEM = """Du bist ein professioneller Fussball-Scout für ein Wett-Analyse-System.
Recherchiere mit der Websuche aktuelle, verlässliche Informationen zum genannten Spiel:
- Verletzte, fragliche und gesperrte Spieler beider Teams (Rote Karte, Gelbsperre), mit Position
  und ob es Stammspieler sind
- Spieler, die mit der nächsten Gelben Karte gesperrt wären
- Torhüter: spielt der Stammtorhüter oder ein Ersatz (verletzt, gesperrt, Rotation)?
- Leistungsträger: wie viele der wichtigsten Spieler (Torjäger, Spielmacher, Abwehrchef, Kapitän)
  stehen auf dem Platz, wer fehlt
- Offizielle Aufstellung, falls schon veröffentlicht (ca. 1 Stunde vor Anpfiff), sonst die
  voraussichtliche Startelf und Formation aus seriösen Vorschauen
- Belastung/Müdigkeit: Spiele in den letzten 7-14 Tagen (inkl. Europapokal, Pokal, Länderspiele),
  Reisen, angekündigte Rotation
- Motivation: Tabellensituation, Bedeutung des Spiels, Trainerwechsel, Unruhe
- Tore: erzielte und kassierte Tore der letzten 5 Spiele beider Teams, xG falls verfügbar,
  direkte Duelle, Spielstil (offensiv, defensiv, Konter), Wetter/Platz falls auffällig
Suche auch in der Landessprache der Teams (z. B. Deutsch, Englisch, Italienisch, Spanisch,
Französisch). Bevorzuge offizielle Vereinsseiten, Pressekonferenzen und grosse Sportmedien.
Gehe mit den Suchen sparsam um: suche zuerst nach einer Vorschau mit Team-News für beide Teams
(z. B. "<Heim> vs <Gast> preview team news" oder auf Deutsch "Vorschau Aufstellung"), dann
Sperren/Karten, zuletzt Aufstellung. Funktioniert eine Suche nicht, formuliere sie einfacher um. Erfinde nichts: Wenn eine Information nicht zu
finden ist, sage das.
Bewerte danach den vorgegebenen Tipp nur anhand dieser Fakten: Ändern die Ausfälle,
Aufstellung oder Belastung etwas Wesentliches (z. B. Torjäger fehlt bei einem Über-Tipp,
Stammtorhüter gesperrt bei einem Unter-Tipp, B-Elf wegen Rotation beim Favoriten)?
Gibt es eine Liste möglicher Tipps, sage am Ende, welcher davon am besten zu den Fakten passt
(z. B. beide Abwehrreihen geschwächt → eher Über-Tipp; Favorit rotiert → eher Tore-Tipp statt Sieg).
Steht bei den Tipps eine geschätzte Sporttip-Quote, gilt: Wenn mehrere Tipps von den Fakten gleich gut
gestützt werden, nimm den mit dem besten Verhältnis Sporttip-Quote zu fairer Quote (bester Wert).
Die Fakten haben aber immer Vorrang – nie einen Tipp nur wegen der Quote wählen.
Nenne keine eigenen Wahrscheinlichkeiten und keine Quoten.

Bei Unter-Tipps (z. B. "Unter 3.5 / 4.5 Tore") prüfe besonders: Muss ein Team unbedingt gewinnen
oder aufholen (offenes Spiel)? Kehren Torjäger zurück? Fehlen Stammverteidiger oder der Stammtorhüter?
Gab es zuletzt torreiche direkte Duelle? Solche Fakten sprechen gegen einen Unter-Tipp.

Regeln für die Bewertung:
- Das Spiel findet zum angegebenen Termin statt. Zweifle Termin oder Ansetzung nie an.
- "streichen" nur bei konkreten, gewichtigen Fakten gegen den Tipp (z. B. Torjäger und Ersatz fehlen
  bei einem Über-Tipp, Stammtorhüter gesperrt, B-Elf angekündigt).
- "vorsicht" nur bei konkreten Fakten, die den Tipp spürbar schwächen.
- Fehlende oder dünne Informationen sind KEIN Grund für "vorsicht" oder "streichen". Findest du nichts
  Negatives, ist der Tipp "bestätigt". Erwähne die dünne Quellenlage höchstens kurz.
- tip_reason: 1–2 kurze Sätze auf Deutsch mit den wichtigsten Fakten, ohne Meta-Kommentare über
  deine Suche."""


def _cost(model: str, usage, searches: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tokens_in += 0.1 * (getattr(usage, "cache_read_input_tokens", 0) or 0)
    return tokens_in / 1e6 * pin + (usage.output_tokens or 0) / 1e6 * pout + searches * WEB_SEARCH_USD


def _count_searches(content) -> int:
    return sum(1 for b in content if getattr(b, "type", "") == "server_tool_use"
               and getattr(b, "name", "") == "web_search")


def _options(alternatives: list[str] | None, notes: list[str] | None = None) -> str:
    """Liste der möglichen Tipps, optional mit Chance, fairer Quote und geschätzter Sporttip-Quote."""
    if not alternatives:
        return ""
    notes = notes or [""] * len(alternatives)
    return ("\nMögliche Tipps (alle laut Markt sicher genug):\n"
            + "\n".join(f"- {a}" + (f"  [{n}]" if n else "") for a, n in zip(alternatives, notes)))


def research(client, match: str, kickoff_local: str, competition: str, tip: str, context: str,
             model: str = MODEL, max_searches: int = 10, alternatives: list[str] | None = None,
             notes: list[str] | None = None) -> tuple[str, float, int]:
    """Schritt 1: Websuche. Gibt (Rechercheergebnis als Text, Kosten, Anzahl Suchen) zurück."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Zurich")).strftime("%A, %d.%m.%Y %H:%M")
    user = (f"Heute ist {today} (Schweizer Zeit). Nutze nur Informationen, die für dieses Spiel aktuell sind; "
            "Artikel aus früheren Saisons oder vor dem letzten Spiel der Teams ignorieren.\n"
            f"Spiel: {match}\nWettbewerb: {competition}\nAnstoss (Schweizer Zeit): {kickoff_local}\n"
            f"Zu prüfender Tipp: {tip}{_options(alternatives, notes)}\nBekannte Daten aus unserer Datenbank:\n{context}\n\n"
            "Recherchiere jetzt und fasse alle Fakten mit Quellen-URLs zusammen.")
    messages = [{"role": "user", "content": user}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    cost, searches = 0.0, 0
    for _ in range(4):  # pause_turn: Server-Tool-Schleife fortsetzen
        resp = client.beta.messages.create(
            model=model, max_tokens=16000, system=SYSTEM, tools=tools, messages=messages,
            output_config={"effort": "medium"}, betas=[FALLBACK_BETA], fallbacks="default",
        )
        n = _count_searches(resp.content)
        searches += n
        cost += _cost(model, resp.usage, n)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Agent abgelehnt: {getattr(resp.stop_details, 'category', None)}")
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        return text, cost, searches
    raise RuntimeError("Recherche nach mehreren Fortsetzungen nicht abgeschlossen")


def structure(client, research_text: str, match: str, tip: str, model: str = MODEL,
              alternatives: list[str] | None = None, notes: list[str] | None = None) -> tuple[MatchIntel, float]:
    """Schritt 2: Rechercheergebnis in das feste Schema überführen (validiert)."""
    resp = client.beta.messages.parse(
        model=model, max_tokens=8000, output_format=MatchIntel, output_config={"effort": "low"},
        betas=[FALLBACK_BETA], fallbacks="default",
        messages=[{"role": "user", "content":
                   f"Spiel: {match}\nTipp: {tip}{_options(alternatives, notes)}\n\nRecherche:\n{research_text}\n\n"
                   "Übertrage die Recherche vollständig und ohne Erfindungen in das Schema. "
                   "Heimteam zuerst. Bewerte den Tipp (bestätigt/vorsicht/streichen). "
                   "best_tip: exakt einer der möglichen Tipps (Text unverändert) oder null."}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("Strukturierung fehlgeschlagen")
    return resp.parsed_output, _cost(model, resp.usage, 0)


def scout_match(client, match: str, kickoff_local: str, competition: str, tip: str, context: str,
                model: str = MODEL, alternatives: list[str] | None = None, max_searches: int = 6,
                notes: list[str] | None = None) -> ScoutResult:
    text, c1, searches = research(client, match, kickoff_local, competition, tip, context, model,
                                  max_searches=max_searches, alternatives=alternatives, notes=notes)
    intel, c2 = structure(client, text, match, tip, model, alternatives=alternatives, notes=notes)
    if intel.best_tip not in (alternatives or []):
        intel.best_tip = None
    return ScoutResult(intel, c1 + c2, searches, model)


class TipJudgement(BaseModel):
    tip_assessment: Literal["bestätigt", "vorsicht", "streichen"]
    tip_reason: str = Field(description="1 kurzer Satz auf Deutsch: warum der Tipp aufgeht oder nicht")


def judge_tip(client, intel: MatchIntel, match: str, tip: str, model: str = MODEL) -> tuple[TipJudgement, float]:
    """Einen weiteren Tipp für ein schon recherchiertes Spiel bewerten – ohne neue Websuche (günstig)."""
    resp = client.beta.messages.parse(
        model=model, max_tokens=2000, output_format=TipJudgement, output_config={"effort": "low"},
        betas=[FALLBACK_BETA], fallbacks="default",
        messages=[{"role": "user", "content":
                   f"Spiel: {match}\nZu bewertender Tipp: {tip}\n\nRecherchierte Fakten (JSON):\n"
                   f"{intel.model_dump_json()}\n\nBewerte den Tipp nur anhand dieser Fakten: bestätigt, vorsicht "
                   "oder streichen, mit einem kurzen Grund (Ausfälle, Torhüter, Form, Müdigkeit, Aufstellung). "
                   "Fehlende Infos sind kein Grund für vorsicht/streichen – nur konkrete negative Fakten. "
                   "Keine Quoten, keine Wahrscheinlichkeiten."}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("Bewertung fehlgeschlagen")
    return resp.parsed_output, _cost(model, resp.usage, 0)


class HalfTime(BaseModel):
    found: bool = Field(description="True nur, wenn der Halbzeitstand in einer Quelle eindeutig steht")
    ht_home: int | None = Field(description="Tore Heimteam zur Halbzeit")
    ht_away: int | None = Field(description="Tore Gastteam zur Halbzeit")


def halftime_score(client, match: str, date: str, final: str, model: str = MODEL) -> tuple[HalfTime, float]:
    """Halbzeitstand eines beendeten Spiels per Websuche nachschlagen (die Ergebnis-Quelle liefert nur den Endstand)."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}]
    messages = [{"role": "user", "content": f"Wie stand es zur Halbzeit im Fussballspiel {match} am {date}? "
                                            f"Endstand war {final}. Antworte kurz mit dem Halbzeitstand und der Quelle."}]
    cost, text = 0.0, ""
    for _ in range(3):
        resp = client.beta.messages.create(model=model, max_tokens=2000, tools=tools, messages=messages,
                                           output_config={"effort": "low"}, betas=[FALLBACK_BETA], fallbacks="default")
        n = _count_searches(resp.content)
        cost += _cost(model, resp.usage, n)
        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        break
    parsed = client.beta.messages.parse(
        model=model, max_tokens=1000, output_format=HalfTime, output_config={"effort": "low"},
        betas=[FALLBACK_BETA], fallbacks="default",
        messages=[{"role": "user", "content": f"Spiel: {match} (Endstand {final})\nRecherche:\n{text}\n\n"
                                              "Halbzeitstand (Heim:Gast) eintragen; found=false, wenn unklar."}])
    cost += _cost(model, parsed.usage, 0)
    ht = parsed.parsed_output or HalfTime(found=False, ht_home=None, ht_away=None)
    return ht, cost


def make_client():
    """Anthropic-Client oder None, wenn keine Zugangsdaten konfiguriert sind."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic

    return anthropic.Anthropic()


def format_intel(match: str, tip: str, intel: MatchIntel) -> str:
    icon = {"bestätigt": "✅", "vorsicht": "⚠️", "streichen": "❌"}[intel.tip_assessment]
    lines = [f"🕵️ <b>{match}</b>", f"Tipp: {tip} → {icon} <b>{intel.tip_assessment}</b>", intel.tip_reason]
    for t in (intel.home, intel.away):
        miss = ", ".join(f"{p.name} ({p.status}{', Stamm' if p.key_player else ''})" for p in t.missing) or "keine"
        lines.append(f"\n<b>{t.team}</b> {'(offizielle Elf)' if t.lineup_confirmed else '(voraussichtlich)'} "
                     f"{t.formation or ''}\nAusfälle: {miss}")
        if t.yellow_card_risk:
            lines.append("Gelbsperre droht: " + ", ".join(t.yellow_card_risk))
        if t.expected_lineup:
            lines.append("Elf: " + ", ".join(t.expected_lineup[:11]))
        if t.goalkeeper:
            lines.append(f"🧤 Tor: {t.goalkeeper}")
        if t.top_players:
            lines.append(f"⭐ Leistungsträger: {t.top_players}")
        lines.append(f"Belastung: {t.fatigue}")
    if intel.goal_trend:
        lines.append(f"\n⚽ Tore: {intel.goal_trend}")
    if intel.best_tip and intel.best_tip != tip:
        lines.append(f"🔄 Scout empfiehlt stattdessen: <b>{intel.best_tip}</b>")
    lines.append(f"\n{intel.summary}")
    return "\n".join(lines)
