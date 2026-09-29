# PeaceJudgebot – Fussball-Prognose-KI

Berechnet kalibrierte Wahrscheinlichkeiten für Fussballspiele, vergleicht sie mit
Buchmacherquoten und markiert nur echten Value. Aufbau in Phasen, siehe
`docs/prompt_fussball_ki.md`.

## Stand

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Datenbasis: Schema, football-data.co.uk-Import, API-Football-Client | ✅ |
| 2 | Spieler-Verfügbarkeit (Ausfälle, Sperren, Impact, Ersatz-Delta) | offen |
| 3 | Kontext- und erweiterte Features | offen |
| 4 | Modell (Dixon-Coles, ELO, LightGBM, Markt-Blend, Kalibrierung) | offen |
| 5 | Value, Kelly, Kombi-Builder | offen |
| 6–8 | Reports, Post-Mortem, private App / Telegram | offen |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # optional, Werte eintragen
python -m fussball init-db
python -m fussball import-fd --seasons 2021 2122 2223 2324 2425 2526 2627
python -m fussball status
python -m pytest
```

- `import-fd` ohne `--leagues` importiert die Ligen mit `enabled: true` in
  `config/leagues.yaml`; ohne `--seasons` nur die laufende Saison (immer frisch
  geladen). Abgeschlossene Saisons werden unter `storage/raw/` gecacht.
- Der Import ist idempotent: mehrfach ausführen ändert nichts ausser Korrekturen.

## Struktur

```
config/            Liga-Konfiguration (später: Sperrregeln, Einsatzlimits)
fussball/data/     Schema, DB, Importer, API-Clients, Point-in-Time-Abfragen
fussball/features/ Feature-Module (Phase 2/3)
fussball/models/   Modelle (Phase 4)
fussball/betting/  Value, Kelly, Kombis (Phase 5)
fussball/reports/  Berichte, Post-Mortems (Phase 6/7)
tests/             pytest
main.py            Telegram-Bot (wird in Phase 8 ausgebaut)
```

## Datenquellen

| Quelle | Inhalt | Zugang / Hinweise |
|---|---|---|
| [football-data.co.uk](https://www.football-data.co.uk) | Ergebnisse, Schüsse, Karten, Schiedsrichter (teilweise), Vorab- und Schlussquoten; ab 2026/27 auch xG | frei für private Nutzung, Quelle nennen. Pinnacle-Quoten nur bis Mitte 2025/26, danach Betfair Exchange (`BFE`) als scharfe Referenz |
| [API-Football](https://www.api-football.com) | Spielpläne, Aufstellungen, Verletzungen, Karten, Spielerstatistiken | Free: 100 Anfragen/Tag, **nur Saisons 2022–2024**. Aktuelle Saison erst ab PRO (19 $/Monat) |

## Kein Data Leakage: `known_at`

Jede Zeile trägt `known_at` („bekannt ab“, UTC). Backtests lesen Daten nur über
`fussball/data/point_in_time.py`, das auf `known_at <= Prognosezeitpunkt` filtert.

- Ergebnisse: Anpfiff + 2h15
- Vorab-Quoten football-data: Freitag bzw. Dienstag 16:00 UK, höchstens Anpfiff − 1h
- Schlussquoten: Anpfiff (nur für CLV, nie als Modell-Input vor Anpfiff)
