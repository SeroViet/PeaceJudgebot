from datetime import datetime

from fussball.app import jobs, telegram_bot

PLAN = {"currency": "CHF", "combos": [], "forecasts": [], "singles": [
    {"id": "1", "match_id": 7, "match": "Como – Roma", "kickoff": "2026-10-11T10:30:00", "comp": "I1",
     "market": "1X2", "selection": "A", "label": "Auswärtssieg Roma", "prob": 0.372, "odds": 2.8,
     "min_odds": 2.77, "edge": 0.042, "stake": 5.0, "bookmaker": "1xBet", "fair_odds": 2.69}]}


def test_reminder_only_in_window_and_once():
    already: set[str] = set()
    assert jobs.due_reminders(PLAN, datetime(2026, 10, 11, 8, 0), 60, already) == []
    due = jobs.due_reminders(PLAN, datetime(2026, 10, 11, 9, 30), 60, already)
    assert len(due) == 1 and "Mindestquote" not in due[0][1] and "2.77" in due[0][1] and "/gesetzt 1 5 2.80" in due[0][1]
    already.add(due[0][0])
    assert jobs.due_reminders(PLAN, datetime(2026, 10, 11, 9, 35), 60, already) == []


def test_format_settled_shows_result_and_clv():
    text = jobs.format_settled([{"status": "won", "text": "Como – Roma 1:2 · Auswärtssieg", "odds": 2.8,
                                 "pnl": 9.0, "clv": 0.031}])
    assert "✅" in text and "+9.00 CHF" in text and "+3.1%" in text


def test_telegram_formatters():
    assert "Mindestquote 2.77" in telegram_bot.format_singles(PLAN)
    assert "Kein" in telegram_bot.format_singles({**PLAN, "singles": []})
    assert "Keine Kombi" in telegram_bot.format_combos(PLAN)
    assert "Kein Spiel" in telegram_bot.format_match(PLAN, "Bayern")


def test_combo_result_and_today_formatting():
    c = {"size": 5, "day": "2026-10-01", "correct": 4, "results": [
        {"won": True, "match": "A – B", "score": "2:1", "label": "Über 1.5 Tore"},
        {"won": False, "match": "C – D", "score": "0:0", "label": "Über 1.5 Tore"}]}
    text = telegram_bot.format_combo_result(c)
    assert "verloren" in text and "4 von 5 richtig" in text and "❌ C – D 0:0" in text
    assert "keine 3 Spiele" in telegram_bot.format_today({"day_combos": []})


def _combo():
    legs = [{"match_id": i, "match": f"H{i} – A{i}", "kickoff": "2026-10-10T18:00:00", "comp": "D1",
             "label": "Über 1.5 Tore", "prob": 0.8, "market": "OU1.5", "selection": "O"} for i in range(5)]
    return {"day_combos": [{"id": "T1", "day": "2026-10-10", "size": 5, "legs": legs, "prob": 0.8 ** 5,
                            "fair_odds": 1 / 0.8 ** 5}]}


def test_sporttip_check_per_leg_and_total():
    plan = _combo()
    good = telegram_bot.check_sporttip(plan, "t1", [1.30] * 5)  # 1.30 > fair 1.25
    assert "Gute Quote" in good and good.count("✅") >= 5
    bad = telegram_bot.check_sporttip(plan, "T1", [2.40])  # fair gesamt 3.05
    assert "Zu tiefe Quote" in bad
    assert "5 Quoten" in telegram_bot.check_sporttip(plan, "T1", [1.3, 1.3])
    assert "nicht gefunden" in telegram_bot.check_sporttip(plan, "T9", [2.0])


def test_day_combo_shows_min_and_live_odds():
    plan = _combo()
    for l in plan["day_combos"][0]["legs"]:
        l.update(book_odds=1.33, book="Bet365", ps_odds=1.27, odds_at="2026-10-10T08:00:00")
    plan["day_combos"][0]["book_odds"] = 1.33 ** 5
    text = telegram_bot.format_day_combos(plan)
    assert "Sporttip mind. <b>1.25</b>" in text and "live 1.33 (Bet365)" in text
    assert "Live-Gesamtquote" in text and "/sporttip T1" in text


def test_today_without_combo_lists_remaining_games():
    from datetime import datetime, timedelta

    from fussball.app import state

    later = (datetime.now(state.TZ) + timedelta(minutes=30))
    if later.date() != datetime.now(state.TZ).date():
        return  # kurz vor Mitternacht nicht prüfbar
    kick = later.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    plan = {"day_combos": [], "safe": [{"match_id": 1, "match": "Lyon – Chelsea", "kickoff": kick,
                                        "label": "Über 1.5 Tore", "prob": 0.8}]}
    text = telegram_bot.format_today(plan)
    assert "Heute noch als Einzeltipps" in text and "Lyon – Chelsea" in text and "1.25" in text


def test_long_messages_split_at_paragraphs():
    blocks = [f"<b>Kombi {i}</b>\n" + "x" * 900 for i in range(10)]
    parts = telegram_bot.chunks("\n\n".join(blocks))
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)
    assert all(p.count("<b>") == p.count("</b>") for p in parts)  # kein Tag zerschnitten
    assert "".join(parts).replace("\n", "") == "".join(blocks).replace("\n", "")
