import json
from datetime import timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fussball import service
from fussball.app import auth, state
from fussball.data import football_data as fd
from fussball.data.db import session_scope
from fussball.data.schema import Bet, Match


@pytest.fixture
def app_env(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("FUSSBALL_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password("richtig-langes-pw"))
    secret = pyotp.random_base32()
    monkeypatch.setenv("APP_TOTP_SECRET", secret)
    monkeypatch.setenv("APP_HTTPS_ONLY", "0")
    from fussball.app.web import create_app

    client = TestClient(create_app(engine, start_background=False))
    return client, secret


def login(client, secret, pw="richtig-langes-pw"):
    return client.post("/login", data={"password": pw, "code": pyotp.TOTP(secret).now()}, follow_redirects=False)


def test_password_hashing():
    h = auth.hash_password("geheim123456")
    assert auth.verify_password("geheim123456", h)
    assert not auth.verify_password("falsch", h)
    assert not auth.verify_password("x", None)


def test_pages_require_login(app_env):
    client, _ = app_env
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/plan").status_code == 401
    assert client.get("/robots.txt").text.startswith("User-agent: *\nDisallow: /")


def test_login_needs_password_and_totp(app_env):
    client, secret = app_env
    assert client.post("/login", data={"password": "richtig-langes-pw", "code": "000000"}).status_code == 401
    assert login(client, secret).status_code == 303
    r = client.get("/")
    assert r.status_code == 200 and "Einzeltipps" in r.text
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_rate_limit_blocks_after_failures(app_env):
    client, secret = app_env
    for _ in range(5):
        client.post("/login", data={"password": "falsch", "code": "1"})
    r = login(client, secret)
    assert r.status_code == 401 and "Zu viele Versuche" in r.text


def test_all_pages_render_when_logged_in(app_env):
    client, secret = app_env
    login(client, secret)
    for path in ("/", "/kombi", "/alle", "/wetten", "/bilanz", "/modell", "/einstellungen", "/status"):
        assert client.get(path).status_code == 200, path


def _seed_plan(engine, tmp_path, fixture_bytes, league):
    with session_scope(engine) as s:
        fd.import_season(s, league, "2425", fixture_bytes("D1_2425_sample.csv"))
        m = s.scalars(select(Match).order_by(Match.kickoff_utc)).first()
        mid, kickoff = m.id, m.kickoff_utc
    plan = {"generated_at": "2024-08-20T10:00:00", "forecasts": [], "blocked_leagues": [], "currency": "CHF",
            "singles": [{"id": "1", "match_id": mid, "match": "M'gladbach – Leverkusen", "kickoff": kickoff.isoformat(),
                         "comp": "D1", "market": "1X2", "line": 0.0, "selection": "A", "label": "Auswärtssieg",
                         "prob": 0.66, "odds": 1.62, "fair_odds": 1.52, "min_odds": 1.59, "edge": 0.07,
                         "stake": 10.0, "bookmaker": "Ø Markt"}],
            "combos": []}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    return mid


def test_record_and_settle_single_bet_with_clv(app_env, engine, tmp_path, fixture_bytes, bundesliga):
    client, secret = app_env
    mid = _seed_plan(engine, tmp_path, fixture_bytes, bundesliga)
    login(client, secret)
    r = client.post("/wetten/neu", data={"tip_id": "1", "stake": "10", "odds": "1.62"}, follow_redirects=False)
    assert r.status_code == 303
    assert service.settle_bets(engine) == 1  # Spiel endete 2:3 → Auswärtssieg
    with session_scope(engine) as s:
        b = s.scalars(select(Bet)).one()
        assert (b.match_id, b.status, b.pnl) == (mid, "won", 6.2)
        # Pinnacle-Schluss 4.94/4.38/1.67 → faire p(A) ≈ 0.590; CLV = 1.62 × 0.590 − 1 ≈ −4.5 %
        assert b.closing_odds == 1.67 and b.clv == pytest.approx(-0.045, abs=0.003)
    stats = service.bet_stats(engine)
    assert stats["n"] == 1 and stats["pnl"] == pytest.approx(6.2)
    assert "gewonnen" in client.get("/wetten").text


def test_combo_settlement_counts_stake_once(engine, fixture_bytes, bundesliga):
    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", fixture_bytes("D1_2425_sample.csv"))
        ms = s.scalars(select(Match).order_by(Match.kickoff_utc)).all()
        results = {m.id: ("H" if m.ft_home > m.ft_away else "D" if m.ft_home == m.ft_away else "A") for m in ms}
    ids = list(results)[:3]
    legs = [{"match_id": i, "market": "1X2", "selection": results[i], "odds": 2.0} for i in ids]
    service.record_bet(engine, legs, stake=5, odds=8.0)
    assert service.settle_bets(engine) == 1
    stats = service.bet_stats(engine)
    assert stats["n"] == 1 and stats["pnl"] == pytest.approx(35.0)
    assert stats["by_type"]["combo"]["roi"] == pytest.approx(7.0)


def test_settings_roundtrip(app_env, engine):
    client, secret = app_env
    login(client, secret)
    form = {"bankroll": "800", "min_edge": "6", "min_prob": "0", "max_odds": "5", "kelly_fraction": "0.2",
            "max_stake_pct": "1.5", "daily_limit_pct": "5", "weekly_limit_pct": "12", "min_legs": "4", "max_legs": "5",
            "leg_min_prob": "62", "leg_min_edge": "3", "leg_min_odds": "1.5", "leagues": ["D1", "E0"],
            "bookmakers": "wh, 1xb"}
    assert client.post("/einstellungen", data=form, follow_redirects=False).status_code == 303
    cfg = service.get_config(engine)
    assert cfg["bankroll"] == 800 and cfg["singles"]["min_edge"] == pytest.approx(0.06)
    assert set(service.enabled_leagues(engine)) == {"D1", "E0"}
    assert cfg["bookmakers"] == ["WH", "1XB"]


def test_diff_plans_detects_changes():
    old = {"singles": [{"match_id": 1, "market": "1X2", "selection": "H", "match": "A – B", "label": "Heimsieg",
                        "odds": 2.0}]}
    new = {"singles": [{"match_id": 2, "market": "OU", "selection": "O", "match": "C – D", "label": "Über 2.5",
                        "odds": 1.9}]}
    changes = state.diff_plans(old, new)
    assert any("Neu" in c for c in changes) and any("Gestrichen" in c for c in changes)
    _ = timedelta
