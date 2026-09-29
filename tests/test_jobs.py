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
    assert "keine 5 Spiele" in telegram_bot.format_today({"day_combos": []})
