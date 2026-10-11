"""Claude-Client: Structured Outputs, Prompt-Caching, Budget, Batch-API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from alpha_scout.llm.budget import (
    BudgetExceeded,
    BudgetGuard,
    actual_cost,
    worst_case_cost,
)
from alpha_scout.llm.prompts import COMMON_RULES, SCHEMAS, SYSTEMS

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Role = Literal["filter", "analysis"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMTask:
    """Eine LLM-Aufgabe; kann live oder im Batch laufen."""

    custom_id: str
    kind: str  # Schlüssel in SCHEMAS/SYSTEMS
    agent: str
    role: Role
    facts_json: str
    instruction: str
    max_tokens: int = 6000
    effort: str = "medium"

    @property
    def schema(self) -> type[BaseModel]:
        return SCHEMAS[self.kind]


class LLM:
    def __init__(
        self,
        budget: BudgetGuard,
        *,
        api_key: str | None,
        model_filter: str,
        model_analysis: str,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self.budget = budget
        self.model_filter = model_filter
        self.model_analysis = model_analysis
        self.enabled = bool(api_key) or client is not None
        self.client = client or (
            anthropic.AsyncAnthropic(api_key=api_key, max_retries=3) if api_key else None
        )

    def model_for(self, role: Role) -> str:
        return self.model_filter if role == "filter" else self.model_analysis

    # ------------------------------------------------------------ Request-Aufbau
    def _system(self, kind: str) -> list[dict[str, Any]]:
        # Gemeinsame Regeln + Aufgaben-Prompt sind stabil -> Cache-Breakpoint am Ende
        return [
            {"type": "text", "text": COMMON_RULES},
            {"type": "text", "text": SYSTEMS[kind], "cache_control": {"type": "ephemeral"}},
        ]

    @staticmethod
    def _user(task: LLMTask) -> str:
        return (
            f"<faktenblatt>\n{task.facts_json}\n</faktenblatt>\n\n<aufgabe>\n{task.instruction}\n</aufgabe>"
        )

    def params(self, task: LLMTask) -> dict[str, Any]:
        return {
            "model": self.model_for(task.role),
            "max_tokens": task.max_tokens,
            "system": self._system(task.kind),
            "messages": [{"role": "user", "content": self._user(task)}],
            "output_config": {
                "effort": task.effort,
                "format": {"type": "json_schema", "schema": transform_schema(task.schema)},
            },
        }

    def _input_chars(self, task: LLMTask) -> int:
        return len(COMMON_RULES) + len(SYSTEMS[task.kind]) + len(self._user(task)) + 2000  # + Schema

    # ------------------------------------------------------------ Antwort
    @staticmethod
    def _usage(usage: Any) -> dict[str, int]:
        return {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

    @staticmethod
    def parse_message(message: Any, schema: type[T], label: str) -> T | None:
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            log.warning("LLM-Ablehnung bei %s: %s", label, getattr(details, "category", None))
            return None
        if stop == "max_tokens":
            log.warning("LLM-Antwort abgeschnitten (max_tokens) bei %s", label)
            return None
        text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
        if not text:
            log.warning("LLM-Antwort ohne Text bei %s", label)
            return None
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            log.warning("LLM-Antwort ungültig bei %s: %s", label, e.errors()[:2])
            return None

    # ------------------------------------------------------------ Live-Aufruf
    async def run(self, task: LLMTask) -> BaseModel | None:
        """Führt eine Aufgabe sofort aus. None bei Budget-Ende, Fehler oder Ablehnung."""
        if not self.enabled or self.client is None:
            log.info("LLM deaktiviert (kein API-Key) – %s übersprungen", task.custom_id)
            return None
        params = self.params(task)
        model = params["model"]
        reserve = worst_case_cost(model, self._input_chars(task), task.max_tokens)
        try:
            await self.budget.reserve(reserve)
        except BudgetExceeded as e:
            log.warning("%s – %s übersprungen", e, task.custom_id)
            return None
        try:
            if model.startswith("claude-sonnet-5-5"):
                # Server-seitiger Fallback bei Ablehnungen (nur Live-API, nicht im Batch)
                message = await self.client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                message = await self.client.messages.create(**params)
        except anthropic.APIError as e:
            await self.budget.cancel(reserve)
            log.error("LLM-Fehler bei %s: %s", task.custom_id, type(e).__name__)
            return None
        served = getattr(message, "model", model) or model
        usage = self._usage(message.usage)
        cost = actual_cost(
            served,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read=usage["cache_read_tokens"],
            cache_write=usage["cache_write_tokens"],
        )
        await self.budget.settle(reserve, model=served, agent=task.agent, cost=cost, usage=usage)
        log.info("LLM %s (%s) kostete $%.4f", task.custom_id, served, cost)
        return self.parse_message(message, task.schema, task.custom_id)

    # ------------------------------------------------------------ Batch
    async def submit_batch(self, tasks: list[LLMTask], purpose: str) -> tuple[str | None, list[LLMTask]]:
        """Reicht Aufgaben als Batch ein (50 % günstiger). Gibt (Batch-ID, eingereichte Aufgaben) zurück.

        Reicht das Budget nicht für alle, werden Aufgaben von hinten gestrichen (Liste nach Priorität sortieren).
        """
        if not self.enabled or self.client is None or not tasks:
            return None, []
        tasks = list(tasks)
        while tasks:
            est = sum(
                worst_case_cost(self.model_for(t.role), self._input_chars(t), t.max_tokens, batch=True)
                for t in tasks
            )
            if est <= await self.budget.remaining():
                break
            dropped = tasks.pop()
            log.warning("Budget: Batch-Aufgabe %s gestrichen", dropped.custom_id)
        if not tasks:
            return None, []
        requests = [{"custom_id": t.custom_id, "params": self.params(t)} for t in tasks]
        try:
            batch = await self.client.messages.batches.create(requests=requests)
        except anthropic.APIError as e:
            log.error("Batch konnte nicht erstellt werden: %s", type(e).__name__)
            return None, []
        try:
            await self.budget.reserve_batch(est, ref=batch.id, model=self.model_analysis)
        except BudgetExceeded:
            log.warning("Budget-Reservierung für Batch %s fehlgeschlagen – storniere", batch.id)
            await self.client.messages.batches.cancel(batch.id)
            return None, []
        log.info("Batch %s (%s) mit %d Aufgaben eingereicht, max. $%.3f", batch.id, purpose, len(tasks), est)
        return batch.id, tasks

    async def batch_status(self, batch_id: str) -> str:
        assert self.client is not None
        batch = await self.client.messages.batches.retrieve(batch_id)
        return batch.processing_status

    async def collect_batch(self, batch_id: str, kinds: dict[str, str]) -> dict[str, BaseModel | None]:
        """Holt Ergebnisse eines beendeten Batches, rechnet Kosten ab, gibt custom_id -> Ergebnis."""
        assert self.client is not None
        out: dict[str, BaseModel | None] = {}
        results = await self.client.messages.batches.results(batch_id)
        async for item in results:
            cid = item.custom_id
            kind = kinds.get(cid)
            if item.result.type != "succeeded" or kind is None:
                log.warning("Batch %s: %s -> %s", batch_id, cid, item.result.type)
                out[cid] = None
                continue
            message = item.result.message
            usage = self._usage(message.usage)
            cost = actual_cost(
                message.model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read=usage["cache_read_tokens"],
                cache_write=usage["cache_write_tokens"],
                batch=True,
            )
            await self.budget.db.add_cost(
                model=message.model, agent=f"batch:{kind}", cost_usd=cost, day=self.budget.today(), **usage
            )
            out[cid] = self.parse_message(message, SCHEMAS[kind], cid)
        await self.budget.db.release_reservation(batch_id)
        return out


def facts_json_with_extra(facts_json: str, extra: dict[str, Any]) -> str:
    """Hängt zusätzliche Kontext-Felder an ein Faktenblatt-JSON an."""
    data = json.loads(facts_json)
    data.update(extra)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
