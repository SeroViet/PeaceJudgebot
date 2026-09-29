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

# Feste Zuordnung (The Odds API → football-data) für Namen, die per Ähnlichkeit
# mehrdeutig oder falsch wären (z. B. "Inter Milan" ≠ "Milan", "Atlético" ≠ "Real Madrid").
TEAM_ALIASES = {
    "Inter Milan": "Inter", "AC Milan": "Milan", "Atlético Madrid": "Ath Madrid", "Atletico Madrid": "Ath Madrid",
    "Athletic Bilbao": "Ath Bilbao", "Paris Saint Germain": "Paris SG", "Paris FC": "Paris FC",
    "Borussia Monchengladbach": "M'gladbach", "Eintracht Frankfurt": "Ein Frankfurt",
    "Nottingham Forest": "Nott'm Forest", "Espanyol": "Espanol", "Wolverhampton Wanderers": "Wolves",
    "West Bromwich Albion": "West Brom", "Queens Park Rangers": "QPR", "Sheffield Wednesday": "Sheffield Weds",
    "Saint Etienne": "St Etienne", "FC St. Pauli": "St Pauli", "1. FC Heidenheim": "Heidenheim",
    "Hellas Verona": "Verona", "Sporting Gijón": "Sp Gijon", "Real Valladolid": "Valladolid",
    "Leganés": "Leganes", "Real Sociedad": "Sociedad", "Real Betis": "Betis", "Manchester City": "Man City",
    "Manchester United": "Man United", "Real Madrid": "Real Madrid",
}
MIN_SCORE = 0.8
MIN_MARGIN = 0.1  # Abstand zum zweitbesten Kandidaten, sonst gilt der Name als mehrdeutig


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|sc|ac|as|ss|us|sv|vfb|vfl|tsg|fsv|afc|rc|ogc|olympique|real|club|de|calcio|1\.)\b", " ", s)
    s = s.replace("&", "and").replace("munchen", "munich").replace("manchester", "man").replace("united", "utd")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    """1.0 nur bei identischem Namen (nach Normalisierung); Teilmengen der Wörter 0.9."""
    if TEAM_ALIASES.get(a) is not None:
        return 1.0 if TEAM_ALIASES[a] == b else 0.0
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:
        return 0.9
    jacc = len(ta & tb) / len(ta | tb)
    return min(0.89, max(SequenceMatcher(None, na, nb).ratio(), jacc))


def best_team(api_name: str, candidates: dict[int, str]) -> int | None:
    """Eindeutig bester Kandidat oder None (zu unähnlich bzw. mehrdeutig)."""
    ranked = sorted(((similarity(api_name, n), tid) for tid, n in candidates.items()), reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_MARGIN:
        return None
    return ranked[0][1]


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
                elif market["key"] == "totals" and o.get("point") is not None:
                    sel = "O" if o["name"] == "Over" else "U" if o["name"] == "Under" else None
                    line, mk = float(o["point"]), "OU"
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

    league_teams = _league_teams(session, comp_id, kickoff)

    def resolve(api_name: str) -> int | None:
        if api_name in TEAM_ALIASES:  # feste Zuordnung hat Vorrang
            return next((t for t in league_teams if names[t] == TEAM_ALIASES[api_name]), None)
        if api_name in alias:
            return alias[api_name]
        return best_team(api_name, {t: names[t] for t in league_teams})

    home_id, away_id = resolve(event["home_team"]), resolve(event["away_team"])
    if home_id is None or away_id is None or home_id == away_id:
        log.warning("Team nicht eindeutig zuordenbar: %s – %s", event["home_team"], event["away_team"])
        return None
    best = next(((mid, h, a) for mid, h, a in cands if h == home_id and a == away_id), None)
    if best is None:
        return _create_match(session, comp_id, event, kickoff, home_id, away_id)
    mid, home_id, away_id = best
    upsert(session, TeamAlias, [{"team_id": home_id, "source": SOURCE, "alias": event["home_team"]},
                                {"team_id": away_id, "source": SOURCE, "alias": event["away_team"]}],
           ["source", "alias"], update_cols=[])
    return mid


def _league_teams(session: Session, comp_id: int, kickoff: datetime) -> set[int]:
    recent = kickoff - timedelta(days=400)
    return {tid for pair in session.execute(
        select(Match.home_team_id, Match.away_team_id).where(Match.competition_id == comp_id,
                                                             Match.kickoff_utc >= recent)).all() for tid in pair}


def _create_match(session: Session, comp_id: int, event: dict, kickoff: datetime, home_id: int,
                  away_id: int) -> int | None:
    """Spiel ist noch nicht in fixtures.csv: mit den eindeutig zugeordneten Teams anlegen."""
    from fussball.cli import current_season_code
    from fussball.data.football_data import season_label

    season = season_label(current_season_code(kickoff.date()))
    existing = session.execute(select(Match).where(Match.competition_id == comp_id, Match.season == season,
                                                   Match.home_team_id == home_id, Match.away_team_id == away_id)
                               ).scalar_one_or_none()
    if existing is not None and existing.status == "finished":
        log.warning("Paarung %s – %s ist diese Saison schon gespielt; Event ignoriert",
                    event["home_team"], event["away_team"])
        return None
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


# ----------------------------------------------------------------------------- weltweiter Modus
# Ligen ausserhalb der football-data-Historie: Teams/Spiele werden aus The Odds API angelegt.
# Für Sicher-Tipps und Tageskombis reicht die Pinnacle-Quote (markt-implizite Torverteilung).

def _get(client: OddsApiClient, path: str, **params) -> tuple[list | dict, dict]:
    resp = client.session.get(f"{BASE_URL}/{path}", params={"apiKey": client.api_key, **params}, timeout=30)
    if "x-requests-remaining" in resp.headers:
        client.remaining = int(resp.headers["x-requests-remaining"])
    resp.raise_for_status()
    return resp.json(), resp.headers


def soccer_sports(client: OddsApiClient) -> list[dict]:
    """Alle aktiven Fussball-Wettbewerbe (kostenlos)."""
    data, _ = _get(client, "sports")
    return [s for s in data if s.get("group") == "Soccer" and not s.get("has_outrights")]


def upcoming_counts(client: OddsApiClient, hours: float = 30.0) -> list[tuple[str, str, int]]:
    """(sport_key, Titel, Anzahl Spiele in den nächsten `hours` Stunden) – kostenlos."""
    now = utcnow()
    out = []
    for s in soccer_sports(client):
        try:
            events, _ = _get(client, f"sports/{s['key']}/events")
        except requests.HTTPError:
            continue
        n = sum(1 for e in events if now < datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                .replace(tzinfo=None) <= now + timedelta(hours=hours))
        if n:
            out.append((s["key"], s["title"], n))
    return sorted(out, key=lambda x: -x[2])


def _competition(session: Session, sport_key: str, title: str) -> int:
    code = next((c for c, k in SPORT_KEYS.items() if k == sport_key), sport_key[:32])
    now = utcnow()
    kind = "international" if any(w in sport_key for w in ("nations", "world_cup", "euro", "uefa_champs", "fifa")) \
        else "league"
    upsert(session, Competition, [{"code": code, "name": title, "kind": kind, "created_at": now, "updated_at": now}],
           ["code"], update_cols=[])
    return session.execute(select(Competition.id).where(Competition.code == code)).scalar_one()


def _team(session: Session, name: str) -> int:
    alias = session.execute(select(TeamAlias.team_id).where(TeamAlias.source == SOURCE, TeamAlias.alias == name)
                            ).scalar_one_or_none()
    if alias is not None:
        return alias
    now = utcnow()
    upsert(session, Team, [{"name": name, "created_at": now, "updated_at": now}], ["name"], update_cols=[])
    tid = session.execute(select(Team.id).where(Team.name == name)).scalar_one()
    upsert(session, TeamAlias, [{"team_id": tid, "source": SOURCE, "alias": name}], ["source", "alias"], update_cols=[])
    return tid


def import_generic(session: Session, client: OddsApiClient, sport_key: str, title: str) -> int:
    """Quoten eines beliebigen Wettbewerbs; Spiele/Teams werden bei Bedarf angelegt (2 Credits)."""
    from fussball.cli import current_season_code
    from fussball.data.football_data import season_label

    if sport_key in SPORT_KEYS.values():  # Top-Ligen: Zuordnung zu football-data-Teams
        code = next(c for c, k in SPORT_KEYS.items() if k == sport_key)
        return import_odds(session, client, [code]).get(code, 0)
    comp_id = _competition(session, sport_key, title)
    fetched_at = utcnow()
    rows = []
    for ev in client.odds(sport_key):
        kickoff = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).replace(tzinfo=None)
        home, away = _team(session, ev["home_team"]), _team(session, ev["away_team"])
        season = season_label(current_season_code(kickoff.date()))
        now = utcnow()
        upsert(session, Match, [{"competition_id": comp_id, "season": season, "kickoff_utc": kickoff,
                                 "kickoff_time_known": True, "home_team_id": home, "away_team_id": away,
                                 "status": "scheduled", "known_at": kickoff + timedelta(hours=2, minutes=15),
                                 "neutral_venue": False, "source": SOURCE, "created_at": now, "updated_at": now}],
               ["competition_id", "season", "home_team_id", "away_team_id"], update_cols=["kickoff_utc", "updated_at"])
        mid = session.execute(select(Match.id).where(Match.competition_id == comp_id, Match.season == season,
                                                     Match.home_team_id == home, Match.away_team_id == away)
                              ).scalar_one()
        rows += [{**r, "match_id": mid, "source": SOURCE} for r in event_rows(ev, fetched_at)]
    upsert(session, Odds, rows, ["match_id", "bookmaker", "market", "line", "selection", "is_closing", "source"],
           update_cols=["price", "known_at"])
    return len(rows)


def import_scores(session: Session, client: OddsApiClient, sport_key: str, days_from: int = 2) -> int:
    """Endstände der letzten Tage (2 Credits) → Spiele auf 'finished' setzen."""
    data, _ = _get(client, f"sports/{sport_key}/scores", daysFrom=days_from)
    n = 0
    for ev in data:
        if not ev.get("completed") or not ev.get("scores"):
            continue
        score = {s["name"]: int(s["score"]) for s in ev["scores"]}
        if ev["home_team"] not in score or ev["away_team"] not in score:
            continue
        ids = [session.execute(select(TeamAlias.team_id).where(TeamAlias.source == SOURCE, TeamAlias.alias == t))
               .scalar_one_or_none() for t in (ev["home_team"], ev["away_team"])]
        if None in ids:
            continue
        kickoff = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).replace(tzinfo=None)
        m = session.execute(select(Match).where(Match.home_team_id == ids[0], Match.away_team_id == ids[1],
                                                Match.kickoff_utc.between(kickoff - timedelta(hours=36),
                                                                          kickoff + timedelta(hours=36)))).scalar_one_or_none()
        if m is None or m.status == "finished":
            continue
        m.ft_home, m.ft_away = score[ev["home_team"]], score[ev["away_team"]]
        m.status, m.known_at = "finished", utcnow()
        n += 1
    return n
