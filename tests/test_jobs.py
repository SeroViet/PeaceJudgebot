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


def test_day_combo_only_tips_no_fair_talk():
    plan = _combo()
    plan["day_combos"][0]["legs"][0]["agent"] = {"assessment": "vorsicht", "reason": "Torjäger fällt aus"}
    text = telegram_bot.format_day_combos(plan)
    assert "Chance gesamt" in text and "🔴" in text and "Torjäger fällt aus" in text
    assert "fair" not in text and "Sporttip mind." not in text

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


def test_top5_short_and_by_category():
    from datetime import datetime, timedelta

    from fussball.app import state

    later = datetime.now(state.TZ) + timedelta(minutes=30)
    if later.date() != datetime.now(state.TZ).date():
        return
    kick = later.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    safe = [{"match_id": i, "match": f"H{i} – A{i}", "kickoff": kick, "comp": comp, "comp_name": name,
             "market": "OU1.5", "selection": "O", "label": "Über 1.5 Tore", "prob": 0.8 + i / 100}
            for i, (comp, name) in enumerate([("soccer_uefa_nations_league", "UEFA Nations League")] * 6
                                             + [("soccer_argentina_primera_division", "Primera División")] * 2)]
    safe.append({**safe[0], "market": "OU4.5", "selection": "U", "label": "Unter 4.5 Tore", "prob": 0.89})
    text = telegram_bot.format_top5({"safe": safe, "day_combos": [], "risky_combos": [], "krass_combos": []})
    assert "🌍 Länderspiele" in text and "🌎 Südamerika" in text
    assert text.count("➡️") == 6  # die 6 sichersten Tipps insgesamt, ein Tipp pro Spiel
    assert "Unter 4.5" not in text and len(text) < 1500
    assert "⚪ = nicht geprüft" in text  # noch nicht geprüft: trotzdem 6 Tipps, ehrlich markiert
    # nach der Prüfung: immer 6 Tipps – bestätigte 🟢 zuerst, dann ungeprüfte ⚪, gewarnte 🔴 nur zum Auffüllen
    checked = [{**t, "agent": "vorsicht", "reason": "Torwart fehlt"} if t["match_id"] == 7
               else {**t, "agent": "streichen"} if t["match_id"] == 6
               else {**t, "agent": "bestätigt"} if t["match_id"] in (3, 5) else t for t in safe]
    text = telegram_bot.format_top5({"top": checked})
    assert text.count("➡️") == 6 and text.count("🟢") >= 2
    assert "H6 – A6" not in text  # gestrichen: nie
    assert "H7 – A7" not in text  # gewarnt: nicht nötig, es gibt genug bessere
    assert "H3 – A3" in text and "H5 – A5" in text
    # Tipps zwischen 70 und 76 % füllen auf; unter 70 % nie
    assert telegram_bot.format_top5({"top": [{**t, "prob": 0.72} for t in safe]}).count("➡️") == 6
    assert "📭" in telegram_bot.format_top5({"top": [{**t, "prob": 0.68} for t in safe]})


def test_warned_tips_only_fill_up_with_reason():
    from datetime import datetime, timedelta

    from fussball.app import state

    later = datetime.now(state.TZ) + timedelta(minutes=30)
    if later.date() != datetime.now(state.TZ).date():
        return
    kick = later.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    top = [{"match_id": i, "match": f"H{i} – A{i}", "kickoff": kick, "comp": "soccer_epl", "comp_name": "EPL",
            "market": "OU1.5", "selection": "O", "label": "Über 1.5 Tore", "prob": 0.8,
            "agent": "vorsicht" if i == 0 else "bestätigt", "reason": "Torjäger fehlt" if i == 0 else ""}
           for i in range(3)]
    text = telegram_bot.format_top5({"top": top})
    assert text.count("➡️") == 3 and "Torjäger fehlt" in text and "nur klein setzen" in text


def test_boost_combo_tips_from_150(engine, monkeypatch):
    from datetime import datetime, timedelta

    from fussball import service
    from fussball.app import state
    from fussball.service import MatchForecast

    start = (datetime.now(state.TZ) + timedelta(minutes=20))
    if (start + timedelta(hours=6)).date() != start.date():
        return  # kurz vor Mitternacht nicht prüfbar
    kick = start.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None)
    fs = [MatchForecast(i, "soccer_epl", "EPL", kick + timedelta(minutes=30 * i), f"H{i}", f"A{i}", 1.6, 1.3,
                        {}, {}, {}, None, None, 0.0, {},
                        implied={"OU2.5": {"O": 0.60, "U": 0.40}, "OU1.5": {"O": 0.85, "U": 0.15},
                                 "BTTS": {"Y": 0.58, "N": 0.42}}) for i in range(6)]
    monkeypatch.setattr(service, "market_forecasts", lambda engine, hours: fs)
    plan = service.daily_plan(engine, days=3, forecasts=[])
    boost = service.build_extra_combos(plan, "boost")
    assert boost and boost[0]["boost"] and boost[0]["size"] == 5
    assert all(0.55 <= l["prob"] <= 0.62 for l in boost[0]["legs"])
    legs = boost[0]["legs"]
    assert all(telegram_bot.leg_sporttip(l) >= 1.5 for l in legs)  # jeder Tipp ab 1.50 → KombiBoost
    text = telegram_bot.format_boost({"boost_combos": [{**boost[0], "id": "B1"}]})
    assert "Boost-Kombi heute" in text and "Quote ca." in text and "Gesamtquote ca." in text


def test_torfest_only_high_scoring_matches(engine, monkeypatch):
    from datetime import datetime, timedelta

    from fussball import service
    from fussball.app import state
    from fussball.models.builder import halftime_markets
    from fussball.models.implied import implied_markets
    from fussball.service import MatchForecast

    start = datetime.now(state.TZ) + timedelta(minutes=20)
    if (start + timedelta(hours=4)).date() != start.date():
        return
    kick = start.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None)
    rates = [(2.4, 1.5), (2.6, 1.2), (2.0, 1.4), (1.2, 0.9), (2.2, 1.3)]  # Nr. 3 = 2.4 Tore erwartet → zu wenig
    fs = [MatchForecast(i, "soccer_epl", "EPL", kick + timedelta(minutes=10 * i), f"H{i}", f"A{i}", l, m, {}, {}, {},
                        None, None, 0.0, {}, implied={**implied_markets(l, m), **halftime_markets(l, m)},
                        implied_rates=(l, m)) for i, (l, m) in enumerate(rates)]
    monkeypatch.setattr(service, "market_forecasts", lambda engine, hours: fs)
    plan = service.daily_plan(engine, days=3, forecasts=[])
    tf = service.build_extra_combos(plan, "torfest")
    assert tf and tf[0]["size"] == 3
    assert {l["match_id"] for l in tf[0]["legs"]} == {0, 1, 4}  # nur die torreichsten Spiele
    assert all(l["market"] == "H1_OU1.5" and l["selection"] == "O" for l in tf[0]["legs"])
    text = telegram_bot.format_day_combos({"day_combos": [{**tf[0], "id": "F1"}]})
    assert "Torfest-Kombi" in text and "1. Halbzeit: Über 1.5 Tore" in text


def test_daily_catch_up_after_restart(engine):
    from datetime import datetime

    from fussball.agents import runner
    from fussball.app import jobs, state

    at = lambda h: datetime(2026, 10, 5, h, 0, tzinfo=state.TZ)  # noqa: E731
    assert not jobs.needs_catch_up(engine, "09:00", at(8))  # vor 09:00: normal warten
    assert jobs.needs_catch_up(engine, "09:00", at(11))  # Neustart um 11 Uhr: nachholen
    assert not jobs.needs_catch_up(engine, "09:00", at(21))  # abends nicht mehr
    runner.mark_daily_done(engine)
    assert not jobs.needs_catch_up(engine, "09:00", datetime.now(state.TZ).replace(hour=11))
    assert "· Tipps " in runner.pin_text(engine) and runner.parse_pin_daily(runner.pin_text(engine))


def test_empty_top_explains_why():
    text = telegram_bot.format_top5({"top": []})
    assert "keine Tipps" in text and "Lieber kein Tipp" in text


def test_status_shows_agents_and_errors():
    from datetime import datetime, timedelta

    from fussball.app import state

    later = datetime.now(state.TZ) + timedelta(minutes=30)
    if later.date() != datetime.now(state.TZ).date():
        return
    kick = later.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None).isoformat()
    top = [{"match": "Italien – Türkei", "kickoff": kick, "prob": 0.8, "agent": "bestätigt"},
           {"match": "A – B", "kickoff": kick, "prob": 0.8, "agent": "vorsicht", "reason": "Torwart fehlt"},
           {"match": "C – D", "kickoff": kick, "prob": 0.8, "agent": "fehler", "reason": "APIError 529"}]
    text = telegram_bot.format_status({"top": top}, 1.2, 4.0, "2026-10-05", None)
    assert "Tagesprüfung heute: gelaufen" in text and "bestätigt: 1" in text and "gestrichen: 1" in text
    assert "Agenten-Fehler: 1" in text and "APIError 529" in text and "Torwart fehlt" in text
    assert "1.20 $ von 4.00 $" in text
    assert "Agenten-Fehler" in telegram_bot.why_no_tips({"top": [top[2]]})
