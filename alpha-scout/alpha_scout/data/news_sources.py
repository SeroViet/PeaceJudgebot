"""Nachrichtenquellen: GDELT 2.0 DOC API und RSS-Feeds (Notenbanken, SEC)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser

from alpha_scout.http import HttpClient, HttpError

log = logging.getLogger(__name__)

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: datetime | None
    query: str = ""

    @property
    def key(self) -> str:
        return "news:" + hashlib.sha1((self.url or self.title).encode()).hexdigest()


def _gdelt_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class Gdelt:
    def __init__(self, http: HttpClient):
        self.http = http

    async def search(self, query: str, timespan: str = "6h", max_records: int = 50) -> list[NewsItem]:
        params = {
            "query": f"{query} sourcelang:english",
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(max_records),
            "timespan": timespan,
            "sort": "datedesc",
        }
        try:
            data = await self.http.get_json(GDELT_URL, params=params, ttl=20 * 60)
        except (HttpError, ValueError) as e:  # GDELT liefert bei Fehlern Klartext statt JSON
            log.warning("GDELT-Abfrage fehlgeschlagen (%s): %s", query[:60], e)
            return []
        out = []
        for a in (data or {}).get("articles", []):
            title = (a.get("title") or "").strip()
            if not title:
                continue
            out.append(
                NewsItem(
                    title=title,
                    url=a.get("url", ""),
                    source=f"GDELT/{a.get('domain', '?')}",
                    published=_gdelt_date(a.get("seendate")),
                    query=query,
                )
            )
        return out


class RssReader:
    def __init__(self, http: HttpClient):
        self.http = http

    async def fetch(self, name: str, url: str) -> list[NewsItem]:
        try:
            text = await self.http.get_text(url, ttl=15 * 60)
        except HttpError as e:
            log.warning("RSS %s nicht erreichbar: %s", name, e)
            return []
        feed = await asyncio.to_thread(feedparser.parse, text)
        out = []
        for e in feed.entries[:30]:
            published = None
            if getattr(e, "published_parsed", None):
                published = datetime(*e.published_parsed[:6], tzinfo=UTC)
            out.append(
                NewsItem(
                    title=e.get("title", "").strip(), url=e.get("link", ""), source=name, published=published
                )
            )
        return [i for i in out if i.title]


def build_or_query(terms: list[str], max_len: int = 240) -> list[str]:
    """GDELT verträgt keine langen Abfragen -> Begriffe in mehrere OR-Gruppen teilen."""
    groups: list[str] = []
    current: list[str] = []
    for t in terms:
        term = f'"{t}"' if " " in t else t
        candidate = current + [term]
        if current and len(" OR ".join(candidate)) + 2 > max_len:
            groups.append(current)
            current = [term]
        else:
            current = candidate
    if current:
        groups.append(current)
    return [f"({' OR '.join(g)})" if len(g) > 1 else g[0] for g in groups]
