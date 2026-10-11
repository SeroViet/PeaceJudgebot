from datetime import date, datetime

from alpha_scout.alerts import format_alert, in_quiet_hours, split_message
from alpha_scout.config import Position, ScoringConfig
from alpha_scout.llm.prompts import Claim, ThesisCheck
from alpha_scout.models import AgentResult, Alert, AlertType, AssetKind, SwissquoteStatus
from alpha_scout.orchestrator import combine_scores, decide
from alpha_scout.pipeline import Analysis

from .conftest import sheet_from

SCORING = ScoringConfig(
    weights={
        "quality": 30,
        "valuation": 25,
        "fundamental": 15,
        "superinvestor": 10,
        "insider": 10,
        "news": 5,
        "project": 5,
    }
)
TODAY = date(2026, 10, 11)


def _analysis(values, score=85.0, vetoes=None, kind=AssetKind.STOCK, results=None):
    a = Analysis(subject="TEST", kind=kind, sheet=sheet_from({"price.last": 100.0, **values}))
    a.score = score
    a.vetoes = vetoes or []
    a.results = results or {}
    return a


def test_combine_scores_weights_and_neutral():
    results = {
        "quality": AgentResult(agent="quality", subject="T", score=100),
        "valuation": AgentResult(agent="valuation", subject="T", score=80),
    }
    score, vetoes = combine_scores(results, SCORING, {}, AssetKind.STOCK)
    # 30*100 + 25*80 + 45*50 (neutral) = 7250 / 100
    assert score == 72.5 and vetoes == []
    score2, _ = combine_scores(results, SCORING, {"quality": 1.5}, AssetKind.STOCK)
    assert score2 > score


def test_combine_scores_veto_zeroes():
    results = {
        "quality": AgentResult(agent="quality", subject="T", score=100),
        "risk": AgentResult(agent="risk", subject="T", veto=True, ko_reason="Bilanz"),
    }
    score, vetoes = combine_scores(results, SCORING, {}, AssetKind.STOCK)
    assert score == 0.0 and vetoes == ["risk: Bilanz"]


def test_new_chance_only_when_not_held_and_not_stopped():
    a = _analysis({"quality.passed": True})
    assert [d.type for d in decide(a, SCORING, TODAY, None)] == [AlertType.NEW_CHANCE]
    stopped = _analysis({"quality.passed": True, "port.buy_stop": "bis Q3"})
    assert decide(stopped, SCORING, TODAY, None) == []
    low = _analysis({"quality.passed": True}, score=79)
    assert decide(low, SCORING, TODAY, None) == []


def test_buy_more_after_drop_for_watchlist():
    a = _analysis({"quality.passed": True, "price.drawdown_52w": -25.0, "port.watch": True}, score=70)
    assert [d.type for d in decide(a, SCORING, TODAY, None)] == [AlertType.BUY_MORE]
    not_watched = _analysis({"quality.passed": True, "price.drawdown_52w": -25.0}, score=70)
    assert decide(not_watched, SCORING, TODAY, None) == []
    crypto = _analysis({"price.drawdown_52w": -25.0, "port.watch": True}, score=70, kind=AssetKind.CRYPTO)
    assert decide(crypto, SCORING, TODAY, None) == []  # Krypto erst ab 40 %
    crypto_deep = _analysis(
        {"price.drawdown_52w": -45.0, "port.watch": True}, score=70, kind=AssetKind.CRYPTO
    )
    assert [d.type for d in decide(crypto_deep, SCORING, TODAY, None)] == [AlertType.BUY_MORE]
    ath_only = _analysis({"crypto.drawdown_ath": -60.0, "port.watch": True}, score=70, kind=AssetKind.CRYPTO)
    assert decide(ath_only, SCORING, TODAY, None) == []  # Allzeithoch allein löst nichts aus


def _pos(days_ago):
    from datetime import timedelta

    return Position(
        ticker="TEST", shares=1, buy_price=100.0, currency="", buy_date=TODAY - timedelta(days=days_ago)
    )


def test_sell_check_respects_six_months():
    a = _analysis({"val.in_sell_zone": True})
    assert decide(a, SCORING, TODAY, _pos(100)) == []
    assert [d.type for d in decide(a, SCORING, TODAY, _pos(200))] == [AlertType.SELL_CHECK]


def test_warning_when_thesis_endangered():
    a = _analysis({})
    thesis = ThesisCheck(status="gefaehrdet", reasons=[Claim(text="x", fact_ids=["price.last"])])
    ds = decide(a, SCORING, TODAY, _pos(30), thesis)
    assert [d.type for d in ds] == [AlertType.WARNING] and ds[0].urgent
    broken = ThesisCheck(status="zerbrochen", reasons=[])
    assert [d.type for d in decide(a, SCORING, TODAY, _pos(300), broken)] == [AlertType.SELL_CHECK]


def test_allocation_veto_is_not_a_warning_for_holdings():
    risk = AgentResult(
        agent="risk",
        subject="TEST",
        veto=True,
        ko_reason="Position > 5 %",
        data={"hard": [], "allocation": ["Position > 5 %"]},
    )
    a = _analysis({}, score=0, vetoes=["risk: Position > 5 %"], results={"risk": risk})
    assert decide(a, SCORING, TODAY, _pos(30)) == []


def test_format_alert_contains_all_sections_and_escapes():
    alert = Alert(
        type=AlertType.NEW_CHANCE,
        subject="A&B",
        title="A&B <Corp>",
        why=["Satz 1.", "Satz 2.", "Satz 3."],
        facts=["Marge 30 %"],
        risks=["Zyklisch"],
        counterarguments=["Teuer"],
        action="3 Tranchen",
        sources=["SEC EDGAR"],
        swissquote=SwissquoteStatus.CONFIRMED,
        score=88,
    )
    text = format_alert(alert)
    for part in (
        "Warum",
        "Fakten",
        "Risiken",
        "Gegenargumente",
        "Handlung",
        "Quelle",
        "Swissquote</b> ✅",
        "A&amp;B &lt;Corp&gt;",
    ):
        assert part in text


def test_split_message_and_quiet_hours():
    text = "\n\n".join(["x" * 1500] * 5)
    chunks = split_message(text, 4000)
    assert all(len(c) <= 4000 for c in chunks) and len(chunks) >= 2
    assert in_quiet_hours(datetime(2026, 1, 1, 23), (22, 7))
    assert in_quiet_hours(datetime(2026, 1, 1, 3), (22, 7))
    assert not in_quiet_hours(datetime(2026, 1, 1, 12), (22, 7))
