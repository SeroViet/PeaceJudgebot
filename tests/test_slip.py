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


def test_tracking_green_red(engine, fixture_bytes, bundesliga):
    from sqlalchemy import select

    from fussball import tracking
    from fussball.data import football_data as fd
    from fussball.data.db import session_scope
    from fussball.data.schema import Match

    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2627", fixture_bytes("D1_2627_sample.csv"))
        m1, m2 = s.scalars(select(Match)).all()[:2]
        for m in (m1, m2):
            m.status, m.ft_home, m.ft_away, m.ht_home, m.ht_away = "scheduled", None, None, None, None
        ids = (m1.id, m2.id)
    rows = [{"leg": leg("Über 1.5 Tore", [part("OU", "O", 1.5)], 1.3, ids[0]), "match_id": ids[0], "match": "A – B"},
            {"leg": leg("1X", [part("DC", "1X")], 1.2, ids[1]), "match_id": ids[1], "match": "C – D"},
            {"leg": leg("Torschütze", [part("andere", "")], 2.0, None), "match_id": None, "match": "E – F"}]
    assert tracking.track(engine, rows) == 2 and tracking.track(engine, rows) == 0  # nicht doppelt
    assert tracking.settle(engine) == []  # noch nicht gespielt
    with session_scope(engine) as s:
        a, b = s.get(Match, ids[0]), s.get(Match, ids[1])
        a.status, a.ft_home, a.ft_away = "finished", 2, 1
        b.status, b.ft_home, b.ft_away = "finished", 0, 1
    done = tracking.settle(engine)
    assert [d["won"] for d in done] == [True, False]
    text = telegram_bot.format_results(done)
    assert "🟢 <b>A – B</b>\n" in text and "🔴 <b>C – D</b>\n" in text  # nur die Namen
    assert "Über 1.5" not in text and "2:1" not in text
    assert "1 von 2 gewonnen" in text and "fair" not in text
    batches = tracking.finished_batches(engine)
    assert len(batches) == 1 and len(batches[0][1]) == 2  # eine Meldung, wenn alle Spiele des Scheins fertig sind
    assert tracking.finished_batches(engine) == []  # nur einmal melden


def test_bot_top_tips_tracked_per_day(engine, fixture_bytes, bundesliga):
    from sqlalchemy import select

    from fussball import tracking
    from fussball.data import football_data as fd
    from fussball.data.db import session_scope
    from fussball.data.schema import Match

    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2627", fixture_bytes("D1_2627_sample.csv"))
        m1, m2 = s.scalars(select(Match)).all()[:2]
        for m, sc in ((m1, (1, 1)), (m2, None)):
            m.status = "scheduled" if sc is None else "finished"
            m.ft_home, m.ft_away = sc if sc else (None, None)
            m.ht_home, m.ht_away = (0, 1) if sc else (None, None)
        ids = (m1.id, m2.id)
    tips = [{"match_id": ids[0], "match": "A – B", "kickoff": "2026-10-04T13:00:00", "label": "1. Halbzeit: X2",
             "market": "H1_DC", "selection": "X2"},
            {"match_id": ids[1], "match": "C – D", "kickoff": "2026-10-04T18:00:00", "label": "Über 1.5 Tore",
             "market": "OU1.5", "selection": "O"}]
    assert tracking.track_tips(engine, tips, "2026-10-04") == 2
    tracking.settle(engine)
    assert tracking.finished_batches(engine) == []  # zweites Spiel läuft noch → noch keine Meldung
    with session_scope(engine) as s:
        m = s.get(Match, ids[1])
        m.status, m.ft_home, m.ft_away = "finished", 0, 1
    tracking.settle(engine)
    (title, items), = tracking.finished_batches(engine)
    assert "Top-Tipps 04.10." in title and [i["won"] for i in items] == [True, False]


def test_agent_fills_halftime_for_halftime_tips(engine, fixture_bytes, bundesliga):
    from sqlalchemy import select

    from fussball import tracking
    from fussball.agents import scout
    from fussball.data import football_data as fd
    from fussball.data.db import session_scope
    from fussball.data.schema import Match, utcnow

    with session_scope(engine) as s:
        fd.import_season(s, bundesliga, "2627", fixture_bytes("D1_2627_sample.csv"))
        m = s.scalars(select(Match)).first()
        m.status, m.ft_home, m.ft_away, m.ht_home, m.ht_away = "finished", 2, 1, None, None
        m.kickoff_utc = utcnow()
        mid = m.id
    tips = [{"match_id": mid, "match": "A – B", "kickoff": utcnow().isoformat(), "label": "1. Halbzeit: Über 0.5",
             "market": "H1_OU0.5", "selection": "O"}]
    tracking.track_tips(engine, tips, "2026-10-04")
    assert tracking.settle(engine) == []  # Halbzeitstand fehlt → wartet
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(stop_reason="end_turn", usage=usage,
                                            content=[SimpleNamespace(type="text", text="Halbzeit 1:0")]),
        parse=lambda **kw: SimpleNamespace(stop_reason="end_turn", usage=usage,
                                           parsed_output=scout.HalfTime(found=True, ht_home=1, ht_away=0)))))
    assert tracking.fill_halftime(engine, client) == 1
    done = tracking.settle(engine)
    assert done[0]["won"] is True
