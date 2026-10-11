"""Gemeinsamer HTTP-Client: Rate-Limit pro Host, Retries, Cache in SQLite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from alpha_scout.db import Database

log = logging.getLogger(__name__)

# Mindestabstand zwischen Anfragen je Host (Sekunden)
HOST_INTERVALS = {
    "data.sec.gov": 0.15,  # SEC: max. 10 Anfragen/s
    "www.sec.gov": 0.15,
    "efts.sec.gov": 0.15,
    "api.gdeltproject.org": 5.5,  # GDELT: 1 Anfrage / 5 s
    "api.coingecko.com": 2.5,  # Free: ~30/min
}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} für {url}")
        self.status = status
        self.url = url
        self.body = body[:300]


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, httpx.TransportError)


class HttpClient:
    def __init__(self, db: Database | None, user_agent: str, timeout: float = 30.0, attempts: int = 4):
        self.db = db
        self.attempts = attempts
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self, host: str) -> None:
        interval = HOST_INTERVALS.get(host)
        if not interval:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlparse(url).netloc
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.attempts),
            wait=wait_exponential_jitter(initial=2, max=30),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                await self._throttle(host)
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code >= 400:
                    raise HttpError(resp.status_code, url, resp.text)
                return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _cache_key(method: str, url: str, params: Any, body: Any) -> str:
        raw = json.dumps([method, url, params, body], sort_keys=True, default=str)
        return "http:" + hashlib.sha256(raw.encode()).hexdigest()

    async def get_json(
        self, url: str, *, params: dict | None = None, ttl: float = 0, headers: dict | None = None
    ) -> Any:
        return await self._cached("GET", url, params=params, ttl=ttl, headers=headers, as_json=True)

    async def post_json(self, url: str, body: Any, *, ttl: float = 0, headers: dict | None = None) -> Any:
        return await self._cached("POST", url, body=body, ttl=ttl, headers=headers, as_json=True)

    async def get_text(
        self, url: str, *, params: dict | None = None, ttl: float = 0, headers: dict | None = None
    ) -> str:
        return await self._cached("GET", url, params=params, ttl=ttl, headers=headers, as_json=False)

    async def _cached(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        body: Any = None,
        ttl: float,
        headers: dict | None,
        as_json: bool,
    ) -> Any:
        key = self._cache_key(method, url, params, body)
        if ttl > 0 and self.db is not None:
            hit = await self.db.cache_get(key)
            if hit is not None:
                return hit
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        resp = await self._request(method, url, **kwargs)
        value = resp.json() if as_json else resp.text
        if ttl > 0 and self.db is not None:
            await self.db.cache_set(key, value, ttl)
        return value
