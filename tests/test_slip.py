from datetime import datetime
from types import SimpleNamespace

import pytest

from fussball.agents import slip
from fussball.app import telegram_bot
from fussball.models.implied import implied_markets
from fussball.service import MatchForecast


def forecast(mid, home, away):
    return MatchForecast(mid, "UNL", "Nations League", datetime(2026, 10, 1, 18, 45), home, away, 2.0, 0.8,
                         {}, {}, {}, None, None, 0.0, {}, implied=implied_markets(2.0, 0.8))


def fake_client(legs, total=None):
    usage = SimpleNamespace(input_tokens=2000, output_tokens=300, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    seen = {}

    def parse(**kw):
        seen.update(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=usage,
                               parsed_output=slip.Slip(is_betting_slip=True, legs=legs, total_odds=total))

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))), seen


def test_read_evaluate_and_learn(engine):
    fs = [forecast(1, "Germany", "Serbia"), forecast(2, "Wales", "Norway")]
    p_over = fs[0].implied["OU1.5"]["O"]
    legs = [slip.SlipLeg(home="Deutschland", away="Serbien", market_text="Über 1.5", market="OU", selection="O",
                         line=1.5, odds=round(0.9 / p_over, 2), match_id=1),
            slip.SlipLeg(home="Wales", away="Norwegen", market_text="Handicap", market="andere", selection="",
                         line=None, odds=2.1, match_id=2)]
    client, seen = fake_client(legs)
    data, cost = slip.read_slip(client, b"img", "image/jpeg", ["1: Germany – Serbia (01.10. 20:45)"])
    assert cost > 0 and "1: Germany – Serbia" in seen["messages"][0]["content"][1]["text"]
    assert seen["messages"][0]["content"][0]["type"] == "image"
    rows = slip.evaluate(data, fs)
    assert rows[0]["match"] == "Germany – Serbia" and rows[0]["ratio"] == pytest.approx(0.9, abs=0.01)
    assert rows[1]["fair"] is None  # Handicap: keine faire Quote
    assert slip.remember(engine, rows) == 1 and slip.remember(engine, rows) == 0  # nicht doppelt
    assert slip.ratios(engine) == {}  # erst ab 3 Beobachtungen
    text = telegram_bot.format_slip(rows, None, 1)
    assert "❌" in text and "keine faire Quote" in text and "gelernt" in text


def test_estimate_after_enough_observations(engine):
    fs = [forecast(i, f"H{i}", f"A{i}") for i in range(3)]
    rows = [{"leg": slip.SlipLeg(home=f.home, away=f.away, market_text="Über 2.5", market="OU", selection="O",
                                 line=2.5, odds=round(0.92 / f.implied["OU2.5"]["O"], 3), match_id=f.match_id),
             "match_id": f.match_id, "key": "OU2.5", "fair": 1 / f.implied["OU2.5"]["O"], "ratio": 0.92}
            for f in fs]
    slip.remember(engine, rows)
    table = slip.ratios(engine)
    assert table["OU"]["n"] == 3 and table["OU"]["ratio"] == pytest.approx(0.92, abs=0.01)
    assert slip.estimate(table, "OU1.5", 1.25) == pytest.approx(1.15, abs=0.01)
    assert slip.estimate(table, "DC", 1.25) is None
