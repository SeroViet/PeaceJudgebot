from datetime import datetime
from types import SimpleNamespace

import pytest

from fussball.agents import slip
from fussball.app import telegram_bot
from fussball.models.builder import Part, joint_prob
from fussball.models.implied import implied_markets
from fussball.service import MatchForecast


def forecast(mid, home, away, lam=2.0, mu=0.8):
    return MatchForecast(mid, "UNL", "Nations League", datetime(2026, 10, 1, 18, 45), home, away, lam, mu,
                         {}, {}, {}, None, None, 0.0, {}, implied=implied_markets(lam, mu), implied_rates=(lam, mu))


def part(market, sel, line=None, team=None, half="ft"):
    return slip.SlipPart(market=market, selection=sel, line=line, team=team, half=half)


def leg(text, parts, odds, mid, boosted=False, home="Deutschland", away="Serbien"):
    return slip.SlipLeg(home=home, away=away, market_text=text, parts=parts, odds=odds, boosted=boosted, match_id=mid)


def fake_client(legs, total=None):
    usage = SimpleNamespace(input_tokens=2000, output_tokens=300, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    seen = {}

    def parse(**kw):
        seen.update(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=usage,
                               parsed_output=slip.Slip(is_betting_slip=True, bookmaker="Sporttip", legs=legs, total_odds=total))

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))), seen


def test_read_evaluate_and_learn(engine):
    fs = [forecast(1, "Germany", "Serbia"), forecast(2, "Wales", "Norway")]
    p_over = fs[0].implied["OU1.5"]["O"]
    legs = [leg("Über 1.5", [part("OU", "O", 1.5)], round(0.9 / p_over, 2), 1),
            leg("Torschütze Musiala", [part("andere", "")], 2.1, 2, home="Wales", away="Norwegen")]
    client, seen = fake_client(legs)
    data, cost = slip.read_slip(client, b"img", "image/jpeg", ["1: Germany – Serbia (01.10. 20:45)"])
    assert cost > 0 and "1: Germany – Serbia" in seen["messages"][0]["content"][1]["text"]
    assert seen["messages"][0]["content"][0]["type"] == "image"
    rows = slip.evaluate(data, fs)
    assert rows[0]["match"] == "Germany – Serbia" and rows[0]["ratio"] == pytest.approx(0.9, abs=0.01)
    assert rows[0]["key"] == "OU1.5"
    assert rows[1]["fair"] is None  # Torschütze: nicht berechenbar
    assert slip.remember(engine, rows) == 1 and slip.remember(engine, rows) == 0  # nicht doppelt
    assert slip.ratios(engine) == {}  # erst ab 3 Beobachtungen
    text = telegram_bot.format_slip(rows, [{"assessment": "bestätigt", "reason": "Stammelf komplett"},
                                           {"assessment": None, "reason": "nicht geprüft"}])
    assert "🟢" in text and "Stammelf komplett" in text and "⚪" in text and "fair" not in text


def test_betbuilder_handicap_halftime_and_boost(engine):
    f = forecast(1, "Germany", "Serbia", 2.3, 0.7)
    parts = [part("BTTS", "N"), part("HCP", "H", -1), part("OU", "U", 1.5, half="1h")]
    fair = 1 / joint_prob(2.3, 0.7, [Part("BTTS", "N"), Part("HCP", "H", -1), Part("OU", "U", 1.5, "1h")])
    boost = leg("Deutschland gewinnt (Boost)", [part("1X2", "H")], round(1.15 / f.implied["1X2"]["H"], 2), 1,
                boosted=True)
    data = slip.Slip(is_betting_slip=True, bookmaker="Bet365", total_odds=None,
                     legs=[leg("BetBuilder", parts, 4.55, 1), boost])
    rows = slip.evaluate(data, [f])
    assert rows[0]["builder"] and rows[0]["key"] == "BB" and rows[0]["fair"] == pytest.approx(fair)
    assert rows[1]["ratio"] > 1.1
    text = telegram_bot.format_slip(rows, [{"assessment": "bestätigt", "reason": ""},
                                           {"assessment": "streichen", "reason": "Torjäger gesperrt"}], "Bet365")
    assert "2 von 2 Tipps rot" in text and "Torjäger gesperrt" in text and "Nur ca. 19% Chance" in text
    assert slip.remember(engine, rows) == 1  # Boost wird nicht gelernt


def test_estimate_after_enough_observations(engine):
    fs = [forecast(i, f"H{i}", f"A{i}") for i in range(3)]
    rows = []
    for f in fs:
        p = f.implied["OU2.5"]["O"]
        rows.append({"leg": leg("Über 2.5", [part("OU", "O", 2.5)], round(0.92 / p, 3), f.match_id),
                     "match_id": f.match_id, "key": "OU2.5", "fair": 1 / p, "ratio": 0.92})
    slip.remember(engine, rows)
    table = slip.ratios(engine)
    assert table["OU"]["n"] == 3 and table["OU"]["ratio"] == pytest.approx(0.92, abs=0.01)
    assert slip.estimate(table, "OU1.5", 1.25) == pytest.approx(1.15, abs=0.01)
    assert slip.estimate(table, "DC", 1.25) is None


def test_bookmakers_learned_separately(engine):
    fs = [forecast(i, f"H{i}", f"A{i}") for i in range(3)]
    for book, ratio in (("Sporttip", 0.90), ("Bet365", 0.95)):
        rows = []
        for f in fs:
            p = f.implied["OU2.5"]["O"]
            rows.append({"leg": leg("Über 2.5", [part("OU", "O", 2.5)], round(ratio / p, 3), f.match_id),
                         "match_id": f.match_id, "key": "OU2.5", "fair": 1 / p, "ratio": ratio,
                         "match": f"{f.home} – {f.away}", "prob": p})
        assert slip.remember(engine, rows, book) == 3
    assert slip.ratios(engine)["OU"]["ratio"] == pytest.approx(0.90, abs=0.01)
    assert slip.ratios(engine, "Bet365")["OU"]["ratio"] == pytest.approx(0.95, abs=0.01)
    assert "Bet365-Schein geprüft" in telegram_bot.format_slip(rows, [{}] * 3, "Bet365")


def test_german_names_match_english_data():
    fs = [forecast(1, "Germany", "Serbia"), forecast(2, "Republic of Ireland", "Austria"), forecast(3, "Wales", "Norway")]
    data = slip.Slip(is_betting_slip=True, bookmaker="Bet365", total_odds=None, legs=[
        leg("Deutschland – Endergebnis", [part("1X2", "H")], 1.25, None),
        leg("Österreich – Endergebnis", [part("1X2", "A")], 2.0, None, home="Irland", away="Österreich"),
        leg("Norwegen – Endergebnis", [part("1X2", "A")], 1.5, None, home="Wales", away="Norwegen")])
    rows = slip.evaluate(data, fs)
    assert [r.get("match_id") for r in rows] == [1, 2, 3] and all(r["prob"] for r in rows)
