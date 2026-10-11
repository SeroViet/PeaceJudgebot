"""SEC EDGAR: Ticker/CIK, Einreichungen, XBRL-Fakten, Form 4 (Insider), 13F (Superinvestoren)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from alpha_scout.http import HttpClient, HttpError

log = logging.getLogger(__name__)

SOURCE = "SEC EDGAR"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"

_SUFFIXES = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|LP|L P|HOLDINGS?|GROUP|SA|AG|NV|N V|SE|"
    r"CLASS [A-C]|CL [A-C]|COM|NEW|DEL|THE|ADR|SPONSORED|ADS|ORD|SHS)\b"
)


def normalize_name(name: str) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    n = _SUFFIXES.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


@dataclass
class InsiderTrade:
    issuer_ticker: str
    owner: str
    owner_cik: str
    title: str
    code: str
    shares: float
    price: float | None
    trade_date: str
    filing_date: str
    url: str

    @property
    def value(self) -> float | None:
        return self.shares * self.price if self.price else None


@dataclass
class Holding13F:
    issuer: str
    cusip: str
    value_usd: float
    shares: float


def parse_form4(xml_text: str, filing_date: str, url: str) -> list[InsiderTrade]:
    root = _strip_ns(ET.fromstring(xml_text))
    ticker = (_text(root, "issuer/issuerTradingSymbol") or "").upper()
    owner_el = root.find("reportingOwner")
    owner = _text(owner_el, "reportingOwnerId/rptOwnerName") or "unbekannt"
    owner_cik = _text(owner_el, "reportingOwnerId/rptOwnerCik") or ""
    rel = owner_el.find("reportingOwnerRelationship") if owner_el is not None else None
    title = _text(rel, "officerTitle") or ""
    if not title and rel is not None:
        if _text(rel, "isDirector") in ("1", "true"):
            title = "Director"
        elif _text(rel, "isTenPercentOwner") in ("1", "true"):
            title = "10%-Eigentümer"
    trades = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode") or ""
        shares = _float(_text(tx, "transactionAmounts/transactionShares/value")) or 0.0
        price = _float(_text(tx, "transactionAmounts/transactionPricePerShare/value"))
        trades.append(
            InsiderTrade(
                issuer_ticker=ticker,
                owner=owner,
                owner_cik=owner_cik,
                title=title,
                code=code,
                shares=shares,
                price=price if price else None,
                trade_date=_text(tx, "transactionDate/value") or filing_date,
                filing_date=filing_date,
                url=url,
            )
        )
    return trades


def parse_13f_infotable(xml_text: str) -> list[Holding13F]:
    """Infotable parsen; Zeilen gleicher CUSIP werden zusammengefasst. Put/Call-Optionen werden ignoriert."""
    root = _strip_ns(ET.fromstring(xml_text))
    agg: dict[str, Holding13F] = {}
    for it in root.iter("infoTable"):
        if _text(it, "putCall"):
            continue
        cusip = (_text(it, "cusip") or "").upper()
        if not cusip:
            continue
        value = _float(_text(it, "value")) or 0.0
        shares = _float(_text(it, "shrsOrPrnAmt/sshPrnamt")) or 0.0
        if cusip in agg:
            agg[cusip].value_usd += value
            agg[cusip].shares += shares
        else:
            agg[cusip] = Holding13F(
                issuer=_text(it, "nameOfIssuer") or "", cusip=cusip, value_usd=value, shares=shares
            )
    return list(agg.values())


class SecEdgar:
    def __init__(self, http: HttpClient):
        self.http = http
        self._tickers: dict[str, dict[str, Any]] | None = None
        self._names: dict[str, str] | None = None

    # ------------------------------------------------------------ Ticker / CIK
    async def _load_tickers(self) -> None:
        if self._tickers is not None:
            return
        raw = await self.http.get_json(TICKERS_URL, ttl=7 * 24 * 3600)
        self._tickers = {}
        self._names = {}
        for row in raw.values():
            t = str(row["ticker"]).upper()
            entry = {"cik": str(row["cik_str"]).zfill(10), "title": row["title"], "ticker": t}
            self._tickers[t] = entry
            self._names.setdefault(normalize_name(row["title"]), t)

    async def cik_for(self, ticker: str) -> str | None:
        if "." in ticker or "=" in ticker:  # Nicht-US-Börsen haben keine SEC-CIK
            return None
        await self._load_tickers()
        assert self._tickers is not None
        entry = self._tickers.get(ticker.upper().replace("-", "."))
        entry = entry or self._tickers.get(ticker.upper())
        return entry["cik"] if entry else None

    async def ticker_for_name(self, name: str) -> str | None:
        """Firmenname (z. B. aus 13F, oft abgekürzt) -> Ticker. Exakt, sonst eindeutiger Präfix-Treffer."""
        await self._load_tickers()
        assert self._names is not None
        norm = normalize_name(name)
        if not norm:
            return None
        exact = self._names.get(norm)
        if exact or len(norm) < 10:
            return exact
        hits = {t for n, t in self._names.items() if n.startswith(norm)}
        return hits.pop() if len(hits) == 1 else None

    # ------------------------------------------------------------ Einreichungen
    async def submissions(self, cik: str, ttl: float = 3 * 3600) -> dict[str, Any]:
        return await self.http.get_json(SUBMISSIONS_URL.format(cik=cik), ttl=ttl)

    async def recent_filings(
        self, cik: str, forms: set[str], since: date, ttl: float = 3 * 3600
    ) -> list[dict[str, str]]:
        sub = await self.submissions(cik, ttl=ttl)
        recent = sub.get("filings", {}).get("recent", {})
        out = []
        for i, form in enumerate(recent.get("form", [])):
            if form not in forms:
                continue
            fdate = recent["filingDate"][i]
            if fdate < since.isoformat():
                continue
            out.append(
                {
                    "form": form,
                    "accession": recent["accessionNumber"][i],
                    "filing_date": fdate,
                    "report_date": recent.get("reportDate", [""] * (i + 1))[i],
                    "primary_doc": recent["primaryDocument"][i],
                }
            )
        return out

    def archive_url(self, cik: str, accession: str, doc: str) -> str:
        return ARCHIVE_URL.format(cik=int(cik), acc=accession.replace("-", ""), doc=doc)

    # ------------------------------------------------------------ XBRL
    async def company_facts(self, cik: str) -> dict[str, Any] | None:
        try:
            return await self.http.get_json(FACTS_URL.format(cik=cik), ttl=24 * 3600)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    @staticmethod
    def latest_fact(facts: dict[str, Any], concept: str, unit: str = "USD") -> tuple[float, str, str] | None:
        """Letzter Wert eines us-gaap-Konzepts: (Wert, Stichtag, Formular)."""
        try:
            rows = facts["facts"]["us-gaap"][concept]["units"][unit]
        except KeyError:
            return None
        rows = [r for r in rows if r.get("form") in ("10-K", "10-Q", "20-F", "40-F") and "end" in r]
        if not rows:
            return None
        r = max(rows, key=lambda x: (x["end"], x.get("filed", "")))
        return float(r["val"]), r["end"], r.get("form", "")

    # ------------------------------------------------------------ Form 4
    async def insider_trades(self, ticker: str, days: int = 30) -> list[InsiderTrade] | None:
        """Alle Form-4-Transaktionen der letzten N Tage. None = keine US-Firma."""
        cik = await self.cik_for(ticker)
        if cik is None:
            return None
        since = date.today() - timedelta(days=days)
        filings = await self.recent_filings(cik, {"4", "4/A"}, since, ttl=3600)
        trades: list[InsiderTrade] = []
        for f in filings[:40]:
            doc = f["primary_doc"].split("/")[-1]  # xslF345X06/form4.xml -> form4.xml (Roh-XML)
            url = self.archive_url(cik, f["accession"], doc)
            try:
                xml_text = await self.http.get_text(url, ttl=30 * 24 * 3600)
                trades.extend(parse_form4(xml_text, f["filing_date"], url))
            except (HttpError, ET.ParseError) as e:
                log.warning("Form 4 nicht lesbar (%s): %s", url, e)
        return trades

    # ------------------------------------------------------------ 13F
    async def latest_13f(self, cik: str, n: int = 2) -> list[dict[str, Any]]:
        """Die letzten n 13F-HR-Meldungen inkl. Positionen (neueste zuerst)."""
        since = date.today() - timedelta(days=550)
        filings = [f for f in await self.recent_filings(cik, {"13F-HR", "13F-HR/A"}, since, ttl=12 * 3600)]
        # pro Stichtag nur die neueste Meldung (Amendments ersetzen)
        by_period: dict[str, dict[str, str]] = {}
        for f in filings:
            p = f["report_date"]
            if p not in by_period or f["filing_date"] > by_period[p]["filing_date"]:
                by_period[p] = f
        out = []
        for period in sorted(by_period, reverse=True)[:n]:
            f = by_period[period]
            holdings = await self._13f_holdings(cik, f["accession"])
            if holdings is None:
                continue
            out.append(
                {
                    "period": period,
                    "filing_date": f["filing_date"],
                    "accession": f["accession"],
                    "holdings": holdings,
                }
            )
        return out

    async def _13f_holdings(self, cik: str, accession: str) -> list[Holding13F] | None:
        idx = await self.http.get_json(
            INDEX_URL.format(cik=int(cik), acc=accession.replace("-", "")), ttl=90 * 24 * 3600
        )
        items = idx.get("directory", {}).get("item", [])
        xmls = [
            i["name"] for i in items if i["name"].lower().endswith(".xml") and i["name"] != "primary_doc.xml"
        ]
        for name in xmls:
            text = await self.http.get_text(self.archive_url(cik, accession, name), ttl=90 * 24 * 3600)
            if "infoTable" in text:
                try:
                    return parse_13f_infotable(text)
                except ET.ParseError as e:
                    log.warning("13F nicht lesbar %s: %s", accession, e)
        return None


def parse_iso(d: str) -> date:
    return datetime.fromisoformat(d[:10]).date()
