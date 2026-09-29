from datetime import datetime

import pytest

from fussball.models.implied import fit_rates, implied_markets, label, outcome, p_over_line
from fussball.models.dixon_coles import score_matrix
from fussball.service import MatchForecast, safe_tips, split_market


def test_fit_reproduces_market():
    lam, mu = fit_rates({"H": 0.55, "D": 0.25, "A": 0.20}, 2.5, 0.55)
    m = implied_markets(lam, mu)
    assert m["1X2"]["H"] == pytest.approx(0.55, abs=0.01)
    assert m["OU2.5"]["O"] == pytest.approx(0.55, abs=0.01)
    assert m["OU0.5"]["O"] > m["OU1.5"]["O"] > m["OU2.5"]["O"] > m["OU3.5"]["O"]
    assert m["DC"]["1X"] == pytest.approx(0.80, abs=0.01)


def test_whole_line_excludes_push():
    m = score_matrix(1.4, 1.1)
    assert 0 < p_over_line(m, 3.0) < p_over_line(m, 2.5)


@pytest.mark.parametrize("market, sel, hg, ag, expected", [
    ("OU2.5", "U", 1, 1, True), ("OU2.5", "O", 2, 1, True), ("BTTS", "Y", 1, 0, False), ("DC", "1X", 1, 1, True),
    ("DC", "X2", 2, 0, False), ("DNB", "H", 1, 1, None), ("HOME0.5", "O", 1, 0, True), ("AWAY1.5", "U", 3, 2, False),
])
def test_outcome(market, sel, hg, ag, expected):
    assert outcome(market, sel, hg, ag) is expected


def test_labels_like_betting_slip():
    assert label("OU2.5", "U", "A", "B") == "Unter 2.5 Tore"
    assert label("DC", "1X", "Bayern", "Dortmund") == "Bayern oder Unentschieden (1X)"
    assert label("BTTS", "Y", "A", "B") == "Beide Teams treffen: Ja"


def test_split_market():
    assert split_market("OU1.5") == ("OU", 1.5)
    assert split_market("HOME0.5") == ("HOME", 0.5)
    assert split_market("DC") == ("DC", 0.0)


def test_safe_tips_respect_range_and_families():
    lam, mu = fit_rates({"H": 0.62, "D": 0.22, "A": 0.16}, 2.5, 0.58)
    f = MatchForecast(1, "D1", "Bundesliga", datetime(2026, 10, 10, 13, 30), "Bayern", "Mainz", lam, mu,
                      {"H": 0.62, "D": 0.22, "A": 0.16}, {"O": 0.58, "U": 0.42}, {}, None, None, 0.0, {},
                      implied=implied_markets(lam, mu))
    tips = safe_tips([f], 0.70, 0.90, per_match=2)
    assert 1 <= len(tips) <= 2
    assert all(0.70 <= t["prob"] <= 0.90 for t in tips)
    assert len({t["market"][:2] for t in tips}) == len(tips)  # verschiedene Familien
