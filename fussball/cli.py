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


def _cmd_backtest(args) -> int:
    import json
    from concurrent.futures import ProcessPoolExecutor

    from fussball.models import backtest as bt
    from fussball.service import np_default

    engine = make_engine()
    leagues = load_leagues()
    codes = args.leagues or [c for c, cfg in leagues.items() if cfg.get("enabled")]
    frame = bt.load_frame(engine)
    cfg = bt.ModelConfig(**bt.BEST_CONFIG)
    rules = bt.BetRules(min_edge=args.min_edge, price_col=args.price)
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        preds = dict(zip(codes, ex.map(bt.walk_forward, [frame] * len(codes), codes, [cfg] * len(codes))))
    summary = {"generated_at": bt.utcnow_iso(), "model_config": bt.BEST_CONFIG, "price": args.price,
               "min_edge": args.min_edge, "leagues": {}}
    for code, p in preds.items():
        s = bt.league_summary(p, rules)
        summary["leagues"][code] = s
        summary["test_seasons"] = s.pop("test_seasons")
        print(f"{code:<4} LL Modell {s['ll_model']:.4f} Markt {s['ll_market']:.4f} Kombi {s['ll_blend']:.4f} | "
              f"Ü/U Markt {s['ou_market']:.4f} Kombi {s['ou_blend']:.4f} | Wetten {s['bets']:>4} "
              f"ROI {s['roi']:+.1%} CLV {s['clv']:+.2%} t={s['t']:+.2f} | "
              f"{'AKTIV' if s['beats_market'] else 'gesperrt'}")
    path = get_settings().storage_dir / "backtest_summary.json"
    path.write_text(json.dumps(summary, default=np_default, indent=2), encoding="utf-8")
    print(f"Gespeichert: {path}")
    return 0


def _cmd_refresh(args) -> int:
    from fussball.app import state

    engine = make_engine()
    init_db(engine)
    info = state.refresh(engine, fetch=not args.no_fetch, days=args.days)
    plan = state.load_plan()
    print(f"{len(plan['forecasts'])} Spiele, {len(plan['singles'])} Einzeltipps, {len(plan['combos'])} Kombis")
    for t in plan["singles"]:
        print(f"  #{t['id']} {t['match']}: {t['label']} @ {t['odds']:.2f} (min {t['min_odds']:.2f}) "
              f"p={t['prob']:.1%} edge={t['edge']:+.1%} Einsatz {t['stake']:.2f}")
    for c in plan["combos"]:
        print(f"  {c['id']} {c['variant']}: {len(c['legs'])} Tipps, Quote {c['odds']:.2f}, p={c['prob']:.1%}, EV {c['ev']:+.1%}")
    for line in info.get("changes", []):
        print("  " + line)
    return 0


def _cmd_set_password(args) -> int:
    import getpass
    import secrets

    from fussball.app.auth import hash_password, new_totp_secret

    pw = getpass.getpass("Neues Passwort (mind. 12 Zeichen): ")
    if len(pw) < 12 or pw != getpass.getpass("Wiederholen: "):
        print("Passwörter stimmen nicht überein oder sind zu kurz.")
        return 2
    secret, uri = new_totp_secret()
    print("\nDiese Werte als Umgebungsvariablen / Secrets beim Hoster eintragen (NICHT committen):\n")
    print(f"APP_PASSWORD_HASH={hash_password(pw)}")
    print(f"APP_TOTP_SECRET={secret}")
    print(f"SESSION_SECRET={secrets.token_urlsafe(32)}")
    print(f"\n2FA: In Google Authenticator/1Password manuell den Schlüssel {secret} eintragen oder diese URI nutzen:\n{uri}")
    return 0


def _cmd_serve(args) -> int:
    import os

    import uvicorn

    from fussball.app.web import create_app

    port = int(args.port or os.getenv("PORT", "8000"))
    uvicorn.run(create_app(), host=args.host, port=port, proxy_headers=True, forwarded_allow_ips="*")
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

    p = sub.add_parser("backtest", help="Walk-Forward-Backtest; legt Gewichte und freigegebene Ligen fest")
    p.add_argument("--leagues", nargs="+")
    p.add_argument("--min-edge", type=float, default=0.05)
    p.add_argument("--price", choices=["avg", "max"], default="avg")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=_cmd_backtest)

    p = sub.add_parser("refresh", help="Daten laden, Wetten abrechnen, Tipps berechnen")
    p.add_argument("--no-fetch", action="store_true", help="ohne Download, nur neu rechnen")
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=_cmd_refresh)

    sub.add_parser("set-password", help="Login-Passwort und 2FA einrichten").set_defaults(func=_cmd_set_password)

    p = sub.add_parser("serve", help="Web-App + Telegram-Bot + Scheduler starten")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)
