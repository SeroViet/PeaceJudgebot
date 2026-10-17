import pytest

from fussball.betting.combos import Combo, build_combos, eligible_legs, system_bet
from fussball.betting.value import Tip, edge, kelly, select_singles, stake

RULES = {"min_edge": 0.05, "min_prob": 0.0, "max_odds": 6.0, "kelly_fraction": 0.25,
         "max_stake_pct": 0.02, "daily_limit_pct": 0.06, "weekly_limit_pct": 0.15}
COMBO_RULES = {"min_legs": 4, "max_legs": 5, "leg_min_prob": 0.6, "leg_min_edge": 0.03,
               "leg_min_odds": 1.5, "min_total_prob": 0.05, "boost": {}}


def tip(mid, prob, odds, sel="H", market="1X2"):
    return Tip(mid, f"M{mid}", "2026-10-03 13:30", "D1", market, 0.0, sel, sel, prob, odds, "Avg",
               market_prob=prob - 0.02)


def test_edge_and_kelly():
    assert edge(0.5, 2.2) == pytest.approx(0.1)
    assert kelly(0.5, 2.2) == pytest.approx(0.1 / 1.2)
    assert kelly(0.4, 2.0) == 0.0
    assert stake(0.5, 2.2, 1000, 0.25, 0.02) == pytest.approx(20.0)  # durch max 2 % begrenzt
    assert stake(0.5, 2.05, 1000, 0.25, 0.05) == pytest.approx(5.95, abs=0.01)


def test_singles_respect_threshold_one_per_match_and_daily_limit():
    tips = [tip(1, 0.55, 2.0), tip(1, 0.30, 4.0, "A"), tip(2, 0.5, 2.0), *[tip(i, 0.6, 2.0) for i in range(3, 10)]]
    picks = select_singles(tips, RULES, bankroll=1000)
    mids = [t.match_id for t, _ in picks]
    assert 2 not in mids  # Edge 0 %
    assert len(mids) == len(set(mids))
    assert sum(s for _, s in picks) <= 60.0 + 1e-9  # Tageslimit 6 %


def test_combo_only_value_legs_and_independent():
    tips = [tip(i, 0.66, 1.65) for i in range(1, 7)] + [tip(1, 0.7, 1.5, "O", "OU"), tip(9, 0.62, 1.5)]
    legs = eligible_legs(tips, COMBO_RULES)
    assert len({t.match_id for t in legs}) == len(legs)
    assert all(t.edge >= 0.03 for t in legs)
    combos = build_combos(tips, COMBO_RULES)
    assert combos and {c.variant for c in combos} <= {"sicher", "ausgewogen", "hoher EV"}
    for c in combos:
        assert 4 <= len(c.legs) <= 5
        assert c.ev > 0
        assert c.prob == pytest.approx(0.66 ** len(c.legs), rel=0.2)


def test_no_combo_without_value():
    tips = [tip(i, 0.6, 1.6) for i in range(1, 7)]  # Edge -4 %
    assert build_combos(tips, COMBO_RULES) == []


def test_combo_boost_and_streak():
    legs = [tip(i, 0.7, 1.6) for i in range(4)]
    plain, boosted = Combo("x", legs), Combo("x", legs, boost=0.1)
    assert boosted.odds > plain.odds
    assert plain.losing_streak_prob(5) == pytest.approx((1 - 0.7**4) ** 5)


def test_system_bet_ev_matches_single_combo_for_full_system():
    legs = [tip(i, 0.7, 1.6) for i in range(4)]
    full = system_bet(legs, 4)
    assert full["ev"] == pytest.approx(Combo("x", legs).ev)
    three_of_four = system_bet(legs, 3)
    assert three_of_four["bets"] == 4 and three_of_four["p_return"] > full["p_return"]
