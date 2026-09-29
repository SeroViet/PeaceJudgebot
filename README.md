# PeaceJudgebot – Fussball-Prognose-KI

Private App (Web + Telegram), die für Fussballspiele kalibrierte Wahrscheinlichkeiten
berechnet, sie mit Buchmacherquoten vergleicht und **nur Tipps mit echtem Value**
vorschlägt, inklusive Einsatzempfehlung (Viertel-Kelly), Mindestquote und Kombis.
Spezifikation: `docs/prompt_fussball_ki.md`.

> Wetten ist mit Verlustrisiko verbunden. Das System maximiert den Erwartungswert,
> garantiert aber keine Gewinne. Eine Liga wird nur freigegeben, wenn das Modell dort
> im Backtest einen messbaren Vorteil hatte.

## Stand

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Datenbasis, football-data.co.uk-Import, API-Football-Client, `known_at` | ✅ |
| 4 | Dixon-Coles + ELO + Markt-Blend, alle Märkte, Walk-Forward-Backtest | ✅ |
| 5/5b | Value, Viertel-Kelly mit Limits, Kombi-Builder, Systemwetten | ✅ |
| 6 | Report pro Spiel (Web + Telegram) | ✅ Basis |
| 8 | Private PWA (Login + 2FA) und Telegram-Bot, Scheduler, Docker/Render | ✅ |
| 2 | Ausfälle, Sperren, Impact, Ersatz-Delta | offen – braucht API-Football PRO für die laufende Saison |
| 3/3b/3c | Kontext-Features, LightGBM, SHAP, LLM-Agenten | offen |
| 7 | Automatisches Post-Mortem | offen (CLV-Tracking läuft bereits) |

## Schnellstart (lokal)

```bash
pip install -r requirements.txt
python -m fussball import-fd --leagues E0 E1 D1 D2 I1 I2 SP1 SP2 F1 F2 \
    --seasons 2021 2122 2223 2324 2425 2526 2627     # ~10 Min.
python -m fussball backtest                           # ~5 Min., legt Gewichte + freigegebene Ligen fest
python -m fussball refresh                            # kommende Spiele + Tipps
python -m fussball set-password                       # Passwort + 2FA für die Web-App
APP_HTTPS_ONLY=0 python main.py                       # http://localhost:8000
python -m pytest
```

## Befehle

| Befehl | Zweck |
|---|---|
| `import-fd` | Saisons von football-data.co.uk laden (idempotent, gecacht) |
| `backtest` | Walk-Forward-Backtest, schreibt `storage/backtest_summary.json` |
| `refresh` | laufende Saison + kommende Spiele laden, Wetten abrechnen, Tipps berechnen |
| `serve` / `python main.py` | Web-App + Telegram-Bot + Scheduler (alle 3 h, Tagesreport 09:00) |
| `set-password` | Passwort-Hash, TOTP-Secret und Session-Secret erzeugen |
| `status` | Datenbestand |

## Umgebungsvariablen

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `APP_PASSWORD_HASH`, `APP_TOTP_SECRET`, `SESSION_SECRET` | ja | aus `python -m fussball set-password` |
| `TELEGRAM_TOKEN` | für Bot | von @BotFather |
| `TELEGRAM_OWNER_ID` | für Bot | deine Telegram-ID (Bot schickt sie dir mit `/id`) |
| `FUSSBALL_STORAGE_DIR` | nein | Ort für DB und Cache (Render: `/var/data`) |
| `DATABASE_URL` | nein | Standard SQLite, Postgres möglich |
| `ODDS_API_KEY` | **ja, für Tipps** | [the-odds-api.com](https://the-odds-api.com), Free: 500 Credits/Monat (2 je Liga und Abruf) |
| `API_FOOTBALL_KEY` | nein | erst für Phase 2 |
| `DAILY_REPORT_TIME` | nein | Tagesreport (Europe/Zurich), Standard `09:00` |

## Telegram

`/heute` Einzeltipps · `/kombi` Kombis · `/spiel Bayern` Prognose · `/bilanz` ·
`/gesetzt 2 10 2.15` (Tipp #2, 10 CHF, Quote 2.15) · `/gesetzt K1 5 12.4` · `/update` · `/id`.
Der Bot antwortet nur auf `TELEGRAM_OWNER_ID`. Automatisch: Tagesübersicht, Alarm bei
neuen/gestrichenen Tipps, Meldung nach Abrechnung.

## So entstehen die Tipps

**Hauptmodus „Markt-Value“ (aktiv):** Die faire Wahrscheinlichkeit kommt aus den
Pinnacle-Quoten (ohne Marge, Power-Methode). Pinnacle gilt als schärfster Buchmacher.
Ein Tipp entsteht, wenn ein anderer Buchmacher mindestens 3 % mehr zahlt als fair wäre.

| Backtest 2020–2026, 10 Ligen | Wetten | ROI | CLV |
|---|---|---|---|
| Pinnacle als Referenz, beste Buchmacher-Quote, Edge ≥ 3 % | 817 | +3.2 % | **+3.6 %** (jede Saison positiv, t = 11) |
| Betfair Exchange als Referenz | 150 | +7.5 % | −2.0 % → **nicht verwendet** |
| eigenes Modell (Dixon-Coles + ELO + Markt), Premier League | 367 | −7.5 % | −4.9 % → **gesperrt** |

Der CLV (Quote gegenüber der fairen Schlussquote) ist der verlässlichste Beweis für
einen echten Vorteil; der ROI schwankt kurzfristig stark. Ohne Pinnacle-Quote gibt die
App **keinen** Tipp. Pinnacle-Quoten kommen live von **The Odds API** (`ODDS_API_KEY`).

**Wichtig für die Praxis:** Der Vorteil entsteht nur, wenn du bei dem Buchmacher wettest,
der die bessere Quote anbietet. Die App zeigt deshalb pro Tipp den Anbieter und eine
**Mindestquote**. Liegt dein Anbieter darunter, nicht spielen. Nutze nur in deinem Land
zugelassene Anbieter. Buchmacher können Konten von Gewinnern limitieren.

**Eigenes Modell (Zusatz, derzeit gesperrt):**

1. **Dixon-Coles** (Torraten je Team) mit Zeitgewichtung (ξ = 0.003/Tag), Shrinkage und
   gemeinsamer Schätzung mit der 2. Liga, damit Aufsteiger bekannt sind. Die Raten werden
   zu 60 % aus Toren und zu 40 % aus Schüssen aufs Tor geschätzt (stabileres Signal).
2. **ELO** als zweite Stimme.
3. **Markt-Blend**: Buchmacherquoten ohne Marge (Pinnacle, sonst Betfair Exchange) als
   Prior. Die Gewichte werden im Backtest nur auf vergangenen Saisons bestimmt.
4. **Value**: Edge = p × Quote − 1 gegen die Durchschnittsquote. Jeder Tipp zeigt eine
   **Mindestquote**: Liegt dein Anbieter (z. B. Sporttip) darunter, nicht spielen.
5. **Einsatz**: Viertel-Kelly, max. 2 % pro Tipp, 6 %/Tag, 15 %/Woche (einstellbar).
6. **Kombis**: nur Tipps mit eigenem Value (p ≥ 60 %, Edge ≥ 3 %), ein Tipp pro Spiel,
   Varianten „sicher / ausgewogen / hoher EV“, mit Wahrscheinlichkeit von Verlustserien.

### Backtest-Ergebnis (Variantenvergleich, Testsaisons 2022/23–2026/27, 5 Top-Ligen)

Log-Loss-Abstand des reinen Modells zum Markt (kleiner = besser):

| Variante | 1X2 | Über/Unter 2.5 |
|---|---|---|
| nur Hauptliga | 0.0209 | 0.0125 |
| + 2. Liga | 0.0181 | 0.0094 |
| + 40 % Schüsse aufs Tor, ξ = 0.003 | **0.0172** | **0.0078** |

Das Modell allein ist schwächer als der Markt, das ist bei Top-Ligen normal. Tipps
entstehen deshalb aus der Kombination mit dem Markt; die Freigabe je Liga steht in der
App unter „Mehr → Modell“.

## Deployment

**VPS (empfohlen):** `.env` anlegen, `DOMAIN=tipps.example.ch docker compose up -d`.
Caddy holt automatisch ein HTTPS-Zertifikat; der `backup`-Dienst sichert die DB täglich.

**Render:** `render.yaml` startet `python main.py` als Web-Service mit persistenter Disk
(`/var/data`). Secrets im Render-Dashboard eintragen. Nach dem ersten Deploy einmal in der
Render-Shell `import-fd` und `backtest` ausführen.

## Datenquellen

| Quelle | Inhalt | Hinweise |
|---|---|---|
| [football-data.co.uk](https://www.football-data.co.uk) | Ergebnisse, Schüsse, Karten, Vorab-/Schlussquoten, kommende Spiele (`fixtures.csv`), ab 2026/27 xG | frei für private Nutzung. Pinnacle nur bis Mitte 2025/26, danach Betfair Exchange |
| [The Odds API](https://the-odds-api.com) | Live-Quoten inkl. **Pinnacle** und EU-Buchmachern | Free 500 Credits/Monat; Pflicht für Tipps |
| [API-Football](https://www.api-football.com) | Aufstellungen, Verletzungen, Karten | Free: nur Saisons 2022–2024; laufende Saison ab PRO (19 $/Monat) |

## Kein Data Leakage: `known_at`

Jede Zeile trägt `known_at` („bekannt ab“, UTC). Der Backtest prognostiziert jedes Spiel
zum Zeitpunkt der Vorab-Quoten und nutzt nur Ergebnisse, die davor bekannt waren.
Schlussquoten dienen ausschliesslich der CLV-Messung.

## Struktur

```
config/            leagues.yaml, betting.yaml
fussball/data/     Schema, Importer, API-Client, Point-in-Time
fussball/models/   Dixon-Coles, ELO, Märkte, Devig, Pooling, Backtest
fussball/betting/  Value, Kelly, Kombis
fussball/app/      Web-App, Login, Telegram, Jobs, Templates
fussball/service.py  Prognosen, Tipps, Wett-Abrechnung
tests/             pytest (52 Tests)
```
