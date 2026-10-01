"""Sporttip-Wettschein per Screenshot: Claude liest Spiele, Tipps und Quoten vom Bild,
wir vergleichen mit den fairen Quoten (Pinnacle) und lernen daraus, wie viel Sporttip
im Schnitt unter fair zahlt (pro Tipp-Art). So kann der Bot Sporttip-Quoten später schätzen.
"""

from __future__ import annotations

import base64
import logging
import statistics
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import Engine

from fussball.agents.scout import FALLBACK_BETA, MODEL, _cost
from fussball.data.db import session_scope
from fussball.data.schema import AppSetting, utcnow
from fussball.models.builder import Part, joint_prob

log = logging.getLogger(__name__)
OBS_KEY = "sporttip_obs"
MAX_OBS = 1000
MIN_OBS = 3  # ab so vielen Beobachtungen pro Tipp-Art wird geschätzt


class SlipPart(BaseModel):
    market: Literal["1X2", "DC", "OU", "BTTS", "TEAM_OU", "HCP", "CS", "andere"] = Field(
        description="1X2 = Sieg/Unentschieden · DC = doppelte Chance · OU = Über/Unter Tore gesamt · "
                    "BTTS = beide Teams treffen · TEAM_OU = Über/Unter Tore eines Teams · HCP = Handicap (3-Weg) · "
                    "CS = genaues Resultat · andere = alles andere (Torschützen, Ecken, Karten, Gerade/Ungerade …)")
    selection: str = Field(description="1X2/HCP: H, D oder A · DC: 1X, X2 oder 12 · OU/TEAM_OU: O oder U · "
                                       "BTTS: Y oder N · CS: z. B. '2:1' (Heim:Gast) · andere: leer")
    line: float | None = Field(description="OU/TEAM_OU: Torlinie (2.5). HCP: Heim-Vorgabe minus Gast-Vorgabe "
                                           "(Sporttip 'Handicap 0:1' → -1, '1:0' → 1, '0:2' → -2). Sonst null")
    team: Literal["home", "away"] | None = Field(description="Nur bei TEAM_OU: welches Team")
    half: Literal["ft", "1h"] = Field(description="ft = ganzes Spiel, 1h = nur 1. Halbzeit")


class SlipLeg(BaseModel):
    home: str = Field(description="Heimteam wie auf dem Bild")
    away: str = Field(description="Gastteam wie auf dem Bild")
    market_text: str = Field(description="Tipp-Text wie auf dem Bild (bei BetBuilder alle Teile mit ' + ')")
    parts: list[SlipPart] = Field(description="Ein Teil bei normaler Wette; bei BetBuilder alle Teil-Tipps")
    odds: float = Field(description="Quote dieser Wette (bei BetBuilder die BetBuilder-Quote) als Dezimalzahl")
    boosted: bool = Field(description="True, wenn die Quote als erhöht/Boost/Prämie markiert ist")
    match_id: int | None = Field(description="ID des passenden Spiels aus der mitgelieferten Liste "
                                             "(Teamnamen können auf Deutsch/Englisch abweichen), sonst null")


class Slip(BaseModel):
    is_betting_slip: bool = Field(description="True, wenn das Bild Sportwetten mit Quoten zeigt")
    bookmaker: Literal["Sporttip", "Bet365", "andere"] = Field(
        description="Von welchem Anbieter der Screenshot ist (Logo/Farben: Sporttip/Swisslos rot, Bet365 grün-gelb)")
    legs: list[SlipLeg]
    total_odds: float | None = Field(description="Gesamtquote der Kombi auf dem Wettschein, falls angezeigt")


PROMPT = """Das Bild ist ein Screenshot von Sporttip (Swisslos) oder Bet365: ein Wettschein, eine Spielseite
mit Quoten, ein BetBuilder oder eine Prämie/Boost. Lies jede Wette ab: Heimteam, Gastteam, Tipp, Quote.
Begriffe: "Endergebnis 1/X/2" bzw. "Spielergebnis" = 1X2; "Mehr als / Weniger als" = Über / Unter; "1X", "X2", "12" = DC; "Over/Under Tore" = OU;
"Erzielen beide Teams ein Tor? Ja/Nein" = BTTS; "Team 1/2 Over/Under" = TEAM_OU; "Handicap 0:1" = HCP mit
line -1; "Resultat 2:1" = CS; "1. Halbzeit - …" = gleicher Markt mit half 1h.
Ein BetBuilder (mehrere Tipps im selben Spiel mit EINER Quote) ist EINE Wette mit mehreren parts.
Zeigt eine Spielseite viele Quoten, nimm jede sichtbare Quote als eigene Wette (ein part). Markiere
erhöhte Quoten (Boost, Prämie, durchgestrichene alte Quote) mit boosted. Dezimalquoten mit Punkt.
Erfinde nichts: was nicht lesbar ist, lässt du weg."""


def read_slip(client, image: bytes, media_type: str = "image/jpeg", matches: list[str] | None = None,
              model: str = MODEL) -> tuple[Slip, float]:
    """`matches`: Zeilen "ID: Heim – Gast (Datum)" der kommenden Spiele zum Zuordnen."""
    listing = ("\n\nKommende Spiele in unserer Datenbank (ID: Heim – Gast, Anstoss):\n" + "\n".join(matches)
               if matches else "")
    resp = client.beta.messages.parse(
        model=model, max_tokens=8000, output_format=Slip, output_config={"effort": "low"},
        betas=[FALLBACK_BETA], fallbacks="default",
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": PROMPT + listing}]}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("Wettschein konnte nicht gelesen werden")
    return resp.parsed_output, _cost(model, resp.usage, 0)


def to_parts(leg: SlipLeg) -> list[Part] | None:
    """Wette → Teile für den BetBuilder-Rechner; None, wenn ein Teil nicht berechenbar ist."""
    out = []
    for p in leg.parts:
        if p.market == "andere":
            return None
        if p.market in ("OU", "TEAM_OU", "HCP") and p.line is None:
            return None
        market = ("HOME" if p.team != "away" else "AWAY") if p.market == "TEAM_OU" else p.market
        out.append(Part(market, p.selection, float(p.line or 0.0), p.half))
    return out or None


def market_key(parts: list[Part]) -> str:
    """Lernschlüssel: Einzelwette nach Markt (z. B. 'OU2.5', '1X2', '1h:OU1.5'), BetBuilder = 'BB'."""
    if len(parts) > 1:
        return "BB"
    p = parts[0]
    key = p.market + (f"{p.line:g}" if p.market in ("OU", "HOME", "AWAY", "HCP") else "")
    return f"1h:{key}" if p.half == "1h" else key


def family(key: str) -> str:
    if key == "BB":
        return "BB"
    half = "1h:" if key.startswith("1h:") else ""
    base = key.removeprefix("1h:")
    for prefix in ("OU", "HOME", "AWAY", "HCP"):
        if base.startswith(prefix):
            return half + ("TEAM" if prefix in ("HOME", "AWAY") else prefix)
    return half + base


def _find_forecast(forecasts, home: str, away: str):
    from fussball.data.odds_api import similarity

    best, score = None, 0.0
    for f in forecasts:
        s = min(similarity(home, f.home), similarity(away, f.away))
        if s > score:
            best, score = f, s
    return best if score >= 0.75 else None


def evaluate(slip: Slip, forecasts) -> list[dict]:
    """Pro Wette: faire Wahrscheinlichkeit (exakt aus der Pinnacle-Torverteilung, auch BetBuilder,
    Handicap und 1. Halbzeit) und Verhältnis Sporttip-Quote / faire Quote."""
    out = []
    for leg in slip.legs:
        row = {"leg": leg, "match": f"{leg.home} – {leg.away}", "fair": None, "ratio": None, "prob": None}
        f = next((f for f in forecasts if f.match_id == leg.match_id), None) \
            or _find_forecast(forecasts, leg.home, leg.away)
        parts = to_parts(leg)
        if f is not None:
            row.update(match=f"{f.home} – {f.away}", match_id=f.match_id, kickoff=f.kickoff_utc.isoformat(),
                       comp=f.comp_name or f.comp)
            if parts and f.implied_rates:
                p = joint_prob(*f.implied_rates, parts)
                if p > 0.001:
                    row.update(prob=p, fair=1 / p, ratio=leg.odds * p, key=market_key(parts),
                               match_id=f.match_id, builder=len(parts) > 1)
        out.append(row)
    return out


def remember(engine: Engine, rows: list[dict], book: str = "Sporttip") -> int:
    """Beobachtungen (Anbieter-Quote / faire Quote) speichern, je Anbieter getrennt; gleiche Wette nur einmal."""
    # Boosts nicht lernen: sie sind absichtlich erhöht und verfälschen die normale Marge des Anbieters
    new = [{"match_id": r["match_id"], "key": r["key"], "sel": "+".join(p.selection for p in r["leg"].parts),
            "odds": r["leg"].odds, "fair": r["fair"], "at": utcnow().isoformat(), "book": book}
           for r in rows if r.get("ratio") and r.get("key") and not r["leg"].boosted]
    with session_scope(engine) as s:
        row = s.get(AppSetting, OBS_KEY)
        obs = list(row.value) if row and isinstance(row.value, list) else []
        key = lambda o: (o.get("book", "Sporttip"), o["match_id"], o["key"], o["sel"], round(o["odds"], 2))  # noqa: E731
        seen = {key(o) for o in obs}
        added = [o for o in new if key(o) not in seen]
        obs = (obs + added)[-MAX_OBS:]
        if row:
            row.value = obs
        else:
            s.add(AppSetting(key=OBS_KEY, value=obs))
    return len(added)


def ratios(engine: Engine, book: str = "Sporttip") -> dict[str, dict]:
    """Median von Anbieter-Quote / fairer Quote pro Tipp-Art (1X2, DC, OU, BTTS, TEAM, BB …)."""
    with session_scope(engine) as s:
        row = s.get(AppSetting, OBS_KEY)
        obs = list(row.value) if row and isinstance(row.value, list) else []
    by: dict[str, list[float]] = {}
    for o in obs:
        if o.get("book", "Sporttip") == book:
            by.setdefault(family(o["key"]), []).append(o["odds"] / o["fair"])
    return {k: {"ratio": statistics.median(v), "n": len(v)} for k, v in by.items() if len(v) >= MIN_OBS}


def estimate(ratio_table: dict, market: str, fair_odds: float) -> float | None:
    """Geschätzte Sporttip-Quote für einen Tipp (market wie 'OU2.5', 'DC', '1X2', 'BTTS')."""
    r = ratio_table.get(family(market))
    return max(1.01, round(fair_odds * r["ratio"], 2)) if r else None
