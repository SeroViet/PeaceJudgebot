from types import SimpleNamespace

import pytest

from alpha_scout.llm.budget import BudgetExceeded, BudgetGuard, actual_cost, worst_case_cost
from alpha_scout.llm.client import LLM, LLMTask
from alpha_scout.llm.prompts import MoatAssessment


def test_costs():
    assert actual_cost("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0) == pytest.approx(2.0)
    assert actual_cost("claude-haiku-5-5", input_tokens=0, output_tokens=1_000_000) == pytest.approx(0.5)
    assert actual_cost(
        "claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0, batch=True
    ) == pytest.approx(1.0)
    assert actual_cost("unbekannt", input_tokens=1_000_000, output_tokens=0) == pytest.approx(10.0)
    assert worst_case_cost("claude-sonnet-5-5", 3000, 1000) == pytest.approx(0.002 + 0.01)


async def test_budget_hard_limit(db):
    b = BudgetGuard(db, 0.05)
    await b.reserve(0.03)
    with pytest.raises(BudgetExceeded):
        await b.reserve(0.03)
    await b.settle(0.03, model="m", agent="a", cost=0.01, usage={})
    assert await b.spent_today() == pytest.approx(0.01)
    await b.reserve(0.03)  # jetzt wieder Platz
    await b.cancel(0.03)
    await b.reserve_batch(0.02, ref="batch_1", model="m")
    assert await b.spent_today() == pytest.approx(0.03)
    await db.release_reservation("batch_1")
    assert await b.spent_today() == pytest.approx(0.01)


def _message(text, stop="end_turn", model="claude-sonnet-5-5"):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        model=model,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


class FakeAnthropic:
    def __init__(self, message):
        self.message = message
        self.kwargs = None
        outer = self

        class Msgs:
            async def create(self, **kw):
                outer.kwargs = kw
                return outer.message

        self.messages = Msgs()
        self.beta = SimpleNamespace(messages=Msgs())


def _task(role="analysis"):
    return LLMTask(
        custom_id="moat-X",
        kind="moat",
        agent="quality",
        role=role,
        facts_json='{"facts": []}',
        instruction="x",
        max_tokens=1000,
    )


VALID = '{"moat": "stark", "moat_types": ["marke"], "evidence": [], "concerns": []}'


async def test_llm_run_parses_and_bills(db):
    fake = FakeAnthropic(_message(VALID))
    llm = LLM(
        BudgetGuard(db, 3.0),
        api_key=None,
        model_filter="claude-haiku-5-5",
        model_analysis="claude-sonnet-5-5",
        client=fake,
    )
    out = await llm.run(_task())
    assert isinstance(out, MoatAssessment) and out.moat == "stark"
    # Sonnet live -> Fallback-Beta + gecachter System-Prompt + Structured Output
    assert fake.kwargs["fallbacks"] == "default" and fake.kwargs["betas"]
    assert fake.kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert fake.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert await llm.budget.spent_today() == pytest.approx((1000 * 2 + 500 * 10) / 1e6)


async def test_llm_haiku_no_fallback(db):
    fake = FakeAnthropic(_message(VALID, model="claude-haiku-5-5"))
    llm = LLM(
        BudgetGuard(db, 3.0),
        api_key=None,
        model_filter="claude-haiku-5-5",
        model_analysis="claude-sonnet-5-5",
        client=fake,
    )
    await llm.run(_task("filter"))
    assert "fallbacks" not in fake.kwargs and fake.kwargs["model"] == "claude-haiku-5-5"


async def test_llm_refusal_and_invalid_json_return_none(db):
    for msg in (_message(VALID, stop="refusal"), _message("{kaputt"), _message(VALID, stop="max_tokens")):
        llm = LLM(
            BudgetGuard(db, 3.0),
            api_key=None,
            model_filter="h",
            model_analysis="claude-sonnet-5-5",
            client=FakeAnthropic(msg),
        )
        assert await llm.run(_task()) is None


async def test_llm_skips_when_budget_exhausted(db):
    fake = FakeAnthropic(_message(VALID))
    llm = LLM(
        BudgetGuard(db, 0.0001),
        api_key=None,
        model_filter="h",
        model_analysis="claude-sonnet-5-5",
        client=fake,
    )
    assert await llm.run(_task()) is None
    assert fake.kwargs is None  # gar nicht erst aufgerufen


async def test_llm_disabled_without_key(db):
    llm = LLM(BudgetGuard(db, 3.0), api_key=None, model_filter="h", model_analysis="s")
    assert not llm.enabled
    assert await llm.run(_task()) is None
