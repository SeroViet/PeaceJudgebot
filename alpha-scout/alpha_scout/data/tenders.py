"""Öffentliche Aufträge: USAspending (USA), TED (EU), SIMAP (Schweiz)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from alpha_scout.http import HttpClient, HttpError

log = logging.getLogger(__name__)

USASPENDING = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TED = "https://api.ted.europa.eu/v3/notices/search"
SIMAP = "https://www.simap.ch/api/publications/v2/project/project-search"


@dataclass
class Tender:
    source: str
    id: str
    title: str
    buyer: str
    value_usd: float | None
    winner: str | None
    published: str
    url: str
    kind: str  # award | tender


def _first_text(v: Any, prefer: tuple[str, ...] = ("eng", "deu", "fra")) -> str:
    """TED liefert mehrsprachige Felder als dict[lang -> str|list]."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return _first_text(v[0]) if v else ""
    if isinstance(v, dict):
        for lang in prefer:
            if lang in v:
                return _first_text(v[lang])
        return _first_text(next(iter(v.values()))) if v else ""
    return ""


class Tenders:
    def __init__(self, http: HttpClient):
        self.http = http

    async def usaspending(self, days: int, min_usd: float) -> list[Tender]:
        end = date.today()
        body = {
            "filters": {
                "time_period": [
                    {
                        "start_date": (end - timedelta(days=days)).isoformat(),
                        "end_date": end.isoformat(),
                        "date_type": "date_signed",
                    }
                ],
                "award_type_codes": ["A", "B", "C", "D"],
                "award_amounts": [{"lower_bound": min_usd}],
            },
            "fields": [
                "Award ID",
                "Recipient Name",
                "Award Amount",
                "Description",
                "Awarding Agency",
                "Start Date",
            ],
            "limit": 100,
            "page": 1,
            "sort": "Award Amount",
            "order": "desc",
        }
        try:
            data = await self.http.post_json(USASPENDING, body, ttl=12 * 3600)
        except HttpError as e:
            log.warning("USAspending: %s", e)
            return []
        out = []
        for r in data.get("results", []):
            out.append(
                Tender(
                    source="USAspending.gov",
                    id=str(r.get("Award ID")),
                    title=(r.get("Description") or "")[:300],
                    buyer=r.get("Awarding Agency") or "",
                    value_usd=r.get("Award Amount"),
                    winner=r.get("Recipient Name"),
                    published=r.get("Start Date") or "",
                    url=f"https://www.usaspending.gov/award/{r.get('generated_internal_id', '')}",
                    kind="award",
                )
            )
        return out

    async def ted(self, keywords: list[str], days: int, eur_usd: float = 1.1) -> list[Tender]:
        since = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
        terms = " OR ".join(f'FT~"{k}"' for k in keywords)
        body = {
            "query": f"({terms}) AND PD>={since}",
            "fields": [
                "publication-number",
                "notice-title",
                "buyer-name",
                "total-value",
                "publication-date",
                "notice-type",
            ],
            "limit": 100,
        }
        try:
            data = await self.http.post_json(TED, body, ttl=12 * 3600)
        except HttpError as e:
            log.warning("TED: %s", e)
            return []
        out = []
        for n in data.get("notices", []):
            pub = str(n.get("publication-number"))
            value = n.get("total-value")
            if isinstance(value, list):
                value = value[0] if value else None
            out.append(
                Tender(
                    source="TED (EU)",
                    id=pub,
                    title=_first_text(n.get("notice-title"))[:300],
                    buyer=_first_text(n.get("buyer-name")),
                    value_usd=float(value) * eur_usd if isinstance(value, int | float) else None,
                    winner=None,
                    published=str(n.get("publication-date", ""))[:10],
                    url=f"https://ted.europa.eu/en/notice/-/detail/{pub}",
                    kind="award" if str(n.get("notice-type", "")).startswith("can") else "tender",
                )
            )
        return out

    async def simap(self, days: int) -> list[Tender]:
        since = (date.today() - timedelta(days=days)).isoformat()
        try:
            data = await self.http.get_json(
                SIMAP, params={"lang": "de", "newestPublicationFrom": since}, ttl=12 * 3600
            )
        except HttpError as e:
            log.warning("SIMAP: %s", e)
            return []
        out = []
        for p in data.get("projects", []):
            title = _first_text(p.get("title"), prefer=("de", "fr", "it", "en"))
            out.append(
                Tender(
                    source="SIMAP (CH)",
                    id=str(p.get("projectNumber") or p.get("id")),
                    title=title[:300],
                    buyer=_first_text(p.get("procOfficeName"), prefer=("de", "fr", "it", "en")),
                    value_usd=None,
                    winner=None,
                    published=p.get("publicationDate", ""),
                    url=f"https://www.simap.ch/de/project-detail/{p.get('id')}",
                    kind=p.get("pubType", "tender"),
                )
            )
        return out
