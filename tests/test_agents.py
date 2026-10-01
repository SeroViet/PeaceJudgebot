from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from fussball import service
from fussball.agents import runner, scout
from fussball.data import football_data as fd
from fussball.data.db import session_scope
from fussball.data.schema import AgentReport, Match, utcnow
from fussball.models.implied import fit_rates, implied_markets


def intel(assessment="bestätigt", home="A", away="B"):
    team = lambda n: {"team": n, "missing": [{"name": "Stürmer X", "position": "Sturm", "status": "fällt aus",  # noqa: E731
                                               "reason": "Muskelverletzung", "key_player": True}],
                      "yellow_card_risk": ["Y"], "lineup_confirmed": False, "expected_lineup": ["P1", "P2"],
                      "formation": "4-3-3", "fatigue": "3 Spiele in 8 Tagen", "motivation": "Abstiegskampf"}
    return scout.MatchIntel.model_validate({"home": team(home), "away": team(away), "tip_assessment": assessment,
                                           "tip_reason": "Grund", "summary": "Kurz", "sources": ["https://x"]})


class FakeClient:
    """Simuliert Recherche (Websuche) und strukturierte Ausgabe."""

    def __init__(self, verdicts):
        self.verdicts = verdicts  # match -> assessment
        self.calls = 0
        usage = SimpleNamespace(input_tokens=20000, output_tokens=1500, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)

        def create(**kw):
            self.calls += 1
            return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[
                SimpleNamespace(type="server_tool_use", name="web_search"),
                SimpleNamespace(type="text", text="Recherche " + kw["messages"][0]["content"][:40])])

        def parse(**kw):
            match = kw["messages"][0]["content"].split("\n")[0].removeprefix("Spiel: ")
            return SimpleNamespace(stop_reason="end_turn", usage=usage,
                                   parsed_output=intel(self.verdicts.get(match, "bestätigt")))

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create, parse=parse))


def test_scout_cost_and_schema():
    res = scout.scout_match(FakeClient({}), "A – B", "Sa 15:30", "D1", "Über 1.5 Tore", "- A: 7 Tage")
    assert res.intel.tip_assessment == "bestätigt" and res.searches == 1
    assert res.cost_usd == pytest.approx(2 * (0.02 * 4 + 0.0015 * 20) + 0.01)
    text = scout.format_intel("A – B", "Über 1.5 Tore", res.intel)
    assert "Stürmer X (fällt aus, Stamm)" in text and "Gelbsperre droht: Y" in text


def _plan_with_matches(engine, fixture_bytes, league, n=7):
    with session_scope(engine) as s:
        fd.import_season(s, league, "2425", fixture_bytes("D1_2425_sample.csv"))
        fd.import_season(s, league, "2627", fixture_bytes("D1_2627_sample.csv"))
        ids = [m.id for m in s.scalars(select(Match))]  # 8 echte Spiele
    # morgen 10:00 UTC: alle Spiele am selben Schweizer Kalendertag und innerhalb von 36 h
    kickoff = (utcnow() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    forecasts = []
    for i in range(n):
        lam, mu = fit_rates({"H": 0.62 - i * 0.01, "D": 0.22, "A": 0.16 + i * 0.01}, 2.5, 0.57)
        forecasts.append(service.MatchForecast(ids[i], "D1", "BL",
                                               kickoff + timedelta(minutes=30 * i), f"H{i}", f"A{i}", lam, mu,
                                               {}, {}, {}, None, None, 0.0, {}, implied=implied_markets(lam, mu)))
    cfg = {"day_combo": {"sizes": [5], "min_prob": 0.75, "max_prob": 0.88}}
    plan = service.DailyPlan(forecasts, [], [], cfg, [], [], service.day_combos(forecasts, (5,)))
    return plan


def test_apply_agents_replaces_struck_leg(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    first = plan.day_combos[0]["legs"]
    struck = first[0]["match"]
    results = service.apply_agents(engine, plan, client=FakeClient({struck: "streichen"}))
    assert any(r["removed"] for r in results)
    legs = plan.day_combos[0]["legs"]
    assert struck not in {l["match"] for l in legs} and len(legs) == 5  # Ersatz ist nachgerückt
    assert all("agent" in l for l in legs)


def test_budget_stops_agents(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    monkeypatch.setenv("AGENT_DAILY_BUDGET_USD", "0.5")
    monkeypatch.setenv("AGENT_MAX_COST_PER_MATCH", "0.25")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    legs = plan.day_combos[0]["legs"]
    out = runner.analyze_legs(engine, legs, client=FakeClient({}))
    assert 1 <= len(out) < len(legs)  # nächstes Spiel (bis 0.25 $) passt nicht mehr ins Limit
    with session_scope(engine) as s:
        assert s.execute(select(AgentReport)).first() is not None


def test_no_client_no_agents(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert scout.make_client() is None
    assert runner.analyze_legs(engine, [{"match_id": 1}]) == []


def test_due_lineup_checks_window():
    from fussball.app.jobs import due_lineup_checks

    plan = {"day_combos": [{"legs": [{"match_id": 5, "kickoff": "2026-10-10T13:30:00"}]}]}
    assert due_lineup_checks(plan, datetime(2026, 10, 10, 12, 15), 75, set())
    assert not due_lineup_checks(plan, datetime(2026, 10, 10, 10, 0), 75, set())


def test_apply_agents_replaces_caution_leg_if_possible(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    risky = plan.day_combos[0]["legs"][0]["match"]
    service.apply_agents(engine, plan, client=FakeClient({risky: "vorsicht"}))
    legs = plan.day_combos[0]["legs"]
    assert risky not in {l["match"] for l in legs} and len(legs) == 5


def test_apply_agents_keeps_caution_leg_without_replacement(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga, n=5)  # kein Ersatz vorhanden
    risky = plan.day_combos[0]["legs"][0]["match"]
    service.apply_agents(engine, plan, client=FakeClient({risky: "vorsicht"}))
    legs = plan.day_combos[0]["legs"]
    assert risky in {l["match"] for l in legs}
    assert next(l for l in legs if l["match"] == risky)["agent"]["assessment"] == "vorsicht"


def test_scout_can_switch_to_better_supported_tip(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    leg = plan.day_combos[0]["legs"][0]
    other = next(a["label"] for a in leg["alternatives"] if a["label"] != leg["label"])
    client = FakeClient({})
    orig_parse = client.beta.messages.parse

    def parse(**kw):
        resp = orig_parse(**kw)
        if kw["messages"][0]["content"].startswith(f"Spiel: {leg['match']}\n"):
            resp.parsed_output.best_tip = other
        return resp

    client.beta.messages.parse = parse
    service.apply_agents(engine, plan, client=client)
    new = next(l for l in plan.day_combos[0]["legs"] if l["match_id"] == leg["match_id"])
    assert new["label"] == other and new["switched_from"]
    c = plan.day_combos[0]
    assert c["fair_odds"] == pytest.approx(1 / c["prob"])


def test_cached_only_never_calls_claude(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    client = FakeClient({})
    assert service.apply_agents(engine, plan, client=client, cached_only=True) == []
    assert client.calls == 0
    service.apply_agents(engine, plan, client=client)  # echter Lauf speichert Berichte
    paid = client.calls
    again = service.apply_agents(engine, plan, client=client, cached_only=True)
    assert client.calls == paid and again and all(r["cached"] for r in again)


def test_scout_checks_krass_combo_and_replaces_struck(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga, n=7)
    plan.krass_combos = service.build_extra_combos(plan, "krass")
    assert plan.krass_combos and plan.krass_combos[0]["krass"]
    struck = plan.krass_combos[0]["legs"][0]["match"]
    res = service.apply_agents_risky(engine, plan, client=FakeClient({struck: "streichen"}), kind="krass")
    assert len(res) == 5
    legs = plan.krass_combos[0]["legs"]
    assert struck not in {l["match"] for l in legs} and len(legs) == 5 and plan.krass_combos[0]["krass"]


def test_costs_include_screenshots_and_stop_below_limit(engine, fixture_bytes, bundesliga, monkeypatch):
    monkeypatch.setattr(runner, "fatigue_context", lambda e, mid: "ctx")
    monkeypatch.setenv("AGENT_DAILY_BUDGET_USD", "1.0")
    runner.add_other_cost(engine, 0.04)
    runner.add_other_cost(engine, 0.03)
    assert runner.other_costs_today(engine)["n"] == 2
    plan = _plan_with_matches(engine, fixture_bytes, bundesliga)
    runner.analyze_legs(engine, plan.day_combos[0]["legs"], client=FakeClient({}))
    assert runner.spent_today(engine) <= 1.0  # Limit wird nie überschritten
    text = runner.cost_summary(engine)
    assert "Screenshots: 2" in text and "von 1.00 $" in text
