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

log = logging.getLogger(__name__)
OBS_KEY = "sporttip_obs"
MAX_OBS = 1000
MIN_OBS = 3  # ab so vielen Beobachtungen pro Tipp-Art wird geschätzt


class SlipLeg(BaseModel):
    home: str = Field(description="Heimteam wie auf dem Bild")
    away: str = Field(description="Gastteam wie auf dem Bild")
    market_text: str = Field(description="Tipp-Text wie auf dem Bild, z. B. 'Total Tore Über 2.5' oder '1X'")
    market: Literal["1X2", "DC", "OU", "BTTS", "andere"] = Field(
        description="1X2 = Sieg/Unentschieden, DC = doppelte Chance, OU = Über/Unter Tore gesamt, "
                    "BTTS = beide Teams treffen, andere = alles andere (Handicap, Halbzeit, Teamtore …)")
    selection: str = Field(description="1X2: H, D oder A · DC: 1X, X2 oder 12 · OU: O oder U · BTTS: Y oder N · "
                                       "andere: leer")
    line: float | None = Field(description="Bei OU die Torlinie (z. B. 2.5), sonst null")
    odds: float = Field(description="Quote dieses Tipps als Dezimalzahl")
    match_id: int | None = Field(description="ID des passenden Spiels aus der mitgelieferten Liste "
                                             "(Teamnamen können auf Deutsch/Englisch abweichen), sonst null")


class Slip(BaseModel):
    is_betting_slip: bool = Field(description="True, wenn das Bild Sportwetten mit Quoten zeigt")
    legs: list[SlipLeg]
    total_odds: float | None = Field(description="Gesamtquote der Kombi, falls angezeigt")


PROMPT = """Das Bild ist ein Screenshot von Sporttip (Schweizer Sportwetten) – ein Wettschein oder eine
Spielliste mit Quoten. Lies jede Wette ab: Heimteam, Gastteam, Tipp und Quote. Ordne jeden Tipp einer
Kategorie zu. Sporttip-Begriffe: "1"/"X"/"2" = Sieg Heim/Unentschieden/Sieg Gast; "1X", "X2", "12" =
doppelte Chance; "Über/Unter x.5" bei Toren gesamt = OU; "Beide Teams treffen Ja/Nein" = BTTS.
Handicap, Halbzeit, Teamtore, Ecken usw. = andere. Wenn eine Spielliste mehrere Quoten pro Spiel zeigt,
nimm jede sichtbare Quote als eigene Wette. Schreibe Dezimalquoten mit Punkt. Erfinde nichts: was nicht
lesbar ist, lässt du weg."""


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


def market_key(leg: SlipLeg) -> str | None:
    if leg.market == "OU":
        return f"OU{leg.line}" if leg.line is not None else None
    return None if leg.market == "andere" else leg.market


def family(key: str) -> str:
    return "OU" if key.startswith("OU") else key


def _find_forecast(forecasts, home: str, away: str):
    from fussball.data.odds_api import similarity

    best, score = None, 0.0
    for f in forecasts:
        s = min(similarity(home, f.home), similarity(away, f.away))
        if s > score:
            best, score = f, s
    return best if score >= 0.75 else None


def evaluate(slip: Slip, forecasts) -> list[dict]:
    """Pro Wette: faire Wahrscheinlichkeit (Pinnacle-Markt) und Verhältnis Sporttip/fair."""
    out = []
    for leg in slip.legs:
        row = {"leg": leg, "match": f"{leg.home} – {leg.away}", "fair": None, "ratio": None, "prob": None}
        key = market_key(leg)
        f = next((f for f in forecasts if f.match_id == leg.match_id), None) \
            or _find_forecast(forecasts, leg.home, leg.away)
        if f is not None:
            row["match"] = f"{f.home} – {f.away}"
            p = ((f.implied or {}).get(key) or {}).get(leg.selection) if key else None
            if p:
                row.update(prob=p, fair=1 / p, ratio=leg.odds * p, key=key, match_id=f.match_id)
        out.append(row)
    return out


def remember(engine: Engine, rows: list[dict]) -> int:
    """Beobachtungen (Sporttip-Quote / faire Quote) speichern; gleiche Wette nur einmal."""
    new = [{"match_id": r["match_id"], "key": r["key"], "sel": r["leg"].selection, "odds": r["leg"].odds,
            "fair": r["fair"], "at": utcnow().isoformat()} for r in rows if r.get("ratio")]
    with session_scope(engine) as s:
        row = s.get(AppSetting, OBS_KEY)
        obs = list(row.value) if row and isinstance(row.value, list) else []
        seen = {(o["match_id"], o["key"], o["sel"], round(o["odds"], 2)) for o in obs}
        added = [o for o in new if (o["match_id"], o["key"], o["sel"], round(o["odds"], 2)) not in seen]
        obs = (obs + added)[-MAX_OBS:]
        if row:
            row.value = obs
        else:
            s.add(AppSetting(key=OBS_KEY, value=obs))
    return len(added)


def ratios(engine: Engine) -> dict[str, dict]:
    """Median von Sporttip-Quote / fairer Quote pro Tipp-Art (1X2, DC, OU, BTTS)."""
    with session_scope(engine) as s:
        row = s.get(AppSetting, OBS_KEY)
        obs = list(row.value) if row and isinstance(row.value, list) else []
    by: dict[str, list[float]] = {}
    for o in obs:
        by.setdefault(family(o["key"]), []).append(o["odds"] / o["fair"])
    return {k: {"ratio": statistics.median(v), "n": len(v)} for k, v in by.items() if len(v) >= MIN_OBS}


def estimate(ratio_table: dict, market: str, fair_odds: float) -> float | None:
    """Geschätzte Sporttip-Quote für einen Tipp (market wie 'OU2.5', 'DC', '1X2', 'BTTS')."""
    r = ratio_table.get(family(market))
    return max(1.01, round(fair_odds * r["ratio"], 2)) if r else None
