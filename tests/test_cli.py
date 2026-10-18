from datetime import date

from fussball.cli import current_season_code


def test_current_season_code():
    assert current_season_code(date(2026, 9, 29)) == "2627"
    assert current_season_code(date(2027, 3, 1)) == "2627"
    assert current_season_code(date(2027, 7, 1)) == "2728"
    assert current_season_code(date(1999, 8, 1)) == "9900"
