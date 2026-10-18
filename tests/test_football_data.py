from datetime import datetime

import pytest
from sqlalchemy import func, select

from fussball.data import football_data as fd
from fussball.data.db import session_scope
from fussball.data.schema import Match, Odds, Team, TeamAlias


def test_season_label():
    assert fd.season_label("2425") == "2024-25"
    assert fd.season_label("9900") == "1999-00"
    assert fd.season_label("0405") == "2004-05"


def test_parse_modern_season_with_pinnacle(fixture_bytes):
    parsed = fd.parse_season(fd.read_csv(fixture_bytes("D1_2425_sample.csv")))
    first = parsed.matches[0]
    assert (first["home"], first["away"]) == ("M'gladbach", "Leverkusen")
    # 23.08.2024 19:30 britische Sommerzeit = 18:30 UTC
    assert first["kickoff_utc"] == datetime(2024, 8, 23, 18, 30)
    assert first["kickoff_time_known"] is True
    assert (first["ft_home"], first["ft_away"], first["status"]) == (2, 3, "finished")
    assert first["shots_home"] == 14 and first["corners_away"] == 4

    odds = {(o["bookmaker"], o["market"], o["selection"], o["is_closing"]): o for o in parsed.odds[("M'gladbach", "Leverkusen")]}
    assert odds[("PS", "1X2", "A", False)]["price"] == 1.6
    assert odds[("PS", "1X2", "A", True)]["price"] == 1.67
    assert odds[("BF", "1X2", "D", False)]["price"] == 4.5  # Spalte "BFD" = Betfair Remis, nicht Betfred
    assert odds[("PS", "OU", "O", False)]["line"] == 2.5  # Spalte "P>2.5"
    assert odds[("B365", "AH", "H", False)]["line"] == 1.0
    assert odds[("BFE", "1X2", "H", True)]["price"] == 5.4


def test_parse_current_season_has_xg_and_betfred(fixture_bytes):
    parsed = fd.parse_season(fd.read_csv(fixture_bytes("D1_2627_sample.csv")))
    first = parsed.matches[0]
    assert first["xg_home"] == pytest.approx(4.06)
    assert first["xg_away"] == pytest.approx(0.78)
    odds = {(o["bookmaker"], o["selection"], o["is_closing"]): o["price"]
            for o in parsed.odds[(first["home"], first["away"])] if o["market"] == "1X2"}
    assert odds[("BFD", "H", False)] == 1.2  # Spalte "BFDH" = Betfred
    assert odds[("BFD", "A", True)] == 11


def test_parse_old_season_without_kickoff_time(fixture_bytes):
    parsed = fd.parse_season(fd.read_csv(fixture_bytes("E0_0405_sample.csv")))
    first = parsed.matches[0]
    assert first["kickoff_time_known"] is False
    assert first["kickoff_utc"] == datetime(2004, 8, 14, 14, 0)  # 15:00 BST angenommen
    assert first["referee"] == "U Rennie"
    ah = [o for o in parsed.odds[("Aston Villa", "Southampton")] if o["market"] == "AH" and o["bookmaker"] == "B365"]
    assert {o["line"] for o in ah} == {-0.5}  # altes Format: Linie pro Buchmacher ("B365AH")


@pytest.mark.parametrize(
    "kickoff, expected",
    [
        # Samstag 14:30 UTC -> Freitag 16:00 BST = 15:00 UTC
        (datetime(2024, 8, 24, 13, 30), datetime(2024, 8, 23, 15, 0)),
        # Freitag 18:30 UTC -> selber Freitag 15:00 UTC
        (datetime(2024, 8, 23, 18, 30), datetime(2024, 8, 23, 15, 0)),
        # Mittwoch 18:30 UTC -> Dienstag 15:00 UTC
        (datetime(2024, 9, 25, 18, 30), datetime(2024, 9, 24, 15, 0)),
        # Dienstag 12:00 UTC (vor der Sammlung) -> Anpfiff - 1h
        (datetime(2024, 9, 24, 12, 0), datetime(2024, 9, 24, 11, 0)),
    ],
)
def test_pre_closing_known_at(kickoff, expected):
    assert fd.pre_closing_known_at(kickoff) == expected


def test_odds_known_at_never_after_kickoff_for_pre_closing(fixture_bytes):
    parsed = fd.parse_season(fd.read_csv(fixture_bytes("D1_2425_sample.csv")))
    kickoffs = {(m["home"], m["away"]): m["kickoff_utc"] for m in parsed.matches}
    for key, rows in parsed.odds.items():
        for o in rows:
            if o["is_closing"]:
                assert o["known_at"] == kickoffs[key]
            else:
                assert o["known_at"] < kickoffs[key]


def test_import_is_idempotent(engine, fixture_bytes, bundesliga):
    content = fixture_bytes("D1_2425_sample.csv")

    def counts():
        with session_scope(engine) as s:
            return tuple(s.execute(select(func.count()).select_from(m)).scalar_one()
                         for m in (Match, Odds, Team, TeamAlias))

    with session_scope(engine) as s:
        result = fd.import_season(s, bundesliga, "2425", content)
    first = counts()
    assert result["matches"] == 5 and first[0] == 5
    assert first[1] == result["odds"] > 100

    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", content)
    assert counts() == first


def test_reimport_updates_changed_values(engine, fixture_bytes, bundesliga):
    content = fixture_bytes("D1_2425_sample.csv")
    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", content)
    changed = content.replace(b"M'gladbach,Leverkusen,2,3", b"M'gladbach,Leverkusen,2,4")
    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2425", changed)
    with session_scope(engine) as s:
        m = s.scalars(select(Match).order_by(Match.kickoff_utc)).first()
        assert (m.ft_home, m.ft_away) == (2, 4)
        assert s.execute(select(func.count()).select_from(Match)).scalar_one() == 5
