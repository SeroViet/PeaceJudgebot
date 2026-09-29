"""Live-Quoten von The Odds API (https://the-odds-api.com), inkl. Pinnacle.

Pinnacle ist die Referenz für faire Wahrscheinlichkeiten; ohne Pinnacle-Quote gibt
die App im Markt-Value-Modus keinen Tipp. Free-Plan: 500 Credits/Monat.
Kosten pro Abruf = Anzahl Märkte × Anzahl Regionen (hier 2 × 1 = 2 Credits je Liga).

Key: Umgebungsvariable ODDS_API_KEY.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from fussball.data.db import upsert
from fussball.data.schema import Competition, Match, Odds, Team, TeamAlias, utcnow

log = logging.getLogger(__name__)

SOURCE = "odds-api"
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {
    "E0": "soccer_epl", "E1": "soccer_efl_champ",
    "D1": "soccer_germany_bundesliga", "D2": "soccer_germany_bundesliga2",
    "I1": "soccer_italy_serie_a", "I2": "soccer_italy_serie_b",
    "SP1": "soccer_spain_la_liga", "SP2": "soccer_spain_segunda_division",
    "F1": "soccer_france_ligue_one", "F2": "soccer_france_ligue_two",
    "N1": "soccer_netherlands_eredivisie", "P1": "soccer_portugal_primeira_liga",
    "B1": "soccer_belgium_first_div", "T1": "soccer_turkey_super_league",
    "SC0": "soccer_spl", "G1": "soccer_greece_super_league",
}  # fmt: skip
BOOKMAKER_CODES = {
    "pinnacle": "PS", "betfair_ex_eu": "BFE", "betfair_ex_uk": "BFE", "williamhill": "WH", "onexbet": "1XB",
    "unibet_eu": "UNI", "marathonbet": "MAR", "sport888": "888", "betclic": "BETC", "betsson": "BTSS",
    "nordicbet": "NORD", "coolbet": "COOL", "tipico_de": "TIP", "everygame": "EVG", "betonlineag": "BOL",
    "matchbook": "MBK", "gtbets": "GTB", "leovegas": "LEO", "suprabets": "SUP", "mybookieag": "MYB",
}  # fmt: skip
EXCHANGES = {"BFE", "MBK"}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|sc|ac|as|ss|us|sv|vfb|vfl|tsg|fsv|afc|rc|ogc|olympique|real|club|de|calcio|1\.)\b", " ", s)
    s = s.replace("&", "and").replace("munchen", "munich").replace("manchester", "man").replace("united", "utd")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb or na in nb or nb in na:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    return max(SequenceMatcher(None, na, nb).ratio(), jacc)


class OddsApiClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.getenv("ODDS_API_KEY")
        self.session = session or requests.Session()
        self.remaining: int | None = None
        self.used: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def odds(self, sport_key: str, regions: str = "eu", markets: str = "h2h,totals") -> list[dict]:
        resp = self.session.get(
            f"{BASE_URL}/sports/{sport_key}/odds",
            params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
            timeout=30,
        )
        self.remaining = int(resp.headers.get("x-requests-remaining", self.remaining or 0) or 0)
        self.used = int(resp.headers.get("x-requests-used", self.used or 0) or 0)
        resp.raise_for_status()
        return resp.json()


def event_rows(event: dict, fetched_at: datetime) -> list[dict]:
    """Quoten eines Events als Odds-Zeilen (ohne match_id)."""
    home, away = event["home_team"], event["away_team"]
    rows = []
    for bm in event.get("bookmakers", []):
        code = BOOKMAKER_CODES.get(bm["key"], bm["key"].upper()[:12])
        for market in bm.get("markets", []):
            for o in market.get("outcomes", []):
                if market["key"] == "h2h":
                    sel = "H" if o["name"] == home else "A" if o["name"] == away else "D" if o["name"] == "Draw" else None
                    line = 0.0
                    mk = "1X2"
                elif market["key"] == "totals" and float(o.get("point", 0)) == 2.5:
                    sel = "O" if o["name"] == "Over" else "U" if o["name"] == "Under" else None
                    line, mk = 2.5, "OU"
                else:
                    continue
                if sel is None or not o.get("price") or o["price"] <= 1.0:
                    continue
                rows.append({"bookmaker": code, "market": mk, "line": line, "selection": sel,
                             "price": float(o["price"]), "is_closing": False, "known_at": fetched_at})
    return rows


def match_event(session: Session, comp_id: int, event: dict, window_h: int = 36) -> int | None:
    """Ordnet ein Event einem angesetzten Spiel zu (Teamnamen + Anstosszeit)."""
    kickoff = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00")).replace(tzinfo=None)
    cands = session.execute(
        select(Match.id, Match.home_team_id, Match.away_team_id)
        .where(Match.competition_id == comp_id,
               Match.kickoff_utc.between(kickoff - timedelta(hours=window_h), kickoff + timedelta(hours=window_h)))
    ).all()
    names = dict(session.execute(select(Team.id, Team.name)).all())
    alias = dict(session.execute(select(TeamAlias.alias, TeamAlias.team_id).where(TeamAlias.source == SOURCE)).all())

    def score(api_name: str, team_id: int) -> float:
        if api_name in alias:
            return 1.0 if alias[api_name] == team_id else 0.0
        return similarity(api_name, names[team_id])

    best, best_score = None, 0.0
    for mid, home_id, away_id in cands:
        sc = min(score(event["home_team"], home_id), score(event["away_team"], away_id))
        if sc > best_score:
            best, best_score = (mid, home_id, away_id), sc
    if best is None or best_score < 0.6:
        return _create_match(session, comp_id, event, kickoff, names, score)
    mid, home_id, away_id = best
    upsert(session, TeamAlias, [{"team_id": home_id, "source": SOURCE, "alias": event["home_team"]},
                                {"team_id": away_id, "source": SOURCE, "alias": event["away_team"]}],
           ["source", "alias"], update_cols=[])
    return mid


def _create_match(session: Session, comp_id: int, event: dict, kickoff: datetime, names: dict, score) -> int | None:
    """Spiel ist noch nicht in fixtures.csv: anhand bekannter Teams der Liga anlegen."""
    from fussball.cli import current_season_code
    from fussball.data.football_data import season_label

    recent = kickoff - timedelta(days=400)
    team_ids = {tid for pair in session.execute(
        select(Match.home_team_id, Match.away_team_id).where(Match.competition_id == comp_id,
                                                             Match.kickoff_utc >= recent)).all() for tid in pair}
    def pick(api_name):
        ranked = sorted(((score(api_name, t), t) for t in team_ids), reverse=True)
        return ranked[0][1] if ranked and ranked[0][0] >= 0.8 else None

    home_id, away_id = pick(event["home_team"]), pick(event["away_team"])
    if home_id is None or away_id is None or home_id == away_id:
        log.info("Kein Spiel/Team gefunden für %s – %s (%s)", event["home_team"], event["away_team"], kickoff)
        return None
    season = season_label(current_season_code(kickoff.date()))
    now = utcnow()
    upsert(session, Match, [{"competition_id": comp_id, "season": season, "kickoff_utc": kickoff,
                             "kickoff_time_known": True, "home_team_id": home_id, "away_team_id": away_id,
                             "status": "scheduled", "known_at": kickoff + timedelta(hours=2, minutes=15),
                             "source": SOURCE, "created_at": now, "updated_at": now}],
           ["competition_id", "season", "home_team_id", "away_team_id"], update_cols=["kickoff_utc", "updated_at"])
    mid = session.execute(select(Match.id).where(Match.competition_id == comp_id, Match.season == season,
                                                 Match.home_team_id == home_id, Match.away_team_id == away_id)
                          ).scalar_one()
    upsert(session, TeamAlias, [{"team_id": home_id, "source": SOURCE, "alias": event["home_team"]},
                                {"team_id": away_id, "source": SOURCE, "alias": event["away_team"]}],
           ["source", "alias"], update_cols=[])
    return mid


def import_odds(session: Session, client: OddsApiClient, league_codes: list[str]) -> dict[str, int]:
    fetched_at = utcnow()
    out = {}
    for code in league_codes:
        sport = SPORT_KEYS.get(code)
        comp_id = session.execute(select(Competition.id).where(Competition.code == code)).scalar_one_or_none()
        if not sport or comp_id is None:
            continue
        try:
            events = client.odds(sport)
        except requests.HTTPError as exc:
            log.warning("Odds API %s: %s", code, exc)
            out[code] = -1
            continue
        rows = []
        for ev in events:
            mid = match_event(session, comp_id, ev)
            if mid is not None:
                rows += [{**r, "match_id": mid, "source": SOURCE} for r in event_rows(ev, fetched_at)]
        upsert(session, Odds, rows, ["match_id", "bookmaker", "market", "line", "selection", "is_closing", "source"],
               update_cols=["price", "known_at"])
        out[code] = len(rows)
    log.info("Odds API: %s, verbleibende Credits %s", out, client.remaining)
    return out
