"""Logging mit Maskierung von Geheimnissen."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram-Bot-Token
    re.compile(r"(bot)\d{6,12}:[A-Za-z0-9_\-]{30,}"),
]


class SecretMaskingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for p in _TOKEN_PATTERNS:
            text = p.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = self.mask(msg)
        if masked != msg:
            record.msg = masked
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretMaskingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Laute Bibliotheken dämpfen; httpx loggt sonst URLs inkl. Bot-Token
    for noisy in ("httpx", "httpx2", "httpcore", "telegram", "apscheduler", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
