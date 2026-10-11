# ALPHA SCOUT – Phase 0: Plan

Stand: 2026-10-11 · Status: **wartet auf OK**

> Hinweis: Alpha Scout ist ein Recherche-Werkzeug, keine Anlageberatung. Jede
> Meldung nennt Quellen und Unsicherheiten; die Kaufentscheidung bleibt bei dir.

---

## 1. Grundprinzip: Zahlen rechnet Python, Urteile schreibt das LLM

Damit nie etwas erfunden wird, trennt das System strikt:

| Schicht | Wer | Beispiele |
|---|---|---|
| **Daten holen** | Python-Adapter (yfinance, EDGAR, …) | Kurse, Bilanzen, 13F, Form 4, News |
| **Rechnen & Regeln** | Deterministischer Python-Code | Wachstum 3J, NetDebt/EBITDA, Verwässerung, Qualitätsfilter-Punkte 1/2/4/5/6, Allokations-Check, 6-Monats-Sperre |
| **Urteilen** | Claude (nur mit übergebenen Fakten) | Burggraben (Punkt 3), News-Relevanz, These, Gegenargumente, Text der Meldung |

- Jeder LLM-Aufruf bekommt ein **Fakten-Paket** (JSON mit Quelle + Abrufdatum pro Wert)
  und antwortet über **Structured Outputs** (Pydantic-Schema). Jede Aussage muss eine
  `fact_id` aus dem Paket zitieren – Aussagen ohne Beleg werden vom Validator verworfen.
- Fehlen Daten, sagt die Meldung „unbekannt“ statt zu schätzen.
- Kein Websearch-Tool im API-Aufruf.

---

## 2. Architektur

```
                ┌──────────── APScheduler ────────────┐
                │ intraday · nightly(batch) · monthly │
                └───────────────┬─────────────────────┘
                                ▼
 ┌──────────── Data Layer (async, Cache in SQLite, Retries) ────────────┐
 │ prices/fundamentals │ sec_edgar │ gdelt/rss │ coingecko │ defillama │
 │ ted/simap/usaspending │ swissquote_universe (kuratierte Liste)       │
 └─────────────────────────────────┬────────────────────────────────────┘
                                   ▼
  Signal-Agenten          News · Superinvestor · Insider · ProjectScout · Crypto · ETF
  Analyse-Agenten         Fundamental → Quality → Valuation
  Kontroll-Agenten        Risk (Veto) · DevilsAdvocate (K.-o.)
                                   ▼
                     Orchestrator (Score 0–100, Alert-Regeln, Dedupe)
                                   ▼
          Telegram (Alerts + Befehle)   ·   PortfolioKeeper   ·   PerformanceTracker
                                                                  └─► Agenten-Gewichte
```

Jeder Agent ist eine Klasse mit gleicher Schnittstelle:

```python
class Agent(Protocol):
    name: str
    async def run(self, ctx: RunContext, subject: Subject) -> AgentResult: ...
# AgentResult: score (0–100) | veto | ko_reason, findings[], facts[], sources[], cost_usd
```

### Ordnerstruktur

```
alpha_scout/
  config.py              # Pydantic-Settings aus .env
  models.py              # Pydantic-Modelle (Subject, Fact, AgentResult, Alert, Holding …)
  db.py                  # SQLite (aiosqlite): cache, signals, recommendations, costs, positions
  llm/
    client.py            # AsyncAnthropic, Prompt-Caching, Structured Outputs
    budget.py            # hartes Tagesbudget (Vorab-Schätzung + Ist-Abrechnung)
    batch.py             # Batch-API für Nachtläufe
  data/                  # ein Adapter pro Quelle, alle mit Cache + Retry
  agents/                # 14 Agenten, je eine Datei
  telegram/              # Bot, Befehle, Formatierung (Modul aus Bybit-Bot übernommen)
  scheduler.py
  main.py
config/
  watchlist.yaml  holdings.yaml  swissquote_universe.yaml
  superinvestors.yaml  scoring.yaml
tests/                   # pro Agent; HTTP über aufgezeichnete Fixtures, keine Live-Calls
Dockerfile  docker-compose.yml  pyproject.toml  .env.example
```

**Stack:** Python 3.12, asyncio, Pydantic v2, aiosqlite, APScheduler, httpx,
python-telegram-bot 21.x, anthropic SDK, yfinance, pytest + pytest-asyncio + respx, ruff.

---

## 3. Datenquellen (alle gratis)

| Quelle | Wofür | Grenzen / Gegenmassnahme |
|---|---|---|
| **yfinance** | Kurse weltweit (inkl. SIX `.SW`, Xetra `.DE`), Fundamentaldaten, Benchmark | Inoffiziell, kann brechen → Cache, Fallback auf EDGAR-XBRL für US-Firmen, Datenlücken explizit als „unbekannt“ |
| **SEC EDGAR** (`data.sec.gov`, XBRL companyfacts, submissions) | US-Fundamentals (verlässlicher als yfinance), 13F, Form 4, 8-K | Max. 10 Req/s, User-Agent Pflicht → Rate-Limiter |
| **SEC 13F** | SuperinvestorAgent | **Bis 45 Tage Verzug** nach Quartalsende, nur US-Long-Positionen → Meldung zeigt Stichtag |
| **SEC Form 4** | InsiderAgent | Nur US-Firmen. Für Europa/CH gibt es keine gleichwertige Gratis-Quelle → v1 nur US, ADRs ausgenommen |
| **SEC Litigation Releases / Enforcement** | Ausschlusskriterium „SEC-Untersuchung“ | Nur öffentlich bekannte Fälle; zusätzlich News-Stichworte (fraud, probe, subpoena) |
| **GDELT 2.0 DOC API** | Weltweite News | Viel Rauschen → Haiku-Filter |
| **RSS** Fed, EZB, SNB, BoJ, SEC Press | Notenbanken | – |
| **CoinGecko** (Free API) | Krypto-Preise, Marktkap., Historie | ~30 Calls/min → Cache |
| **DefiLlama** | Gebühren-/Protokoll-Einnahmen, TVL, Unlocks (soweit frei) | – |
| **TED API (EU)**, **SIMAP (CH)**, **USAspending.gov** | ProjectScout: Ausschreibungen/Zuschläge | SAM.gov bräuchte Gratis-Key → optional |
| **ETF-Stammdaten** | TER, Grösse, Domizil | Keine saubere Gratis-API → kuratierte `etf_universe.yaml` + yfinance für Kurs/Volumen |

### Swissquote-Handelbarkeit
Swissquote hat **keine öffentliche API** für das handelbare Universum. Lösung:
1. Regel: Primärbörse ∈ {SIX, NYSE, Nasdaq, Xetra, LSE, Euronext, Borsa Italiana, BME,
   Nasdaq Nordic, Wiener Börse, TSE, HKEX, SGX, ASX, TSX} → `⚠️ wahrscheinlich`.
2. Kuratierte `config/swissquote_universe.yaml` (von dir bestätigt) → `✅`.
3. Sonst: ADR- oder passender ETF-Vorschlag.

---

## 4. Agenten – konkrete Logik

| # | Agent | Kern | LLM |
|---|---|---|---|
| 1 | NewsAgent | GDELT + RSS → Dedupe → Haiku klassifiziert Relevanz (Ticker, Makro, Bestand) → nur Score ≥ Schwelle weiter | Haiku |
| 2 | SuperinvestorAgent | 13F-Diffs Quartal/Quartal für Liste in `superinvestors.yaml` (CIKs); „gross“ = neue Position ≥ 3 % des Portfolios oder +50 %; Cluster = ≥ 3 Investoren gleiche Firma | – (Text: Sonnet) |
| 3 | InsiderAgent | Form 4, nur Code „P“ (Open-Market-Kauf), ≥ 2 verschiedene Insider in 30 Tagen → Signal; Verkäufe nur informativ | – |
| 4 | FundamentalAgent | Umsatz-CAGR 3J, Marge, FCF, NetDebt/EBITDA, Aktienzahl-Δ, Auftragsbestand (aus 10-Q/10-K-Text) | Sonnet nur für Textauszüge |
| 5 | QualityAgent | 6 Kriterien; 1,2,4,5,6 deterministisch, 3 (Burggraben) per Sonnet mit Begründung; ≥ 4/6 Pflicht; Ausschlüsse hart | Sonnet |
| 6 | ValuationAgent | Reverse-DCF + 3 Szenarien (Bär/Basis/Bulle) für 3 und 10 Jahre; Kaufzone = Basis-Wert mit ≥ 20 % Sicherheitsmarge, Verkaufszone = > Bullen-Wert | Sonnet nur Annahmen-Begründung |
| 7 | ProjectScout | Grosse Zuschläge/Ausschreibungen (Strom, Rechenzentren, Pipelines, Halbleiter, Rüstung) → Mapping Auftragnehmer → börsennotierte Firma | Haiku Filter, Sonnet Mapping |
| 8 | ETFAgent | Ranking nach TER, Fondsvolumen, Domizil CH/IE, Replikation, Tracking Difference | – |
| 9 | CryptoAgent | Nur Allowlist-Logik (≥ 3 J Historie, reale Gebühren-Einnahmen via DefiLlama); Events: Regulierung, ETF-Flows, Hacks, Unlocks | Haiku |
| 10 | RiskAgent | Veto bei: Allokation verletzt (60/30/10, 5 %/Aktie), Sektor-/Länder-Klumpen, Bilanzrisiko, Geopolitik-Flag | Sonnet nur Geopolitik |
| 11 | DevilsAdvocate | Eigener Sonnet-Call, Auftrag: stärkste Gegenargumente + K.-o.-Prüfung | Sonnet |
| 12 | Orchestrator | Gewichteter Score, Alert-Regeln, Dedupe (gleiche Meldung max. 1× / 14 Tage) | – |
| 13 | PortfolioKeeper | `holdings.yaml` + `/buy` `/sell`; prüft täglich These, ≥ 20 %-Rückgang, Zielzone, 6-Monats-Sperre | Sonnet bei Thesen-Check |
| 14 | PerformanceTracker | Jede Empfehlung vs. MSCI World (IWDA/URTH) nach 3/6/12/36 M; Agenten-Gewicht = f(Trefferquote), Grenzen 0.5–1.5 | – |

**Score (Startgewichte, in `scoring.yaml`, später vom PerformanceTracker angepasst):**
Quality 30 · Valuation 25 · Fundamental 15 · Superinvestor 10 · Insider 10 · News/Projekt 10.
Risk-Veto oder DevilsAdvocate-K.-o. → Score = 0, keine Empfehlung.

**Krypto-Allowlist v1:** BTC, ETH, SOL. Weitere Coins nur, wenn alle Kriterien maschinell
erfüllt sind – und dann erst als „beobachten“, nie automatisch als Kauf.

---

## 5. Telegram

- Nur Meldungen der 5 Typen (💎 📈 ⚠️ 💰 🐋), festes Format:
  Was – Warum (3 Sätze) – Fakten – Risiken – Gegenargumente – Kaufplan/Handlung – Quelle – Swissquote ✅/⚠️
- Ruhezeiten (z. B. 22–07 Uhr) → Meldungen werden gesammelt, ausser ⚠️.
- Monatsbericht am 1. (Depot, Performance vs. MSCI World, LLM-Kosten).
- Befehle: `/check TICKER` `/top` `/watchlist` `/holdings` `/buy` `/sell` `/costs` `/status`.
- Bot antwortet **nur** auf `TELEGRAM_CHAT_ID` (alle anderen Chats ignoriert).

---

## 6. Kosten (LLM)

Preise laut aktueller Anthropic-Preisliste: Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10
pro Mio. Tokens (Input/Output). Batch API −50 %, Cache-Reads ≈ 10 % des Input-Preises.

| Job | Annahme | $/Tag |
|---|---|---|
| News-Filter (Haiku) | ~600 Meldungen × 300 Tok in, 50 Tok out | ~0.03 |
| Nachtlauf Analysen (Sonnet, Batch) | ~8 Titel mit neuen Daten × 15k in / 2k out | ~0.20 |
| DevilsAdvocate + Risk (Sonnet, Batch) | nur Kandidaten ≥ 70, ~3 × 8k in / 1.5k out | ~0.05 |
| Thesen-Check Bestand (Sonnet, Batch) | ~10 Positionen, nur bei neuen Daten | ~0.08 |
| `/check` (Sonnet, live) | ~0.08 pro Aufruf, ~3/Tag | ~0.25 |
| **Typisch** | | **~0.40–0.80** (≈ CHF 12–25/Monat) |

- **Hartes Limit** `DAILY_LLM_BUDGET_USD=3`: Vor jedem Call wird mit `count_tokens` +
  max_tokens der Worst Case geschätzt; reicht das Restbudget nicht, wird der Call
  übersprungen (bzw. in den nächsten Nachtlauf geschoben) und `/status` zeigt es.
  Ist-Kosten aus `response.usage` landen in SQLite (`/costs`).
- Prompt-Caching: stabile System-Prompts + Regelwerk als gecachter Prefix.
- Datenkosten: 0 (nur Gratis-Quellen). Hosting: siehe Frage 3.

---

## 7. Zeitplan der Jobs (Europe/Zurich)

| Wann | Was |
|---|---|
| alle 30 min (Mo–Fr 07–23) | News, Form 4, Kurse Bestand/Watchlist → Sofort-Alerts |
| täglich 02:00 | Batch: Fundamental/Quality/Valuation für Titel mit neuen Daten, Thesen-Check |
| täglich 06:30 | Batch-Ergebnisse auswerten → Orchestrator → Alerts |
| wöchentlich So | Universum-Scan (neue Kandidaten), ETF-Ranking, ProjectScout |
| 13F-Saison (Feb/Mai/Aug/Nov) täglich | SuperinvestorAgent |
| 1. des Monats 08:00 | Monatsbericht, PerformanceTracker |

---

## 8. Qualität & Betrieb

- Tests pro Agent mit aufgezeichneten API-Antworten (keine Live-Calls in CI), LLM gemockt.
- Strukturiertes Logging (JSON), Secrets werden im Logger maskiert.
- Retries mit Exponential Backoff (tenacity), Cache-TTL je Quelle.
- `.env` in `.gitignore`, `.env.example` ohne Werte.
- Docker-Image + `docker-compose.yml` mit Volume für SQLite.

---

## 9. Phasen

| Phase | Inhalt |
|---|---|
| 1 | Grundgerüst, Config, DB, LLM-Client + Budget, Telegram (+ `/check` `/watchlist` `/status` `/costs`), Watchlist, NewsAgent, FundamentalAgent, Docker |
| 2 | Quality, Valuation, Superinvestor, Insider, PortfolioKeeper (`/holdings` `/buy` `/sell`) |
| 3 | ETFAgent, CryptoAgent, ProjectScout |
| 4 | Risk, DevilsAdvocate, Orchestrator (`/top`, alle Alert-Typen), PerformanceTracker, Monatsbericht |

---

## 10. Offene Punkte (brauche deine Antwort)

1. **Bybit-Bot:** Der Pfad ist noch der Platzhalter `<PFAD-ZUM-BYBIT-BOT>` und der Bot liegt
   nicht in dieser Umgebung. Nenne mir das GitHub-Repo (owner/name), dann binde ich es
   nur lesend ein. Alternativ schreibe ich Telegram- und GDELT-Modul neu. Die
   `TELEGRAM_CHAT_ID` trägst du dann selbst in `.env` ein.
2. **Alter Code im Repo** (`main.py`, `render.yaml`, `build.sh`, PeaceJudgeBot):
   Vorschlag – entfernen (bleibt in der Git-Historie).
3. **Hosting:** Der Bot muss 24/7 laufen (Scheduler). Render-Free schläft ein und ist
   ungeeignet. Vorschlag: Docker auf kleinem VPS (z. B. Hetzner/Infomaniak, ~CHF 5/Monat)
   oder Raspberry Pi/NAS zu Hause.
4. **Watchlist-Details:** „Gold“ → physisch hinterlegter Gold-ETC/ETF in CHF
   (z. B. ZKB Gold ETF) ok? `R3NK` = RENK Group (Xetra) – richtig?
5. **Depot:** Basiswährung CHF? Und hast du schon Positionen für `holdings.yaml`
   (Ticker, Stückzahl, Kaufdatum, Kaufkurs, Kaufgrund)?
