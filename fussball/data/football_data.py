"""Import von Ergebnissen, Spielstatistiken und Quoten von football-data.co.uk.

Nutzung: Die Daten sind frei für private Analysen; siehe
https://www.football-data.co.uk/ (Quelle bei Weitergabe nennen).

Zeitstempel "bekannt ab" (Data-Leakage-Schutz):
- Ergebnis/Statistik: Anpfiff + 2h15 (ohne bekannte Anstosszeit: Ende des Spieltags).
- Vorab-Quoten ("pre-closing"): football-data sammelt sie laut eigenen Notes
  freitagnachmittags (Wochenend- und Montagsspiele) bzw. dienstagnachmittags
  (Spiele Di–Do). Wir setzen known_at = min(Sammelzeitpunkt, Anpfiff − 1h).
- Schlussquoten ("closing", Spalten mit C): known_at = Anpfiff. Sie dienen nur
  zur CLV-Messung und sind vor Anpfiff nie verfügbar.
Alle Zeitangaben in den CSVs sind britische Ortszeit und werden nach UTC umgerechnet.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from urllib3.util.retry import Retry

from fussball.data.db import upsert
from fussball.data.schema import Competition, IngestLog, Match, Odds, Team, TeamAlias, utcnow

log = logging.getLogger(__name__)

SOURCE = "football-data"
BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
UK = ZoneInfo("Europe/London")
DEFAULT_KICKOFF = time(15, 0)
RESULT_DELAY = timedelta(hours=2, minutes=15)
PRE_CLOSING_COLLECTION = time(16, 0)

BOOKMAKERS = {
    "B365": "Bet365",
    "BW": "bwin",
    "BF": "Betfair Sportsbook",
    "BFD": "Betfred",
    "BFE": "Betfair Exchange",
    "BV": "BetVictor",
    "PS": "Pinnacle",
    "WH": "William Hill",
    "1XB": "1xBet",
    "PP": "Paddy Power",
    "SKB": "Sky Bet",
    "IW": "Interwetten",
    "VC": "VC Bet",
    "LB": "Ladbrokes",
    "GB": "Gamebookers",
    "SB": "Sportingbet",
    "SJ": "Stan James",
    "BS": "Blue Square",
    "CL": "Coral",
    "Max": "Marktmaximum",
    "Avg": "Marktdurchschnitt",
    "BbMx": "Betbrain Maximum",
    "BbAv": "Betbrain Durchschnitt",
}
# Bei Über/Unter und Asian Handicap kürzt football-data Pinnacle mit "P" ab.
_ALIASES = {"P": "PS"}

_codes = "|".join(sorted(map(re.escape, [*BOOKMAKERS, *_ALIASES]), key=len, reverse=True))
RE_1X2 = re.compile(rf"^({_codes})(C?)([HDA])$")
RE_OU = re.compile(rf"^({_codes})(C?)([<>])2\.5$")
RE_AH = re.compile(rf"^({_codes})(C?)AH([HA])$")

STAT_COLUMNS = {
    "HTHG": "ht_home", "HTAG": "ht_away",
    "HxG": "xg_home", "AxG": "xg_away",
    "HS": "shots_home", "AS": "shots_away",
    "HST": "shots_on_target_home", "AST": "shots_on_target_away",
    "HF": "fouls_home", "AF": "fouls_away",
    "HC": "corners_home", "AC": "corners_away",
    "HY": "yellow_home", "AY": "yellow_away",
    "HR": "red_home", "AR": "red_away",
}  # fmt: skip
FLOAT_STATS = {"xg_home", "xg_away"}


def season_label(code: str) -> str:
    """'2425' -> '2024-25', '9900' -> '1999-00'."""
    start = int(code[:2])
    century = 1900 if start >= 90 else 2000
    return f"{century + start}-{code[2:]}"


def to_utc_naive(local: datetime) -> datetime:
    return local.replace(tzinfo=UK).astimezone(timezone.utc).replace(tzinfo=None)


def pre_closing_known_at(kickoff_utc: datetime) -> datetime:
    """Konservativer Zeitpunkt, ab dem die Vorab-Quoten öffentlich waren."""
    local = kickoff_utc.replace(tzinfo=timezone.utc).astimezone(UK)
    wd = local.weekday()  # Mo=0 … So=6
    if wd in (1, 2, 3):  # Di–Do: Sammlung am Dienstag
        days_back = wd - 1
    else:  # Fr–Mo: Sammlung am Freitag davor
        days_back = (wd - 4) % 7
    collected = datetime.combine(local.date() - timedelta(days=days_back), PRE_CLOSING_COLLECTION)
    return min(to_utc_naive(collected), kickoff_utc - timedelta(hours=1))


@dataclass
class ParsedSeason:
    matches: list[dict] = field(default_factory=list)
    # Quoten pro Spiel, Schlüssel = (Heimteam, Auswärtsteam)
    odds: dict[tuple[str, str], list[dict]] = field(default_factory=dict)


def _num(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_csv(content: bytes) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(io.StringIO(text), dtype=str, on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    return df.dropna(subset=["HomeTeam", "AwayTeam", "Date"])


def parse_season(df: pd.DataFrame) -> ParsedSeason:
    out = ParsedSeason()
    cols = list(df.columns)
    odds_cols = []
    for col in cols:
        if m := RE_1X2.match(col):
            odds_cols.append((col, m.group(1), "1X2", m.group(3), bool(m.group(2))))
        elif m := RE_OU.match(col):
            odds_cols.append((col, m.group(1), "OU", "O" if m.group(3) == ">" else "U", bool(m.group(2))))
        elif m := RE_AH.match(col):
            odds_cols.append((col, m.group(1), "AH", m.group(3), bool(m.group(2))))

    for row in df.to_dict("records"):
        day = pd.to_datetime(row["Date"], dayfirst=True).date()
        raw_time = (row.get("Time") or "").strip() if isinstance(row.get("Time"), str) else ""
        time_known = bool(raw_time)
        kickoff_local = datetime.combine(
            day, datetime.strptime(raw_time, "%H:%M").time() if time_known else DEFAULT_KICKOFF
        )
        kickoff = to_utc_naive(kickoff_local)
        result_known = (
            kickoff + RESULT_DELAY
            if time_known
            else to_utc_naive(datetime.combine(day, time(23, 59)))
        )
        home, away = row["HomeTeam"].strip(), row["AwayTeam"].strip()
        ft_home, ft_away = _num(row.get("FTHG")), _num(row.get("FTAG"))
        match = {
            "home": home,
            "away": away,
            "kickoff_utc": kickoff,
            "kickoff_time_known": time_known,
            "referee": (row.get("Referee") or None) if isinstance(row.get("Referee"), str) else None,
            "ft_home": None if ft_home is None else int(ft_home),
            "ft_away": None if ft_away is None else int(ft_away),
            "status": "finished" if ft_home is not None else "scheduled",
            "known_at": result_known,
        }
        for src, dst in STAT_COLUMNS.items():
            val = _num(row.get(src))
            match[dst] = val if dst in FLOAT_STATS or val is None else int(val)
        out.matches.append(match)

        pre_known = pre_closing_known_at(kickoff)
        rows = []
        for col, bk, market, selection, closing in odds_cols:
            price = _num(row.get(col))
            if price is None or price <= 1.0:
                continue
            line = 0.0
            if market == "OU":
                line = 2.5
            elif market == "AH":
                line_val = _num(row.get(f"{bk}{'C' if closing else ''}AH"))  # altes Format: pro Buchmacher
                if line_val is None:
                    line_col = ("BbAHh" if bk.startswith("Bb") else ("AHCh" if closing else "AHh"))
                    line_val = _num(row.get(line_col))
                if line_val is None:
                    continue
                line = line_val
            rows.append({
                "bookmaker": _ALIASES.get(bk, bk),
                "market": market,
                "line": line,
                "selection": selection,
                "price": price,
                "is_closing": closing,
                "known_at": kickoff if closing else pre_known,
            })  # fmt: skip
        out.odds[(home, away)] = rows
    return out


def download(code: str, season: str, cache_dir: Path, refresh: bool = False) -> bytes:
    """Lädt eine Saison-CSV. Abgeschlossene Saisons werden aus dem Cache gelesen."""
    path = cache_dir / season / f"{code}.csv"
    if path.exists() and not refresh:
        return path.read_bytes()
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    resp = session.get(BASE_URL.format(season=season, code=code), timeout=30)
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return resp.content


def _team_ids(session: Session, names: set[str], country: str | None) -> dict[str, int]:
    known = dict(
        session.execute(
            select(TeamAlias.alias, TeamAlias.team_id).where(
                TeamAlias.source == SOURCE, TeamAlias.alias.in_(names)
            )
        ).all()
    )
    missing = sorted(names - known.keys())
    if missing:
        now = utcnow()
        upsert(
            session,
            Team,
            [{"name": n, "country": country, "created_at": now, "updated_at": now} for n in missing],
            ["name"],
            update_cols=[],
        )
        ids = dict(session.execute(select(Team.name, Team.id).where(Team.name.in_(missing))).all())
        upsert(
            session,
            TeamAlias,
            [{"team_id": ids[n], "source": SOURCE, "alias": n} for n in missing],
            ["source", "alias"],
            update_cols=[],
        )
        known.update(ids)
    return known


def ensure_competition(session: Session, league: dict) -> int:
    now = utcnow()
    upsert(
        session,
        Competition,
        [{
            "code": league["code"],
            "name": league["name"],
            "country": league.get("country"),
            "kind": "league",
            "tier": league.get("tier"),
            "api_football_id": league.get("api_football_id"),
            "created_at": now,
            "updated_at": now,
        }],
        ["code"],
        update_cols=["name", "country", "tier", "api_football_id", "updated_at"],
    )  # fmt: skip
    return session.execute(select(Competition.id).where(Competition.code == league["code"])).scalar_one()


def import_season(session: Session, league: dict, season_code: str, content: bytes) -> dict[str, int]:
    """Schreibt eine Saison in die DB. Idempotent (Upsert auf natürlichen Schlüsseln)."""
    parsed = parse_season(read_csv(content))
    comp_id = ensure_competition(session, league)
    season = season_label(season_code)
    teams = _team_ids(
        session, {m["home"] for m in parsed.matches} | {m["away"] for m in parsed.matches}, league.get("country")
    )

    now = utcnow()
    match_rows = []
    for m in parsed.matches:
        row = {k: v for k, v in m.items() if k not in ("home", "away")}
        row.update(
            competition_id=comp_id,
            season=season,
            home_team_id=teams[m["home"]],
            away_team_id=teams[m["away"]],
            source=SOURCE,
            created_at=now,
            updated_at=now,
        )
        match_rows.append(row)
    conflict = ["competition_id", "season", "home_team_id", "away_team_id"]
    upsert(
        session,
        Match,
        match_rows,
        conflict,
        update_cols=[c for c in match_rows[0] if c not in (*conflict, "created_at")] if match_rows else [],
    )

    match_ids = {
        (h, a): mid
        for mid, h, a in session.execute(
            select(Match.id, Match.home_team_id, Match.away_team_id).where(
                Match.competition_id == comp_id, Match.season == season
            )
        ).all()
    }
    odds_rows = []
    for (home, away), rows in parsed.odds.items():
        mid = match_ids[(teams[home], teams[away])]
        odds_rows.extend({**r, "match_id": mid, "source": SOURCE} for r in rows)
    upsert(
        session,
        Odds,
        odds_rows,
        ["match_id", "bookmaker", "market", "line", "selection", "is_closing", "source"],
        update_cols=["price", "known_at"],
    )
    return {"matches": len(match_rows), "odds": len(odds_rows), "teams": len(teams)}


def run_import(engine, leagues: list[dict], seasons: list[str], cache_dir: Path, refresh: bool = False) -> list[dict]:
    """Importiert mehrere Ligen/Saisons, jede in eigener Transaktion mit Log-Eintrag."""
    from fussball.data.db import session_scope

    results = []
    for league in leagues:
        for season in seasons:
            resource = f"{league['code']}/{season}"
            with session_scope(engine) as s:
                entry = IngestLog(source=SOURCE, resource=resource)
                s.add(entry)
                s.flush()
                entry_id = entry.id
            try:
                content = download(league["code"], season, cache_dir, refresh=refresh)
                with session_scope(engine) as s:
                    counts = import_season(s, league, season, content)
                status, msg = "ok", None
                log.info("%s: %s", resource, counts)
            except requests.HTTPError as exc:
                counts, status, msg = {}, "error", f"HTTP {exc.response.status_code}"
                log.warning("%s: %s", resource, msg)
            except Exception as exc:  # noqa: BLE001 – Fehler loggen, restliche Ligen weiter importieren
                counts, status, msg = {}, "error", repr(exc)
                log.exception("%s fehlgeschlagen", resource)
            with session_scope(engine) as s:
                entry = s.get(IngestLog, entry_id)
                entry.status, entry.message = status, msg
                entry.rows = counts.get("matches", 0) + counts.get("odds", 0)
                entry.finished_at = utcnow()
            results.append({"resource": resource, "status": status, **counts, "message": msg})
    return results
