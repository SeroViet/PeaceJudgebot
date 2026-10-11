# Alpha Scout

Langfrist-Investment-Scout (3–10 Jahre) für Aktien, ETFs und Krypto. Sucht weltweit nach soliden
Qualitätsanlagen, prüft sie mit 14 Agenten und meldet sich per Telegram (@scout_aktien_krypto_bot)
**nur, wenn es wichtig ist**. Du kaufst selbst bei Swissquote.

> Recherche-Werkzeug, keine Anlageberatung. Jede Meldung nennt Quellen, Risiken und Gegenargumente.

## Einrichtung

1. `.env` anlegen: `cp .env.example .env` und ausfüllen
   - `ANTHROPIC_API_KEY` – console.anthropic.com
   - `TELEGRAM_BOT_TOKEN` – von @BotFather für @scout_aktien_krypto_bot
   - `TELEGRAM_CHAT_ID` – deine Chat-ID (z. B. aus dem Bybit-Bot übernehmen oder per @userinfobot)
   - `SEC_EDGAR_USER_AGENT` – Pflicht laut SEC: `"Vorname Nachname email@beispiel.ch"`
   - `DAILY_LLM_BUDGET_USD=3` – hartes Tageslimit
2. `config/holdings.yaml` mit deinem Bestand füllen (oder später per `/buy`).
3. In `config/swissquote_universe.yaml` Titel eintragen, die du bei Swissquote geprüft hast → ✅ statt ⚠️.

### Start auf Render (wie der Fussball-Bot)

Schritt-für-Schritt-Anleitung: [DEPLOY.md](DEPLOY.md) – eigener Background Worker mit Disk.

### Start mit Docker (eigener Server)

```bash
cd alpha-scout
docker compose up -d --build
docker compose logs -f
```

`./data` enthält die SQLite-Datenbank, `./config` ist eingebunden (Watchlist/Bestand ohne Neubau änderbar).
Beim allerersten Start läuft nach 1 Minute automatisch eine Analyse-Runde (Bestand + Watchlist) und der
13F-Abgleich, damit `/top` und `/watchlist` sofort Werte zeigen.

### Start lokal

```bash
cd alpha-scout
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m alpha_scout                 # Bot + Scheduler
python -m alpha_scout --once check MSFT   # Einzelanalyse in die Konsole
python -m alpha_scout --once nightly      # Nachtlauf manuell
pytest                                     # Tests
```

## Telegram-Befehle

| Befehl | Wirkung |
|---|---|
| `/check TICKER` | Vollanalyse (Yahoo-Ticker: `MSFT`, `NESN.SW`, `R3NK.DE`, `BTC`, `Gold`) |
| `/top` | beste Titel der letzten Analysen |
| `/watchlist` | Watchlist mit Scores, Kaufstopps |
| `/holdings` | Depot in CHF, Aufteilung Kern/Qualität/Rest, 6-Monats-Sperren |
| `/buy TICKER STÜCK PREIS [WÄHRUNG] [kern\|qualitaet\|rest] [TT.MM.JJJJ] [These]` | Kauf erfassen |
| `/sell TICKER STÜCK [PREIS]` | Verkauf erfassen (warnt bei < 6 Monaten) |
| `/costs` | LLM-Kosten heute/Monat/je Agent |
| `/status` | Systemstatus, Restbudget, letzte Läufe, Fehler |

Der Bot reagiert nur auf `TELEGRAM_CHAT_ID`.

## Meldungen

| Typ | Auslöser |
|---|---|
| 💎 NEUE CHANCE | nicht im Bestand, kein Veto, Score ≥ 80 (ETF nur, wenn Kategorie-Sieger und Kategorie fehlt) |
| 📈 NACHKAUF | Bestand/Watchlist, ≥ 20 % unter 52W-Hoch oder Kaufkurs (Krypto ≥ 40 %), Qualität intakt |
| ⚠️ WARNUNG | Bestand: Qualitätsfilter/Bilanz/SEC-Verfahren/Devil's-K.-o. oder These gefährdet (sofort, auch nachts) |
| 💰 VERKAUFEN PRÜFEN | Bestand ≥ 6 Monate und Kurs in Verkaufszone oder These zerbrochen |
| 🐋 SUPERINVESTOR | neuer 13F: grosse neue Position (≥ 3 %) / Aufstockung ≥ 50 % / Kaufcluster ≥ 3 Investoren |

Format: Was – Warum (3 Sätze) – Fakten – Risiken – Gegenargumente – Handlung – Quelle – Swissquote ✅/⚠️.
Ruhezeiten (Standard 22–7 Uhr) sammeln Meldungen, ausser Warnungen. Gleiche Meldung höchstens alle
7–60 Tage (je nach Typ). Am 1. des Monats kommt der Monatsbericht.

## Zeitplan (Europe/Zurich)

| Wann | Job |
|---|---|
| Mo–Fr 07–23 Uhr, :05/:35 | Intraday: News (GDELT, Notenbanken, SEC), Kurse Bestand/Watchlist, Thesen-Check bei Risiko-News |
| Sa/So alle 3 Std. | Intraday (Krypto/News) |
| täglich 02:00 | Nachtlauf: Analysen per Batch-API (−50 %), Sonntag inkl. Welt-Universum |
| alle 15 Min. | Batch-Ergebnisse abholen → Orchestrator → Meldungen |
| So 01:00 | ProjectScout (USAspending, TED, SIMAP), ETF-Ranking |
| Feb/Mai/Aug/Nov 1.–25. täglich, sonst Mi | 13F-Superinvestoren |
| 1. des Monats 08:00 | Monatsbericht + PerformanceTracker |

## Architektur

```
Daten (yfinance, SEC EDGAR, GDELT, RSS, CoinGecko, DefiLlama, USAspending/TED/SIMAP)
  └─ Cache (SQLite) + Retries + Rate-Limits
Agenten: Fundamental · Quality · Valuation · Superinvestor · Insider · News · ProjectScout · ETF · Crypto
Kontrolle: Risk (Veto) · DevilsAdvocate (K.-o.)
Orchestrator (Score 0–100, Alerts) · PortfolioKeeper · PerformanceTracker (Agenten-Gewichte 0.5–1.5)
```

- **Zahlen rechnet Python, Urteile schreibt Claude.** Claude bekommt ein Faktenblatt (jeder Wert mit
  Quelle und Datum) und antwortet per Structured Output; jede Aussage muss eine `fact_id` zitieren,
  sonst wird sie verworfen. Kein Websearch-Tool.
- Modelle: `claude-haiku-5-5` (News-/Ausschreibungs-Filter), `claude-sonnet-5-5` (Burggraben,
  Advocatus Diaboli, These-Check, Meldungstext). Prompt-Caching für die Systemprompts, Batch-API nachts,
  LLM-Ergebnisse werden wiederverwendet, solange sich die Fakten nicht ändern.
- Budget: Vor jedem Aufruf wird der schlimmste Fall reserviert; reicht `DAILY_LLM_BUDGET_USD` nicht,
  wird der Aufruf übersprungen. Ohne API-Key läuft alles Deterministische weiter.

### Qualitätsfilter (mind. 4 von 6)

1 Profitabel/Weg dahin · 2 Umsatz > 10 % p. a. (3 J.) · 3 starker Burggraben (Claude, ≥ 2 Belege) ·
4 Nettoverschuldung < 3× EBITDA · 5 Verwässerung ≤ 5 % p. a. · 6 Top-Investor im letzten 13F.
Fehlende Daten zählen als nicht erfüllt. Ausschluss: Kurs < 5 USD, < 1 Mrd USD Marktkap. bei Verlust,
SEC-Verfahren.

### Bewertung

FCF-Modell auf Firmenebene (ADR-sicher, Finanzwährung wird umgerechnet): Bär/Basis/Bulle mit
Wachstum aus Umsatz-CAGR (max. 25 %), Exit-Multiple 15/20/25× FCF, Diskontsatz 9 %. Kaufzone =
fairer Wert Basis − 10 %, Verkaufszone = fairer Wert Bulle. Zusätzlich Reverse-DCF (eingepreistes
Wachstum). Alles in `config/scoring.yaml` einstellbar.

## Konfiguration

| Datei | Inhalt |
|---|---|
| `watchlist.yaml` | Watchlist, Kaufstopps, Kurznamen (`Gold` → `ZGLD.SW`, `R3NK` → `R3NK.DE`) |
| `holdings.yaml` | Bestand (wird von `/buy`/`/sell` gepflegt) |
| `scoring.yaml` | Gewichte, Schwellen, Bewertungsannahmen, Depotregeln 60/30/10, 5 %/Aktie |
| `superinvestors.yaml` | 14 Top-Investoren (CIKs gegen SEC geprüft) |
| `universe.yaml` | weltweites Startuniversum für den Sonntags-Scan |
| `etf_universe.yaml` | ETF-Kandidaten je Kategorie (MSCI World CH/IE u. a.) |
| `crypto.yaml` | Allowlist BTC/ETH/SOL, Kriterien, Memecoin-Sperrliste |
| `themes.yaml` | ProjectScout-Themen, Stichworte, Profiteur-Kandidaten |
| `news.yaml` | RSS-Feeds (Fed, EZB, SNB, BoJ, SEC), GDELT-Abfragen |
| `swissquote_universe.yaml` | von dir bestätigte Titel, Börsenraster |

## Grenzen (ehrlich)

- **13F** kommt bis 45 Tage nach Quartalsende und zeigt nur US-Long-Positionen.
- **Insider (Form 4)** gibt es nur für US-Firmen; für CH/EU fehlt eine gleichwertige Gratis-Quelle.
- **yfinance** ist inoffiziell – Datenlücken werden als „unbekannt“ gezeigt, nie geschätzt.
- **Swissquote** hat keine öffentliche API: ⚠️ heisst „Börse wird angeboten, Titel bitte prüfen“.
- **Krypto-Unlocks/Hacks** nur über News (DefiLlama-Unlocks ist kostenpflichtig).
- Die Bewertung ist ein Modell mit Annahmen, keine Wahrheit – deshalb Bär/Basis/Bulle.
- Performance wird in der Währung der Empfehlung gegen den MSCI World (URTH, umgerechnet) gemessen,
  ohne Dividenden.
