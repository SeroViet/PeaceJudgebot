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


def test_no_12_or_draw_tips_and_goal_tips_preferred():
    from fussball.service import best_tip_per_match

    f = MatchForecast(1, "D1", "BL", datetime(2026, 10, 10, 18, 0), "H", "A", 1.5, 1.0, {}, {}, {}, None, None,
                      0.0, {}, implied={"DC": {"12": 0.84, "1X": 0.80}, "OU1.5": {"O": 0.81, "U": 0.19}})
    tip = best_tip_per_match([f], 0.75, 0.88)[0]
    assert tip["market"] == "OU1.5" and tip["selection"] == "O"  # Tore-Tipp vor 1X, "12" nie
    assert "12" not in {a["selection"] for a in tip["alternatives"]}
    assert {a["label"] for a in tip["alternatives"]} == {"Über 1.5 Tore", "1X (H)"}


def test_risky_combo_uses_other_matches_and_higher_odds_markets():
    from fussball.service import risky_combos

    fs = [MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.6, 1.3, {}, {}, {},
                        None, None, 0.0, {}, implied={"OU2.5": {"O": 0.66, "U": 0.34}, "BTTS": {"Y": 0.40, "N": 0.64},
                                                      "OU1.5": {"O": 0.85, "U": 0.15}})
          for i in range(5)]
    safe = [{"day": "2026-10-10", "legs": [{"match_id": 0}]}]
    combos = risky_combos(fs, safe, size=3)
    assert len(combos) == 1 and combos[0]["risky"]
    legs = combos[0]["legs"]
    assert 0 not in {l["match_id"] for l in legs}  # nicht in der sicheren Kombi
    assert all(l["label"] == "Über 2.5 Tore" for l in legs)  # kein "beide treffen: Nein", kein Über 1.5
    assert combos[0]["fair_odds"] > 3


def test_risky_combo_falls_back_to_safe_combo_matches():
    from fussball.service import risky_combos

    fs = [MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.6, 1.3, {}, {}, {},
                        None, None, 0.0, {}, implied={"OU2.5": {"O": 0.66, "U": 0.34}}) for i in range(3)]
    safe = [{"day": "2026-10-10", "legs": [{"match_id": 0}, {"match_id": 1}]}]
    assert len(risky_combos(fs, safe, size=3)[0]["legs"]) == 3


def test_combo_varies_tips_and_offers_team_goals():
    from fussball.service import day_combos

    fs = [MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.5, 1.0, {}, {}, {},
                        None, None, 0.0, {}, implied={"OU1.5": {"O": 0.85, "U": 0.15},
                                                      "HOME1.5": {"O": 0.80, "U": 0.20},
                                                      "HOME0.5": {"O": 0.95, "U": 0.05}}) for i in range(5)]
    legs = day_combos(fs, sizes=(5,))[0]["legs"]
    labels = [l["label"] for l in legs]
    # höchstens 2× derselbe Tipp; erst wenn es nicht anders geht (nur 2 Tipp-Arten), wird aufgefüllt
    assert sum("über 1.5 Tore (Team)" in x for x in labels) == 2 and labels.count("Über 1.5 Tore") == 3
    assert not any("0.5" in x for x in labels)  # keine 0.5-Tipps


def test_krass_combo_high_odds():
    from fussball.service import KRASS_MARKETS, risky_combos

    fs = [MatchForecast(i, "D1", "BL", datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.6, 1.3, {}, {}, {},
                        None, None, 0.0, {}, implied={"OU2.5": {"O": 0.62, "U": 0.38}, "BTTS": {"Y": 0.60, "N": 0.40},
                                                      "OU1.5": {"O": 0.85, "U": 0.15}}) for i in range(6)]
    c = risky_combos(fs, [], size=5, min_prob=0.55, max_prob=0.70, markets=KRASS_MARKETS)[0]
    assert c["size"] == 5 and c["fair_odds"] > 8
    labels = [l["label"] for l in c["legs"]]
    assert "Über 1.5 Tore" not in labels and labels.count("Über 2.5 Tore") <= 3


def test_categories_never_mixed():
    from fussball.service import category, day_combos

    assert category("soccer_uefa_champs_league_women", "UEFA Champions League Women") == "frauen"
    assert category("soccer_uefa_nations_league", "UEFA Nations League") == "national"
    assert category("soccer_fifa_world_cup_qualifiers_europe") == "national"
    assert category("soccer_uefa_champs_league", "UEFA Champions League") == "europa"
    assert category("D1", "Bundesliga") == "liga_eu" and category("soccer_epl", "EPL") == "liga_eu"
    assert category("soccer_argentina_primera_division", "Primera División - Argentina") == "suedamerika"
    assert category("soccer_brazil_campeonato", "Brazil Série A") == "suedamerika"  # nicht Italien
    assert category("soccer_usa_mls", "MLS") == "nordamerika"
    assert category("soccer_japan_j_league", "J League") == "asien"
    fs = []
    for i, (code, name) in enumerate([("soccer_uefa_nations_league", "UEFA Nations League")] * 3
                                     + [("soccer_argentina_primera_division", "Primera División")] * 3):
        fs.append(MatchForecast(i, code, name, datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.5, 1.0, {}, {},
                                {}, None, None, 0.0, {}, implied={"OU1.5": {"O": 0.82, "U": 0.18}}))
    combos = day_combos(fs, sizes=(3,))
    assert {c["cat"] for c in combos} == {"national", "suedamerika"}
    for c in combos:
        assert len({category(l["comp"], l["comp_name"]) for l in c["legs"]}) == 1  # nie gemischt

def test_halftime_tips_available_and_calibrated_markets_only():
    from fussball.models.builder import halftime_markets
    from fussball.service import COMBO_MARKETS, best_tip_per_match

    ht = halftime_markets(1.2, 1.0)
    assert abs(sum(ht["H1_1X2"].values()) - 1) < 1e-6 and ht["H1_DC"]["1X"] > ht["H1_1X2"]["H"]
    assert "HCP" not in " ".join(COMBO_MARKETS) and "H1_OU0.5" in COMBO_MARKETS
    assert not any(m.endswith("0.5") and not m.startswith("H1_") for m in COMBO_MARKETS)  # ganzes Spiel: kein 0.5
    f = MatchForecast(1, "D1", "BL", datetime(2026, 10, 10, 18, 0), "H", "A", 1.2, 1.0, {}, {}, {}, None, None, 0.0, {},
                      implied={"H1_DC": {"1X": 0.80, "X2": 0.70, "12": 0.5}, "OU2.5": {"O": 0.5, "U": 0.5}})
    tip = best_tip_per_match([f], 0.75, 0.88)[0]
    assert tip["market"] == "H1_DC" and tip["label"].startswith("1. Halbzeit: 1X")


def test_no_womens_football_in_plan(engine, monkeypatch):
    from fussball import service

    fs = [MatchForecast(i, code, name, datetime(2026, 10, 10, 12 + i, 0), f"H{i}", f"A{i}", 1.5, 1.0, {}, {}, {},
                        None, None, 0.0, {}, implied={"OU1.5": {"O": 0.82, "U": 0.18}})
          for i, (code, name) in enumerate([("soccer_uefa_champs_league_women", "UCL Women")] * 3
                                           + [("soccer_epl", "EPL")] * 3)]
    monkeypatch.setattr(service, "market_forecasts", lambda engine, hours: fs)
    plan = service.daily_plan(engine, days=3, forecasts=[])
    assert {f.comp for f in plan.all_forecasts} == {"soccer_epl"}
    assert all(c["cat"] == "liga_eu" for c in plan.day_combos)


def test_only_europe(monkeypatch):
    from fussball.service import region_ok

    assert region_ok("soccer_uefa_nations_league", "UEFA Nations League")
    assert region_ok("soccer_uefa_europa_league", "UEFA Europa League")
    assert region_ok("soccer_epl", "EPL") and region_ok("D1", None)
    assert region_ok("soccer_fifa_world_cup_qualifiers_europe", "FIFA World Cup Qualifiers - Europe")
    assert region_ok("soccer_brazil_campeonato", "Brazil Série A")  # Brasilien auf Wunsch dabei
    assert region_ok("soccer_brazil_serie_b", "Brazil Série B")
    for comp, name in (("soccer_japan_j_league", "J League"),
                       ("soccer_argentina_primera_division", "Primera División - Argentina"),
                       ("soccer_usa_mls", "MLS"), ("soccer_fifa_world_cup_qualifiers_south_america", "WC Qual SA"),
                       ("soccer_international_friendlies", "International Friendlies"),
                       ("soccer_uefa_champs_league_women", "UCL Women")):
        assert not region_ok(comp, name), comp
    assert not region_ok("soccer_club_friendlies", "Club Friendlies")
    monkeypatch.setenv("TIP_REGIONS", "welt")
    assert region_ok("soccer_japan_j_league", "J League")
    assert not region_ok("soccer_international_friendlies", "International Friendlies")  # Testspiele nie


def test_match_type_decides_the_tip():
    from fussball.models.builder import halftime_markets
    from fussball.service import best_tip_per_match, builder_legs, match_profile

    def fc(i, lam, mu):
        return MatchForecast(i, "soccer_uefa_nations_league", "UEFA Nations League", datetime(2026, 10, 4, 18, 45),
                             f"H{i}", f"A{i}", lam, mu, {}, {}, {}, None, None, 0.0, {},
                             implied={**implied_markets(lam, mu), **halftime_markets(lam, mu)}, implied_rates=(lam, mu))

    greece_germany, nl_serbia, por_nor = fc(1, 1.00, 1.67), fc(2, 3.26, 0.59), fc(3, 2.40, 1.37)
    assert [match_profile(*f.implied_rates) for f in (greece_germany, nl_serbia, por_nor)] == ["zaeh", "favorit", "offen"]
    tips = {t["match_id"]: t for t in best_tip_per_match([greece_germany, nl_serbia, por_nor], 0.55, 0.90)}
    # zähes Spiel: keine Tore-Tipps
    assert (tips[1]["market"], tips[1]["selection"]) not in {("AWAY1.5", "O"), ("OU3.5", "O"), ("OU2.5", "O")}
    assert all((a["market"], a["selection"]) != ("AWAY1.5", "O") for a in tips[1]["alternatives"])
    # Länderspiel, Favorit gegen Schwächeren: kein Bonus mehr für Teamtore, aber erlaubt
    assert tips[2]["profile"] == "favorit" and (tips[2]["market"], tips[2]["selection"]) != ("BTTS", "Y")
    # Länderspiel, Favorit auswärts: "Team über 1.5" nur ab 75 % (Griechenland – Deutschland 0:0)
    away = {t["match_id"]: t for t in best_tip_per_match([fc(4, 0.7, 2.2), fc(5, 0.5, 2.9)], 0.45, 0.90)}
    assert all((a["market"], a["selection"]) != ("AWAY1.5", "O") for a in [away[4], *away[4]["alternatives"]])
    assert any((a["market"], a["selection"]) == ("AWAY1.5", "O") for a in [away[5], *away[5]["alternatives"]])
    # offenes Spiel: kein Unter-Tipp
    assert tips[3]["selection"] != "U" and tips[3]["profile"] == "offen"
    # BetBuilder im zähen Spiel ohne Tore-Teile
    for leg in builder_legs([greece_germany], 0.3, 0.6):
        assert not any((p[0] == "OU" and p[1] == "O" and p[2] >= 2.5) or (p[0] in ("AWAY", "HOME") and p[1] == "O")
                       or (p[0] == "BTTS" and p[1] == "Y") for p in leg["parts"])


def test_under_45_only_in_tough_matches():
    from fussball.models.builder import halftime_markets
    from fussball.service import best_tip_per_match

    def fc(i, lam, mu):
        return MatchForecast(i, "soccer_epl", "EPL", datetime(2026, 10, 4, 18, 45), f"H{i}", f"A{i}", lam, mu, {}, {},
                             {}, None, None, 0.0, {}, implied={**implied_markets(lam, mu), **halftime_markets(lam, mu)},
                             implied_rates=(lam, mu))

    tips = {t["match_id"]: t for t in best_tip_per_match([fc(1, 1.0, 1.6), fc(2, 1.7, 1.3)], 0.75, 0.90)}
    assert (tips[1]["market"], tips[1]["selection"]) == ("OU4.5", "U") and tips[1]["label"] == "Unter 4.5 Tore"
    labels2 = [tips[2]["label"]] + [a["label"] for a in tips[2]["alternatives"]]
    assert "Unter 4.5 Tore" not in labels2  # nicht im normalen Spiel


def test_league_favourite_still_prefers_team_over_15():
    from fussball.models.builder import halftime_markets
    from fussball.service import best_tip_per_match

    lam, mu = 3.26, 0.59
    f = MatchForecast(1, "soccer_epl", "EPL", datetime(2026, 10, 4, 18, 45), "H", "A", lam, mu, {}, {}, {}, None,
                      None, 0.0, {}, implied={**implied_markets(lam, mu), **halftime_markets(lam, mu)},
                      implied_rates=(lam, mu))
    tip = best_tip_per_match([f], 0.55, 0.90)[0]
    assert (tip["market"], tip["selection"]) == ("HOME1.5", "O")


def test_last_meeting_blocks_goal_tips(engine):
    from datetime import timedelta

    from fussball.data.db import session_scope
    from fussball.data.schema import Competition, Match, Team
    from fussball.models.builder import halftime_markets
    from fussball.service import best_tip_per_match, mark_last_meetings

    with session_scope(engine) as s:
        c = Competition(code="UNL", name="UEFA Nations League", kind="international")
        gre, ger = Team(name="Griechenland", is_national=True), Team(name="Deutschland", is_national=True)
        s.add_all([c, gre, ger])
        s.flush()
        kick = datetime(2026, 10, 4, 18, 45)
        s.add(Match(competition_id=c.id, season="2026-27", kickoff_utc=kick - timedelta(days=7), status="finished",
                    home_team_id=ger.id, away_team_id=gre.id, ft_home=0, ft_away=1, source="test"))
        nxt = Match(competition_id=c.id, season="2026-27", kickoff_utc=kick, home_team_id=gre.id, away_team_id=ger.id, source="test")
        s.add(nxt)
        s.flush()
        mid = nxt.id
    lam, mu = 1.0, 2.2  # Markt: Deutschland klarer Favorit
    f = MatchForecast(mid, "soccer_uefa_nations_league", "UEFA Nations League", kick, "Griechenland", "Deutschland",
                      lam, mu, {}, {}, {}, None, None, 0.0, {},
                      implied={**implied_markets(lam, mu), **halftime_markets(lam, mu)}, implied_rates=(lam, mu))
    mark_last_meetings(engine, [f])
    assert f.last_meeting == (1, 0)  # aus Sicht des Heimteams Griechenland
    tips = best_tip_per_match([f], 0.45, 0.95)
    picked = {(a["market"], a["selection"]) for a in [tips[0], *tips[0]["alternatives"]]}
    assert not picked & {("OU1.5", "O"), ("OU2.5", "O"), ("AWAY1.5", "O"), ("BTTS", "Y")}


def test_scan_priority_big_competitions_first():
    from fussball.service import _priority, region_ok

    order = sorted(["soccer_sweden_superettan", "soccer_epl", "soccer_uefa_nations_league",
                    "soccer_switzerland_superleague", "soccer_uefa_champs_league"], key=_priority)
    assert order[:2] == ["soccer_uefa_nations_league", "soccer_uefa_champs_league"]
    assert order[-1] == "soccer_sweden_superettan"
    assert region_ok("soccer_fa_cup", "FA Cup")


def test_forecast_without_pinnacle_uses_bookmaker_consensus(engine):
    from datetime import timedelta

    from fussball.data.db import session_scope
    from fussball.data.schema import Competition, Match, Odds, Team, utcnow
    from fussball.service import market_forecasts

    now = utcnow()
    with session_scope(engine) as s:
        c = Competition(code="soccer_uefa_nations_league", name="UEFA Nations League", kind="international")
        h, a = Team(name="Zypern"), Team(name="Lettland")
        s.add_all([c, h, a])
        s.flush()
        m = Match(competition_id=c.id, season="2026-27", kickoff_utc=now + timedelta(hours=5), home_team_id=h.id,
                  away_team_id=a.id, source="odds-api")
        s.add(m)
        s.flush()
        for bm, (ph, pd, pa) in {"B365": (1.58, 3.9, 5.8), "UNIBET": (1.6, 3.8, 5.6), "BWIN": (1.57, 4.0, 5.9)}.items():
            for sel, price in zip("HDA", (ph, pd, pa)):
                s.add(Odds(match_id=m.id, bookmaker=bm, market="1X2", line=0.0, selection=sel, price=price,
                           known_at=now, source="odds-api"))
            for sel, price in (("O", 1.95), ("U", 1.85)):
                s.add(Odds(match_id=m.id, bookmaker=bm, market="OU", line=2.5, selection=sel, price=price,
                           known_at=now, source="odds-api"))
    fs = market_forecasts(engine, hours=24)
    assert len(fs) == 1 and fs[0].reference == "Ø"
    assert 0.55 < fs[0].probs_1x2["H"] < 0.65 and fs[0].implied  # Spiel ohne Pinnacle trotzdem dabei
