"""Private Web-App (PWA): Dashboard, Spiel-Detail, Kombis, Wett-Tracking, Bilanz."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from fussball import service
from fussball.app import auth, state
from fussball.config import load_leagues
from fussball.data.db import init_db, make_engine, session_scope
from fussball.data.schema import Bet, IngestLog, Match, Odds, Team, utcnow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

LABELS = {"H": "1", "D": "X", "A": "2", "O": "Über", "U": "Unter"}


def pct(x, digits=1):
    return "–" if x is None else f"{x * 100:.{digits}f} %"


def money(x, cur="CHF"):
    return "–" if x is None else f"{x:,.2f} {cur}".replace(",", "'")


templates.env.filters.update(
    pct=pct,
    money=money,
    local=lambda s: state.local(s).strftime("%a %d.%m. %H:%M")
    .replace("Mon", "Mo").replace("Tue", "Di").replace("Wed", "Mi").replace("Thu", "Do")
    .replace("Fri", "Fr").replace("Sat", "Sa").replace("Sun", "So"),
    odds=lambda x: "–" if x is None else f"{x:.2f}",
    sel=lambda s: LABELS.get(s, s),
)


def create_app(engine=None, start_background: bool = True) -> FastAPI:
    engine = engine or make_engine()
    init_db(engine)
    limiter = auth.RateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks, jobs = [], None
        if start_background:
            try:
                from fussball.app import jobs

                tasks = await jobs.start(engine)
            except Exception:  # noqa: BLE001 – Web-App soll auch ohne Bot/Scheduler laufen
                log.exception("Hintergrund-Jobs/Telegram konnten nicht starten")
                jobs = None
        yield
        for t in tasks:
            t.cancel()
        if jobs is not None:
            await jobs.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        public = request.url.path in ("/login", "/robots.txt", "/manifest.webmanifest", "/sw.js", "/healthz") \
            or request.url.path.startswith("/static")
        if not public and not request.session.get("auth"):
            if request.url.path.startswith("/api"):
                return JSONResponse({"error": "login"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        response: Response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # Zuletzt hinzugefügt = äusserste Middleware: Session muss vor dem Login-Wächter laufen.
    app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(), max_age=14 * 24 * 3600,
                       same_site="strict", https_only=os.getenv("APP_HTTPS_ONLY", "1") == "1")

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("plan", state.load_plan())
        ctx["status"] = state.status()
        ctx["active"] = name.split(".")[0]
        return templates.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------------ öffentlich

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": "PeaceJudge Tipps", "short_name": "PeaceJudge", "start_url": "/", "display": "standalone",
            "background_color": "#0b0f14", "theme_color": "#0b0f14",
            "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
        }, media_type="application/manifest+json")  # fmt: skip

    @app.get("/sw.js")
    def service_worker():
        return Response((HERE / "static" / "sw.js").read_text(), media_type="application/javascript")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        pw_hash, _ = auth.credentials()
        return templates.TemplateResponse(request, "login.html", {"error": None, "configured": bool(pw_hash),
                                                                  "totp": bool(auth.credentials()[1])})

    @app.post("/login")
    def login(request: Request, password: str = Form(...), code: str = Form("")):
        ip = request.client.host if request.client else "?"
        pw_hash, totp_secret = auth.credentials()
        error = None
        if limiter.blocked(ip):
            error = "Zu viele Versuche. Bitte 15 Minuten warten."
        elif not (auth.verify_password(password, pw_hash) and auth.verify_totp(code, totp_secret)):
            limiter.fail(ip)
            error = "Passwort oder Code falsch."
        if error:
            return templates.TemplateResponse(request, "login.html", {"error": error, "configured": bool(pw_hash),
                                                                      "totp": bool(totp_secret)}, status_code=401)
        limiter.reset(ip)
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ Seiten

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return render(request, "dashboard.html", stats=service.bet_stats(engine))

    @app.get("/spiel/{match_id}", response_class=HTMLResponse)
    def match_detail(request: Request, match_id: int):
        plan = state.load_plan()
        f = next((f for f in plan["forecasts"] if f["match_id"] == match_id), None)
        if f is None:
            return render(request, "message.html", title="Spiel nicht gefunden",
                          text="Für dieses Spiel gibt es aktuell keine Prognose.")
        tips = [s for s in plan["singles"] if s["match_id"] == match_id]
        return render(request, "match.html", f=f, tips=tips)

    @app.get("/kombi", response_class=HTMLResponse)
    def combos(request: Request):
        return render(request, "kombi.html")

    @app.get("/alle", response_class=HTMLResponse)
    def all_matches(request: Request):
        return render(request, "alle.html")

    @app.get("/wetten", response_class=HTMLResponse)
    def bets(request: Request):
        with session_scope(engine) as s:
            rows = s.execute(
                select(Bet, Match, Team.name).join(Match, Match.id == Bet.match_id)
                .join(Team, Team.id == Match.home_team_id).order_by(Bet.placed_at.desc()).limit(200)
            ).all()
            away = {t.id: t.name for t in s.scalars(select(Team))}
            items = [{"id": b.id, "type": b.bet_type, "group": b.combo_group, "match": f"{h} – {away[m.away_team_id]}",
                      "kickoff": m.kickoff_utc.isoformat(), "market": b.market, "line": b.line,
                      "selection": b.selection, "odds": b.odds_taken, "stake": b.stake, "status": b.status,
                      "pnl": b.pnl, "clv": b.clv, "score": f"{m.ft_home}:{m.ft_away}" if m.ft_home is not None else ""}
                     for b, m, h in rows]
        return render(request, "wetten.html", items=items)

    @app.post("/wetten/neu")
    def add_bet(request: Request, tip_id: str = Form(...), stake: float = Form(...), odds: float = Form(...),
                bookmaker: str = Form("Sporttip")):
        plan = state.load_plan()
        if tip_id.upper().startswith("K"):
            combo = next((c for c in plan["combos"] if c["id"] == tip_id.upper()), None)
            legs = combo["legs"] if combo else None
        else:
            tip = next((s for s in plan["singles"] if s["id"] == tip_id), None)
            legs = [tip] if tip else None
        if not legs:
            return render(request, "message.html", title="Tipp nicht gefunden", text=f"Tipp {tip_id} existiert nicht.")
        service.record_bet(engine, legs, stake, odds, bookmaker)
        return RedirectResponse("/wetten", status_code=303)

    @app.post("/wetten/{bet_id}/loeschen")
    def delete_bet(bet_id: int):
        with session_scope(engine) as s:
            b = s.get(Bet, bet_id)
            if b is not None:
                if b.combo_group:
                    for leg in s.scalars(select(Bet).where(Bet.combo_group == b.combo_group)):
                        s.delete(leg)
                else:
                    s.delete(b)
        return RedirectResponse("/wetten", status_code=303)

    @app.get("/bilanz", response_class=HTMLResponse)
    def stats(request: Request):
        return render(request, "bilanz.html", stats=service.bet_stats(engine))

    @app.get("/modell", response_class=HTMLResponse)
    def model_quality(request: Request):
        return render(request, "modell.html", summary=service.model_summary(), leagues=load_leagues())

    @app.get("/einstellungen", response_class=HTMLResponse)
    def settings_form(request: Request):
        with session_scope(engine) as s:
            books = sorted({b for (b,) in s.execute(select(Odds.bookmaker).where(Odds.source == "odds-api").distinct())}
                           - {"PS", "BFE", "MBK"})
        return render(request, "einstellungen.html", cfg=service.get_config(engine), leagues=load_leagues(), books=books,
                      book_names=service.BOOK_NAMES,
                      enabled=service.enabled_leagues(engine), saved=request.query_params.get("ok"))

    @app.post("/einstellungen")
    async def settings_save(request: Request):
        form = await request.form()
        f = lambda k: float(str(form.get(k)).replace(",", "."))  # noqa: E731
        leagues = load_leagues()
        chosen = set(form.getlist("leagues"))
        default_on = {c for c, v in leagues.items() if v.get("enabled")}
        service.save_config(engine, {
            "bankroll": f("bankroll"),
            "singles": {"min_edge": f("min_edge") / 100, "min_prob": f("min_prob") / 100, "max_odds": f("max_odds"),
                        "kelly_fraction": f("kelly_fraction"), "max_stake_pct": f("max_stake_pct") / 100,
                        "daily_limit_pct": f("daily_limit_pct") / 100, "weekly_limit_pct": f("weekly_limit_pct") / 100},
            "combos": {"min_legs": int(f("min_legs")), "max_legs": int(f("max_legs")),
                       "leg_min_prob": f("leg_min_prob") / 100, "leg_min_edge": f("leg_min_edge") / 100,
                       "leg_min_odds": f("leg_min_odds")},
            "force_enabled_leagues": sorted(chosen - default_on) + (["*"] if form.get("ignore_backtest") else []),
            "force_disabled_leagues": sorted(default_on - chosen),
            "bookmakers": [b.strip().upper() for b in str(form.get("bookmakers") or "").replace(";", ",").split(",")
                           if b.strip()],
        })  # fmt: skip
        return RedirectResponse("/einstellungen?ok=1", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def system_status(request: Request):
        with session_scope(engine) as s:
            logs = s.scalars(select(IngestLog).order_by(IngestLog.id.desc()).limit(30)).all()
            logs = [{"resource": x.resource, "status": x.status, "rows": x.rows, "message": x.message,
                     "at": x.started_at.isoformat()} for x in logs]
        return render(request, "status.html", logs=logs)

    @app.post("/aktualisieren")
    async def trigger_refresh(request: Request):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: state.refresh(engine, fetch=True))
        return RedirectResponse("/status", status_code=303)

    @app.get("/api/plan")
    def api_plan():
        return state.load_plan()

    return app


def upcoming_window():
    now = utcnow()
    return now, now + timedelta(days=3)
