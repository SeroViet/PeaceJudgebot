"""Client für API-Football v3 (api-sports.io).

- Authentifizierung über Header `x-apisports-key`. Ist API_FOOTBALL_KEY nicht
  gesetzt, wird der Header weggelassen (in der Claude-Cloud-Umgebung injiziert
  ihn der Proxy).
- Rate-Limit pro Minute wird lokal eingehalten; bei 429/5xx Retry mit Backoff.
- Tageskontingent: Bleiben weniger als `daily_reserve` Anfragen übrig, bricht der
  Client ab, damit das T−60min-Update nicht leer ausgeht.
- Free-Plan: nur Saisons 2022–2024 (Stand 29.09.2026, per Test verifiziert).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from fussball.config import get_settings

log = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"


class ApiFootballError(RuntimeError):
    pass


class QuotaExhausted(ApiFootballError):
    pass


class ApiFootballClient:
    def __init__(
        self,
        api_key: str | None = None,
        per_minute: int | None = None,
        daily_reserve: int | None = None,
        session: requests.Session | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.api_football_key
        self.per_minute = per_minute or settings.api_football_per_minute
        self.daily_reserve = settings.api_football_daily_reserve if daily_reserve is None else daily_reserve
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self.remaining_today: int | None = None
        self.requests_made = 0

    def _throttle(self) -> None:
        interval = 60.0 / self.per_minute
        if self._last_call is not None:
            wait = interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def get(self, endpoint: str, **params: Any) -> dict:
        if self.remaining_today is not None and self.remaining_today <= self.daily_reserve:
            raise QuotaExhausted(
                f"Nur noch {self.remaining_today} Anfragen heute (Reserve {self.daily_reserve})"
            )
        headers = {"x-apisports-key": self.api_key} if self.api_key else {}
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            self.requests_made += 1
            remaining = resp.headers.get("x-ratelimit-requests-remaining")
            if remaining is not None:
                self.remaining_today = int(remaining)
            if resp.status_code == 429 or resp.status_code >= 500:
                backoff = 2 ** (attempt + 1)
                log.warning("API-Football %s → HTTP %s, Retry in %ss", endpoint, resp.status_code, backoff)
                self._sleep(backoff)
                continue
            resp.raise_for_status()
            payload = resp.json()
            errors = payload.get("errors")
            if errors:
                if isinstance(errors, dict) and "rateLimit" in errors:
                    self._sleep(2 ** (attempt + 1))
                    continue
                raise ApiFootballError(f"{endpoint} {params}: {errors}")
            log.debug("API-Football %s %s: %s Treffer, %s übrig heute",
                      endpoint, params, payload.get("results"), self.remaining_today)
            return payload
        raise ApiFootballError(f"{endpoint} {params}: nach {self.max_retries} Retries fehlgeschlagen")

    def get_all(self, endpoint: str, **params: Any) -> list[dict]:
        """Alle Seiten eines paginierten Endpunkts (z. B. /players)."""
        page, items = 1, []
        while True:
            payload = self.get(endpoint, **params, **({"page": page} if page > 1 else {}))
            items.extend(payload.get("response", []))
            paging = payload.get("paging") or {}
            if page >= int(paging.get("total", 1)):
                return items
            page += 1

    def status(self) -> dict:
        """Konto- und Kontingentinfo. Zählt nicht gegen das Tageslimit."""
        return self.get("status")["response"]
