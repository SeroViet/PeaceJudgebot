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
    assert label("DC", "1X", "Bayern", "Dortmund") == "1X (Bayern)"
    assert label("1X2", "D", "Bayern", "Dortmund") == "X (Unentschieden)"
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


def test_day_combos_same_day_one_tip_per_match():
    from fussball.service import day_combos

    fs = []
    for i, hour in enumerate([11, 13, 14, 16, 18, 19, 20]):
        lam, mu = fit_rates({"H": 0.6, "D": 0.23, "A": 0.17}, 2.5, 0.55)
        fs.append(MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, hour, 0), f"H{i}", f"A{i}", lam, mu,
                                {}, {}, {}, None, None, 0.0, {}, implied=implied_markets(lam, mu)))
    fs.append(MatchForecast(99, "D1", "BL", datetime(2026, 10, 11, 13, 0), "X", "Y", 1.5, 1.0, {}, {}, {},
                            None, None, 0.0, {}, implied=implied_markets(1.5, 1.0)))
    combos = day_combos(fs, sizes=(5, 6))
    assert {c["day"] for c in combos} == {"2026-10-10"}  # nur 1 Spiel am 11.10.
    for c in combos:
        assert len({l["match_id"] for l in c["legs"]}) == c["size"]
        assert all(0.75 <= l["prob"] <= 0.88 and l["market"] != "OU0.5" for l in c["legs"])
        assert c["prob"] == pytest.approx(__import__("math").prod(l["prob"] for l in c["legs"]))


def test_day_combos_widen_range_when_too_few():
    from fussball.service import day_combos

    fs = [MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.5, 1.0,
                        {}, {}, {}, None, None, 0.0, {}, implied={"OU1.5": {"O": 0.89, "U": 0.11}})
          for i in range(5)]  # 89 %: ausserhalb 75–88 %, innerhalb 70–90 %
    assert day_combos(fs, sizes=(5,), widen=False) == []
    wide = day_combos(fs, sizes=(5,))
    assert len(wide) == 1 and wide[0]["size"] == 5 and wide[0]["widened"]
