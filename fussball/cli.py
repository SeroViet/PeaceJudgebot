"""Kommandozeile: `python -m fussball <befehl>`."""

from __future__ import annotations

import argparse
import logging
from datetime import date

from sqlalchemy import func, select

from fussball.config import get_settings, load_leagues
from fussball.data import football_data
from fussball.data.db import init_db, make_engine, session_scope
from fussball.data.schema import Base, Competition, Match


def current_season_code(today: date | None = None) -> str:
    """Saison-Code von football-data, Saisonwechsel ab Juli: 29.09.2026 -> '2627'."""
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def _cmd_init_db(args) -> int:
    engine = make_engine()
    init_db(engine)
    print(f"Datenbank bereit: {engine.url}")
    return 0


def _cmd_import_fd(args) -> int:
    settings = get_settings()
    leagues = load_leagues()
    codes = args.leagues or [c for c, cfg in leagues.items() if cfg.get("enabled")]
    unknown = [c for c in codes if c not in leagues]
    if unknown:
        print(f"Unbekannte Liga-Codes: {unknown}. Siehe config/leagues.yaml")
        return 2
    engine = make_engine()
    init_db(engine)
    current = current_season_code()
    seasons = args.seasons or [current]
    results = []
    for season in seasons:
        # Laufende Saison immer neu laden, abgeschlossene aus dem Cache.
        results += football_data.run_import(
            engine,
            [leagues[c] for c in codes],
            [season],
            settings.storage_dir / "raw" / "football-data",
            refresh=args.refresh or season == current,
        )
    ok = [r for r in results if r["status"] == "ok"]
    for r in results:
        detail = f"{r.get('matches', 0)} Spiele, {r.get('odds', 0)} Quoten" if r["status"] == "ok" else r["message"]
        print(f"  {r['resource']:<10} {r['status']:<6} {detail}")
    print(f"{len(ok)}/{len(results)} Dateien importiert")
    return 0 if len(ok) == len(results) else 1


def _cmd_status(args) -> int:
    engine = make_engine()
    init_db(engine)
    with session_scope(engine) as s:
        print("Tabellen:")
        for table in Base.metadata.sorted_tables:
            count = s.execute(select(func.count()).select_from(table)).scalar_one()
            print(f"  {table.name:<20} {count:>8}")
        rows = s.execute(
            select(Competition.code, Match.season, func.count(Match.id), func.max(Match.kickoff_utc))
            .join(Match, Match.competition_id == Competition.id)
            .group_by(Competition.code, Match.season)
            .order_by(Competition.code, Match.season)
        ).all()
        if rows:
            print("\nSpiele je Liga/Saison:")
            for code, season, n, last in rows:
                print(f"  {code:<5} {season}  {n:>4} Spiele, letztes {last:%d.%m.%Y}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fussball", description="Fussball-Prognose-KI")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Datenbank und Tabellen anlegen").set_defaults(func=_cmd_init_db)

    p = sub.add_parser("import-fd", help="Ergebnisse und Quoten von football-data.co.uk importieren")
    p.add_argument("--leagues", nargs="+", help="Liga-Codes, z. B. D1 E0 (Standard: enabled in leagues.yaml)")
    p.add_argument("--seasons", nargs="+", help="Saison-Codes, z. B. 2324 2425 (Standard: aktuelle Saison)")
    p.add_argument("--refresh", action="store_true", help="Cache ignorieren und neu herunterladen")
    p.set_defaults(func=_cmd_import_fd)

    sub.add_parser("status", help="Datenbestand anzeigen").set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)
