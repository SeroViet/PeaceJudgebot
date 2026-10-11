"""Formatierung (Telegram-HTML) und Versand mit Ruhezeiten, Dedupe und Empfehlungs-Logbuch."""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from alpha_scout.models import Alert, AlertType

log = logging.getLogger(__name__)

MAX_LEN = 4000
Sender = Callable[[str], Awaitable[None]]


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {esc(i)}" for i in items)


def format_alert(a: Alert) -> str:
    """Format: Was – Warum – Fakten – Risiken – Gegenargumente – Handlung – Quelle – Swissquote."""
    parts = [f"<b>{esc(a.type.value)}: {esc(a.title)}</b>"]
    if a.score is not None:
        parts[0] += f"  <i>Score {a.score:.0f}/100</i>"
    if a.why:
        parts.append("<b>Warum</b>\n" + esc(" ".join(a.why[:3])))
    if a.facts:
        parts.append("<b>Fakten</b>\n" + _bullets(a.facts))
    if a.risks:
        parts.append("<b>Risiken</b>\n" + _bullets(a.risks))
    if a.counterarguments:
        parts.append("<b>Gegenargumente</b>\n" + _bullets(a.counterarguments))
    if a.action:
        parts.append("<b>Handlung</b>\n" + esc(a.action))
    if a.sources:
        parts.append("<b>Quelle</b>\n" + esc(", ".join(a.sources[:8])))
    parts.append(f"<b>Swissquote</b> {a.swissquote.value} {esc(a.swissquote_note)}")
    parts.append("<i>Keine Anlageberatung – Entscheidung liegt bei dir.</i>")
    return "\n\n".join(parts)


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            while len(block) > limit:
                cut = block.rfind("\n", 0, limit)
                cut = cut if cut > 0 else limit
                chunks.append(block[:cut])
                block = block[cut:].lstrip("\n")
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def in_quiet_hours(now: datetime, window: tuple[int, int]) -> bool:
    start, end = window
    h = now.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


class Dispatcher:
    """Entscheidet, ob/wann eine Meldung rausgeht, und protokolliert sie."""

    def __init__(self, ctx, sender: Sender | None):
        self.ctx = ctx
        self.sender = sender

    async def send_text(self, text: str) -> None:
        if self.sender is None:
            log.info("Kein Telegram konfiguriert – Meldung:\n%s", text)
            return
        for chunk in split_message(text):
            await self.sender(chunk)

    async def dispatch(
        self, alert: Alert, price: float | None = None, currency: str = "", agent_scores: dict | None = None
    ) -> bool:
        from alpha_scout.orchestrator import DEDUPE_DAYS

        days = DEDUPE_DAYS.get(alert.type, self.ctx.cfg.scoring.dedupe_days)
        if await self.ctx.db.recently_alerted(alert.dedupe_key, days):
            log.info("Alert %s unterdrückt (Dedupe %d Tage)", alert.dedupe_key, days)
            return False
        text = format_alert(alert)
        quiet = in_quiet_hours(self.ctx.now(), self.ctx.settings.quiet_window())
        send_now = alert.urgent or not quiet
        if send_now:
            try:
                await self.send_text(text)
            except Exception as e:
                log.error("Telegram-Versand fehlgeschlagen: %s", type(e).__name__)
                send_now = False
        await self.ctx.db.add_alert(
            type_=alert.type.value,
            subject=alert.subject,
            dedupe_key=alert.dedupe_key,
            text=text,
            sent=send_now,
        )
        if price and alert.type != AlertType.WARNING:
            await self.ctx.db.execute(
                "INSERT INTO recommendations (ts, subject, type, score, price, currency, agent_scores) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.ctx.now().isoformat(),
                    alert.subject,
                    alert.type.name,
                    alert.score,
                    price,
                    currency,
                    json.dumps(agent_scores or {}),
                ),
            )
        return True

    async def flush_pending(self) -> int:
        """Nach den Ruhezeiten gesammelte Meldungen senden."""
        if in_quiet_hours(self.ctx.now(), self.ctx.settings.quiet_window()):
            return 0
        n = 0
        for row in await self.ctx.db.pending_alerts():
            try:
                await self.send_text(row["text"])
            except Exception as e:
                log.error("Telegram-Versand fehlgeschlagen: %s", type(e).__name__)
                break
            await self.ctx.db.mark_alert_sent(row["id"])
            n += 1
        return n
