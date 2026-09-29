from datetime import datetime

import pytest

from sqlalchemy import select

from fussball.data import football_data as fd
from fussball.data import odds_api as oa
from fussball.data.db import session_scope
from fussball.data.schema import Match, Odds
from fussball.service import MatchForecast, tips_from_forecasts


def _event(home, away, commence, pin=(2.0, 3.6, 3.9), wh=(2.15, 3.5, 3.6)):
    def h2h(p):
        return {"key": "h2h", "outcomes": [{"name": home, "price": p[0]}, {"name": away, "price": p[2]},
                                           {"name": "Draw", "price": p[1]}]}
    return {
        "id": "x", "home_team": home, "away_team": away, "commence_time": commence,
        "bookmakers": [
            {"key": "pinnacle", "markets": [h2h(pin), {"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.9, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}]}]},
            {"key": "williamhill", "markets": [h2h(wh), {"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.8, "point": 3.5}]}]},
        ],
    }


def test_similarity_handles_common_variants():
    assert oa.similarity("Borussia Monchengladbach", "M'gladbach") == 1.0  # feste Zuordnung
    assert oa.similarity("Manchester United", "Man United") == 1.0
    assert oa.similarity("Bayer Leverkusen", "Leverkusen") == 0.9
    assert oa.similarity("Werder Bremen", "Augsburg") < 0.5


@pytest.mark.parametrize("api_name, expected", [
    ("Inter Milan", "Inter"), ("AC Milan", "Milan"), ("Atlético Madrid", "Ath Madrid"), ("Real Madrid", "Real Madrid"),
    ("Paris Saint Germain", "Paris SG"), ("Paris FC", "Paris FC"), ("Bayer Leverkusen", "Leverkusen"),
])
def test_best_team_never_confuses_similar_clubs(api_name, expected):
    teams = dict(enumerate(["Inter", "Milan", "Ath Madrid", "Real Madrid", "Paris SG", "Paris FC", "Leverkusen",
                            "Leverkusen II"]))
    assert teams[oa.best_team(api_name, teams)] == expected


def test_best_team_rejects_ambiguous_names():
    teams = {1: "Madrid Norte", 2: "Madrid Sur"}
    assert oa.best_team("Madrid", teams) is None


def test_event_rows_maps_bookmakers_and_markets():
    rows = oa.event_rows(_event("Augsburg", "Werder Bremen", "2024-08-24T13:30:00Z"), datetime(2024, 8, 23))
    ps = {(r["market"], r["selection"]): r["price"] for r in rows if r["bookmaker"] == "PS"}
    assert ps == {("1X2", "H"): 2.0, ("1X2", "D"): 3.6, ("1X2", "A"): 3.9, ("OU", "O"): 1.9, ("OU", "U"): 1.95}
    wh_ou = [(r["line"], r["selection"]) for r in rows if r["bookmaker"] == "WH" and r["market"] == "OU"]
    assert wh_ou == [(3.5, "O")]  # alle Linien werden gespeichert (Pinnacle-Hauptlinie ist nicht immer 2.5)


def test_import_matches_existing_fixture_and_learns_alias(engine, fixture_bytes, bundesliga):
    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", fixture_bytes("D1_2425_sample.csv"))

    class Fake:
        remaining = 400

        def odds(self, sport):
            assert sport == "soccer_germany_bundesliga"
            return [_event("FC Augsburg", "SV Werder Bremen", "2024-08-24T13:30:00Z")]

    with session_scope(engine) as s:
        out = oa.import_odds(s, Fake(), ["D1"])
    assert out["D1"] == 9  # 5 Pinnacle + 3 William Hill 1X2 + 1 William Hill Über 3.5
    with session_scope(engine) as s:
        rows = s.scalars(select(Odds).where(Odds.source == "odds-api")).all()
        m = s.get(Match, rows[0].match_id)
        assert m.kickoff_utc == datetime(2024, 8, 24, 13, 30)
        assert s.execute(select(Match).where(Match.source == "odds-api")).first() is None  # kein Duplikat


def test_import_creates_missing_match_for_known_teams(engine, fixture_bytes, bundesliga):
    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", fixture_bytes("D1_2425_sample.csv"))

    class Fake:
        remaining = 1

        def odds(self, sport):
            return [_event("Bayer Leverkusen", "Augsburg", "2024-09-14T13:30:00Z"),
                    _event("Unbekannt FC", "Augsburg", "2024-09-14T13:30:00Z")]

    with session_scope(engine) as s:
        oa.import_odds(s, Fake(), ["D1"])
        created = s.scalars(select(Match).where(Match.source == "odds-api")).all()
        assert len(created) == 1 and created[0].status == "scheduled" and created[0].season == "2024-25"


def test_tips_use_best_named_bookmaker():
    f = MatchForecast(1, "D1", "Bundesliga", datetime(2026, 10, 3, 13, 30), "A", "B", 1.5, 1.1,
                      {"H": 0.5, "D": 0.27, "A": 0.23}, {"O": 0.5, "U": 0.5}, {"H": 0.5, "D": 0.27, "A": 0.23},
                      None, None, 0.0,
                      {"best": {"H": 2.15}, "books": {"H": "WH"}, "avg": {"H": 1.95}, "max": {}})
    tips = tips_from_forecasts([f], price="best")
    assert len(tips) == 1 and tips[0].bookmaker == "William Hill"
    assert tips[0].edge == 0.5 * 2.15 - 1


def test_no_ou_tip_without_own_reference():
    base = dict(match_id=1, comp="E0", comp_name="PL", kickoff_utc=datetime(2026, 10, 10, 11, 30), home="A", away="B",
                lam=1.2, mu=0.8, probs_1x2={"H": 0.7, "D": 0.2, "A": 0.1}, probs_ou={"O": 0.41, "U": 0.59},
                model_1x2={"H": 0.7, "D": 0.2, "A": 0.1}, market_1x2={"H": 0.7, "D": 0.2, "A": 0.1}, market_ou=None,
                elo_diff=0.0, odds={"best": {"U": 2.15}, "books": {"U": "COOL"}})
    assert tips_from_forecasts([MatchForecast(**base, ou_ok=False)]) == []
    assert len(tips_from_forecasts([MatchForecast(**base, ou_ok=True)])) == 1
