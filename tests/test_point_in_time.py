from datetime import timedelta

from sqlalchemy import select

from fussball.data import football_data as fd
from fussball.data.db import session_scope
from fussball.data.point_in_time import latest_odds_as_of, results_as_of, team_matches_before
from fussball.data.schema import Match


def _load(engine, fixture_bytes, league):
    with session_scope(engine) as s:
        fd.import_season(s, league, "2425", fixture_bytes("D1_2425_sample.csv"))


def test_closing_odds_invisible_before_kickoff(engine, fixture_bytes, bundesliga):
    _load(engine, fixture_bytes, bundesliga)
    with session_scope(engine) as s:
        match = s.scalars(select(Match).order_by(Match.kickoff_utc)).first()
        before = latest_odds_as_of(s, match.id, match.kickoff_utc - timedelta(minutes=60))
        assert before and not any(o.is_closing for o in before)
        ps_away = [o for o in before if (o.bookmaker, o.market, o.selection) == ("PS", "1X2", "A")]
        assert [o.price for o in ps_away] == [1.6]

        after = latest_odds_as_of(s, match.id, match.kickoff_utc)
        ps_away = [o for o in after if (o.bookmaker, o.market, o.selection) == ("PS", "1X2", "A")]
        assert [(o.price, o.is_closing) for o in ps_away] == [(1.67, True)]

        nothing = latest_odds_as_of(s, match.id, match.kickoff_utc - timedelta(days=7))
        assert nothing == []


def test_results_hidden_until_known(engine, fixture_bytes, bundesliga):
    _load(engine, fixture_bytes, bundesliga)
    with session_scope(engine) as s:
        match = s.scalars(select(Match).order_by(Match.kickoff_utc)).first()
        at_kickoff = s.scalars(results_as_of(match.kickoff_utc)).all()
        assert match.id not in {m.id for m in at_kickoff}
        later = s.scalars(results_as_of(match.kickoff_utc + timedelta(hours=3))).all()
        assert match.id in {m.id for m in later}

        # Das Folgespiel des Heimteams darf das eigene Ergebnis nicht vorab sehen.
        history = s.scalars(team_matches_before(match.home_team_id, match.kickoff_utc)).all()
        assert history == []
