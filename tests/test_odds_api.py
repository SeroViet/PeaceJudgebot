from datetime import datetime

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
    assert oa.similarity("Borussia Monchengladbach", "M'gladbach") < oa.similarity("Bayern Munich", "Bayern Munich")
    assert oa.similarity("Manchester United", "Man United") >= 0.8
    assert oa.similarity("Bayer Leverkusen", "Leverkusen") == 1.0
    assert oa.similarity("Werder Bremen", "Augsburg") < 0.5


def test_event_rows_maps_bookmakers_and_markets():
    rows = oa.event_rows(_event("Augsburg", "Werder Bremen", "2024-08-24T13:30:00Z"), datetime(2024, 8, 23))
    ps = {(r["market"], r["selection"]): r["price"] for r in rows if r["bookmaker"] == "PS"}
    assert ps == {("1X2", "H"): 2.0, ("1X2", "D"): 3.6, ("1X2", "A"): 3.9, ("OU", "O"): 1.9, ("OU", "U"): 1.95}
    assert not any(r["bookmaker"] == "WH" and r["market"] == "OU" for r in rows)  # nur Linie 2.5


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
    assert out["D1"] == 8  # 5 Pinnacle + 3 William Hill
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
