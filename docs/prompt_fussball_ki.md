# Prompt für Claude Code / Cowork: Fussball-Prognose-KI

> Diesen ganzen Text als ersten Prompt einfügen. Pfade und API-Keys vorher anpassen.

---

## Rolle & Kontext

Du bist Senior Data Scientist und Python-Entwickler mit Erfahrung in Sportwetten-Modellen.
Ich habe bereits ein Projekt mit **Dixon-Coles- und ELO-Modellen in Python**, **Walk-Forward-Backtesting** und **Kelly-Sizing** (Pfad: `<HIER PFAD EINFÜGEN>`). Lies zuerst den bestehenden Code und baue darauf auf, statt alles neu zu schreiben.

**Ziel:** Ein System, das für Fussballspiele (Klubligen UND Nationalmannschaften) kalibrierte Wahrscheinlichkeiten berechnet, sie mit Buchmacherquoten vergleicht, nur echten Value markiert und jede Prognose nachvollziehbar begründet. Nach jedem Spiel soll es selbst analysieren, warum ein Tipp falsch war.

**Wichtig:** Stell mir zuerst Rückfragen, bevor du mit Code anfängst. Arbeite dann in den Phasen unten, eine nach der anderen, mit Tests. Zeig mir nach jeder Phase, was fertig ist.

---

## Phase 1 – Datenbasis

Baue eine Datenpipeline (SQLite zum Start, später Postgres-fähig) mit diesen Tabellen: `matches`, `teams`, `players`, `lineups`, `player_match_stats`, `injuries`, `cards`, `suspensions`, `odds`, `predictions`, `bets`, `post_mortems`.

Quellen (prüfe jeweils Lizenz/ToS und aktuelle Verfügbarkeit, schlag Alternativen vor):
- **Ergebnisse + historische Quoten Klubs:** football-data.co.uk
- **Spielpläne, Aufstellungen, Verletzungen, Karten, Sperren:** API-Football (api-sports.io) oder vergleichbare API
- **Spieler-Stats / xG:** die beste aktuell verfügbare Quelle (z. B. Understat, FBref, API-Anbieter) – prüfe, was heute noch frei nutzbar ist
- **Marktwerte / Verletzungshistorie:** Transfermarkt (nur wenn ToS-konform, sonst API-Alternative)
- **Nationalteam-Stärke:** Elo-Ratings (eloratings.net) + Aggregation der Spielerstärke aus ihren Klubs
- **Live-/Vorabquoten:** The Odds API oder vergleichbar, inkl. **Schlussquote** (Closing Line)

Regeln:
- Jeder Datensatz bekommt einen Zeitstempel **„bekannt ab“** – im Backtest darf nur verwendet werden, was vor Anpfiff bekannt war (kein Data Leakage).
- Idempotente Updates, Logging, Retry bei API-Limits, API-Keys in `.env`.

---

## Phase 2 – Spieler-Verfügbarkeit (Kernmodul)

Für jedes Spiel und beide Teams: **Wer fehlt, warum, wie lange, und wie schlimm ist es?**

### 2.1 Ausfälle erfassen
- **Verletzung:** Art, Datum, erwartete Rückkehr, Status (fraglich / fällt aus / zurück, aber nicht fit)
- **Rote Karte:** direkte Rot oder Gelb-Rot, daraus resultierende Sperre
- **Gelbsperre:** Kartenzähler pro Spieler **pro Wettbewerb**, Sperre bei Erreichen der Grenze
- **Sperrregeln als konfigurierbare Tabelle pro Wettbewerb** (Bundesliga, Premier League, Serie A, Champions League, Nations League, WM-/EM-Quali usw.): Gelbgrenze, Sperrdauer bei Rot/Gelb-Rot, ob Karten verfallen (z. B. nach Gruppenphase). Recherchiere die aktuellen Regeln und hinterlege die Quelle.
- **Sonstiges:** nicht nominiert, Rotation angekündigt, abgereist, private Gründe
- **Unsicherheit:** Wenn Quellen sich widersprechen (z. B. „Torhüter fällt aus“ vs. „spielt“), speichere beide Angaben mit Quelle und Zeitstempel und rechne mit einer Wahrscheinlichkeit statt einfach eine Angabe zu übernehmen.

### 2.2 Wichtigkeit des Spielers
Berechne pro Spieler einen **Impact-Score**:
- Einsatzminuten-Anteil der letzten 10 Spiele (Stammspieler ja/nein)
- Offensiv: xG + xA pro 90, Schüsse, Schlüsselpässe
- Defensiv: Zweikämpfe, Ballgewinne, Gegentore/xGA mit vs. ohne ihn (on/off)
- **Torhüter separat:** Paraden-Quote, PSxG − Gegentore (Goals Prevented)
- Marktwert als Zusatzsignal, Kapitän/Spielmacher-Rolle

### 2.3 Ersatz-Delta
Das Entscheidende ist nicht nur *wer fehlt*, sondern *wer ihn ersetzt*:
- Voraussichtlichen Ersatzspieler bestimmen (gleiche Position, Kaderhierarchie)
- **Delta = Impact(Stammspieler) − Impact(Ersatz)**
- Positionsgewichtung: Ein fehlender **Stammtorhüter** oder **einziger echter Stürmer** wiegt schwerer als ein fehlender Aussenverteidiger bei breitem Kader
- Kumulierte Effekte: Mehrere Ausfälle im gleichen Mannschaftsteil (z. B. 3 Stürmer fehlen) überproportional gewichten

### 2.4 Output
Pro Team: angepasste erwartete Tore (Angriff) und Gegentore (Abwehr) durch Ausfälle, z. B. „Stammkeeper gesperrt → +0.20 erwartete Gegentore“.

---

## Phase 3 – Kontext-Features

- Heim/Auswärts, neutraler Platz
- Ruhetage, Reisedistanz, englische Wochen
- **Motivation / Spielsituation:** Tabellenlage, Muss-Sieg (z. B. nach Auftaktniederlage), Abstiegs-/Aufstiegskampf, bereits qualifiziert, Freundschaftsspiel vs. Pflichtspiel
- Trainerwechsel, angekündigte Rotation
- Form: xG-Differenz der letzten 5–10 Spiele (nicht nur Ergebnisse!) mit abnehmender Gewichtung
- **Stichprobengrösse beachten:** Bei wenigen Spielen (z. B. 1 Spiel in neuer Saison) stark zum Langzeitwert zurückziehen (Shrinkage)
- Nationalteams: Stärke der voraussichtlichen Startelf aus Klub-Daten der Spieler berechnen

---

## Phase 3b – Erweiterte Features („gross denken“)

Baue jede Feature-Gruppe als eigenes Modul, das sich an- und abschalten lässt. **Jede Gruppe muss per Ablation-Test im Backtest beweisen, dass sie Log Loss / CLV verbessert – sonst fliegt sie raus.** Priorität: A = zuerst, B = danach, C = später/experimentell.

### Müdigkeit & Belastung (A)
- Einsatzminuten pro Spieler in den letzten 7 / 14 / 30 Tagen, Anzahl Spiele in 10 Tagen
- Ruhetage seit letztem Spiel (Team und pro Startspieler)
- Reisedistanz, Zeitzonen-Wechsel (z. B. Spieler aus MLS/Südamerika bei Länderspielen), Jetlag-Tage
- Rückkehrer aus Länderspielpause (Reise, Minuten im Nationalteam)
- Nächstes wichtiges Spiel in ≤ 4 Tagen → Rotationsrisiko
- Falls verfügbar (meist kostenpflichtig): Laufdistanz, Sprints, High-Intensity-Runs

### Erholung & Fitness (A)
- Spieler zurück nach Verletzung: Tage seit Rückkehr, Minuten seitdem (Spielpraxis), Minutenbegrenzung
- Verletzungshistorie / Anfälligkeit pro Spieler
- Alter × Belastung

### Hin- und Rückspiel / K.o.-Modus (A)
- Ergebnis des Hinspiels, aktueller Gesamtstand, was jedes Team braucht
- Regeln pro Wettbewerb (Auswärtstorregel ja/nein, Verlängerung, Elfmeterschiessen)
- Game-State-Effekt: Team mit Vorsprung verwaltet, Team im Rückstand muss angreifen → Einfluss auf Torerwartung und Über/Unter
- Wichtig: Wettmärkte beziehen sich meist auf 90 Minuten – Verlängerung separat behandeln

### Motivation & Situation (A)
- Tabellenlage, Punkte zum Ziel (Titel, Europa, Abstieg), bedeutungslose Spiele
- Muss-Sieg-Situationen, Gruppenkonstellation (wer braucht welches Ergebnis)
- Derby / Rivalität
- Trainer unter Druck, neuer Trainer (Effekt der ersten Spiele)
- Saisonphase (Start, Winterpause, Endspurt)

### Chemie & Eingespieltheit (B)
- Wie viele Spiele hat die voraussichtliche Startelf (bzw. Paare/Achsen wie Innenverteidigung) zusammen gespielt
- Anzahl Neuzugänge in der Startelf, Kaderumbruch seit Saisonbeginn
- Amtszeit des Trainers, Stabilität der Formation
- Nationalteams: Spieler aus demselben Klub, Anzahl Länderspiele zusammen
- Passnetzwerk-Stabilität (falls Event-Daten verfügbar)

### Taktik & Matchup (B)
- Spielstil: Ballbesitz, Pressinghöhe (PPDA), direkte vs. kurze Spieleröffnung, Konter
- Stil-Matchups (hohe Linie vs. schnelle Stürmer, Pressing vs. schwacher Spielaufbau)
- Standardsituationen: Tore/xG aus Standards vs. Anfälligkeit des Gegners bei Standards, Luftzweikämpfe
- Formation und erwartete Formation

### Disziplin & Schiedsrichter (B)
- Schiedsrichter: Karten pro Spiel, Elfmeter pro Spiel, Heimtendenz
- Kartenanfälligkeit der Teams/Spieler
- **Spieler kurz vor Gelbsperre** (spielen vorsichtiger oder werden geschont)
- Relevant auch für Karten-Märkte

### Spezialrollen (A)
- Elfmeterschütze fehlt? Standardschütze fehlt? Kapitän fehlt?
- Einziger echter Stürmer / Spielmacher fehlt
- Kadertiefe pro Position (wie gut ist die Bank)

### Umfeld (B)
- Wetter: Regen, Wind, Hitze, Kälte
- Höhenlage (z. B. La Paz, Quito), Kunstrasen, Platzqualität
- Zuschauer: volles Stadion, Geisterspiel, Auswärtsfans ausgeschlossen
- Anstosszeit

### Markt-Signale (A)
- Quotenbewegung seit Eröffnung (Line Movement), Unterschiede zwischen scharfen (Pinnacle, Betfair Exchange) und weichen Buchmachern
- Plötzliche Bewegung vor Anpfiff = oft Aufstellungs-/Verletzungsinfo
- Betfair-Volumen, falls verfügbar

### News & Stimmung (C)
- NLP auf Pressekonferenzen und Team-News, **auch in Landessprache** (türkische, italienische, spanische Quellen usw.)
- Erkennen von: Verletzung, Sperre, Rotation, Streit in der Kabine, ausstehende Gehälter, Besitzerwechsel, Trainer-Entlassung
- Jede Meldung mit Quelle, Zeitstempel und Glaubwürdigkeit speichern

### Sonstiges (C)
- Aufsteiger / Absteiger, erste Saison in neuer Liga
- Pokalspiel gegen unterklassigen Gegner
- Head-to-Head nur mit geringem Gewicht (meist wenig aussagekräftig)
- Marktwert-Verhältnis der Startelfen

### Erklärbarkeit
- SHAP-Werte pro Prognose, damit der Report zeigt, welche Faktoren wie stark gewirkt haben
- Das Post-Mortem nutzt diese Werte, um zu erklären, welcher Faktor falsch lag

### Später: Live-Modell (C)
- In-Play-Prognose nach Spielstand, Minute, roten Karten, Live-xG
- Nur wenn das Pre-Match-Modell nachweislich funktioniert

---

## Phase 3c – Experten-Agenten (Anthropic API)

**Grundregel:** Die Agenten sammeln, prüfen und erklären Informationen. **Die Wahrscheinlichkeiten berechnet ausschliesslich das statistische Modell** (Phase 4). LLMs dürfen keine Prozentwerte schätzen, die direkt in Tipps einfliessen – sie liefern nur strukturierte Features (JSON) an das Modell.

Umsetzung mit der Anthropic API (`anthropic` Python SDK), Tool Use + Web Search. Modellwahl pro Agent konfigurierbar (günstiges Modell wie `claude-haiku-4-5-20251001` für Massen-Scans, stärkeres wie `claude-sonnet-5` für Abgleich und Reports). Prompt Caching und Batch API nutzen, wo möglich. Kosten pro Lauf loggen, Tagesbudget als Limit.

### Agenten
1. **News-Scout** – durchsucht pro Spiel Team-News, Pressekonferenzen, lokale Medien **in Landessprache**. Output: JSON mit Ausfällen, Grund, Dauer, Quelle, Zeitstempel, Glaubwürdigkeit.
2. **Kader- & Sperren-Agent** – gleicht News-Scout-Ergebnisse mit API-Daten (Verletzungen, Karten, Sperrregeln) ab, löst Widersprüche auf oder markiert sie mit Wahrscheinlichkeit. Prüft aktiv: Wer hat im letzten Spiel Rot gesehen? Wer steht vor einer Gelbsperre?
3. **Kontext-Agent** – Motivation, Tabellensituation, Hin-/Rückspiel-Stand, Trainer-Situation, Unruhe im Verein, Rotationsankündigungen.
4. **Aufstellungs-Agent** – läuft T−60min, holt offizielle Aufstellungen, triggert Neuberechnung und meldet Abweichungen zur erwarteten Startelf.
5. **Quant (kein LLM)** – das statistische Modell aus Phase 4, berechnet Wahrscheinlichkeiten und Edge.
6. **Risiko-Agent** – prüft Kombis gegen die Regeln aus Phase 5/5b (Value pro Tipp, Korrelation, Einsatzlimits) und kann Tipps blockieren.
7. **Report-Agent** – schreibt den Spielbericht auf Deutsch aus Modell-Output + SHAP-Werten + Agenten-Infos.
8. **Post-Mortem-Agent** – analysiert nach Abpfiff, warum ein Tipp verloren ging (Pech vs. Modellfehler vs. fehlende Info), und schlägt konkrete Verbesserungen vor.

### Orchestrierung
- Ein Orchestrator steuert den Ablauf: Scan → News-Scout → Kader-Agent → Kontext-Agent → Quant → Risiko-Agent → Report. T−60min: Aufstellungs-Agent → Quant → Risiko-Agent → finaler Report.
- Alle Agenten-Outputs mit festem JSON-Schema validieren (Pydantic); bei ungültigem Output Retry, dann Fallback ohne dieses Feature.
- Jede Agenten-Aussage mit Quelle speichern, damit das Post-Mortem nachvollziehen kann, welche Info falsch war.

---

## Phase 4 – Modell

1. **Basis:** Bestehendes Dixon-Coles (Torraten pro Team) + ELO
2. **Anpassung:** Gradient Boosting (LightGBM) sagt Korrekturen der Torraten aus den Features von Phase 2 + 3 vorher
3. **Markt-Blend:** Buchmacherquoten ohne Marge (z. B. Shin- oder Power-Methode) als starkes Prior einbeziehen; Modellgewicht vs. Marktgewicht per Backtest optimieren
4. **Kalibrierung:** Isotonic Regression oder Platt Scaling
5. **Märkte aus der Torverteilung ableiten:** 1X2, Doppelte Chance, Draw No Bet, Über/Unter 0.5–4.5, Beide treffen, korrektes Ergebnis
6. **Zwei Durchläufe pro Spiel:** Prognose T−24h und Update T−60min, wenn offizielle Aufstellungen da sind

Metriken: Log Loss, Brier Score, Ranked Probability Score, Kalibrierungsplot, **CLV (Closing Line Value)**, ROI im Walk-Forward-Backtest. Vergleiche immer gegen die Baseline „nur Buchmacherquote“ – das Modell muss diese schlagen, sonst ist es wertlos.

---

## Phase 5 – Value & Einsatz

- Edge = Modellwahrscheinlichkeit × Quote − 1
- Tipp nur bei Edge > konfigurierbarem Schwellenwert (Start: 5 %) UND ausreichender Konfidenz
- Einsatz: **Viertel-Kelly**, max. X % der Bankroll pro Tipp, Tages- und Wochenlimit
- Kombis nur anzeigen mit Warnung: Gesamtwahrscheinlichkeit, kumulierte Marge, Erwartungswert
- Jeden Tipp in `bets` loggen, P/L und CLV tracken

---

## Phase 5b – Multi-Liga-Scanner & Kombi-Builder

### Abdeckung
- Alle verfügbaren Wettbewerbe scannen: Top-5-Ligen, 2. Ligen, Champions/Europa/Conference League, Nationalteams, weitere europäische Ligen (NL, POR, BEL, TUR, SUI, AUT, SCO, Skandinavien usw.)
- Mehr Ligen = mehr Chancen, Value zu finden. Aber: Pro Liga eigene Kalibrierung und Mindestdatenmenge; Ligen, in denen das Modell im Backtest den Markt nicht schlägt, automatisch ausschliessen.
- Täglicher Scan aller Spiele der nächsten 48h, Ranking nach Edge × Konfidenz

### Kombi-Builder (Standard: mindestens 4 Tipps)
- **Nur Tipps mit eigenem positivem Edge dürfen in eine Kombi.** Keine „Auffüller“ mit schlechter Quote.
- Jeder Tipp: Modellwahrscheinlichkeit ≥ konfigurierbarer Mindestwert (Start: 60 %) UND Edge ≥ 3 %
- Mindestquote pro Tipp konfigurierbar (z. B. 1.50 für KombiBoost bei Sporttip)
- Nur unabhängige Spiele kombinieren (keine zwei Märkte aus demselben Spiel, keine stark korrelierten Ereignisse)
- Optimierung: Wähle die 4–6 Tipps mit maximalem Erwartungswert der Kombi bei vorgegebener Mindest-Gesamtwahrscheinlichkeit
- KombiBoost / Bonusregeln des Anbieters als Konfiguration einrechnen
- Mehrere Varianten ausgeben: „sicherer“ (höchste Gesamtwahrscheinlichkeit), „ausgewogen“, „hoher EV“
- Optional: Systemwetten (z. B. 3 aus 4, 4 aus 5) berechnen und mit der Vollkombi vergleichen

### Pflichtanzeige pro Kombi
- Gesamtwahrscheinlichkeit, Gesamtquote, Erwartungswert in % und CHF
- Wahrscheinlichkeit einer Verlustserie (z. B. 5 / 10 Kombis in Folge verloren)
- Empfohlener Einsatz (Bruchteil-Kelly, deutlich kleiner als bei Einzeltipps)
- Wenn keine Kombi die Kriterien erfüllt: **keine Kombi ausgeben**

### Tracking
- Getrenntes P/L-Tracking für Einzeltipps vs. Kombis vs. Systemwetten
- Monatlicher Vergleich: Welche Variante verdient wirklich Geld?

---

## Phase 6 – Report pro Spiel

Pro Spiel (Markdown oder Dashboard):
- Wahrscheinlichkeiten, faire Quoten, Buchmacherquoten, Edge
- **Ausfall-Liste beider Teams:** Spieler, Grund (Verletzung / Rot / Gelbsperre / nicht nominiert), Dauer, Impact-Score, Ersatz, Delta
- Die 5 wichtigsten Faktoren mit ihrem Effekt auf die erwarteten Tore
- Unsicherheiten / widersprüchliche Infos klar markieren
- Empfehlung: Tipp / kein Tipp, mit Begründung

---

## Phase 7 – Post-Mortem (automatisch nach Abpfiff)

Das System soll mir ehrlich sagen, **warum ein Tipp falsch war**:
- Tatsächliches Ergebnis vs. Prognose vs. **xG des Spiels** → war es Pech (xG passte zur Prognose) oder Modellfehler (xG weit weg)?
- Welche Infos waren vor Anpfiff verfügbar, aber falsch gewichtet oder nicht erfasst (z. B. Sperre übersehen)?
- Spielverlauf: frühe Tore, rote Karten im Spiel, die das Szenario gekippt haben
- Ergebnis als Text in `post_mortems` + Zusammenfassung pro Woche: wo liegt das Modell systematisch daneben?

**Regressionstest:** Türkei – Italien, Nations League, 28.09.2026, Endstand 1:4. Mein Tipp war Unter 2.5. Der türkische Stammtorhüter Çakır hatte im Spiel davor gegen Frankreich spät Rot gesehen. Das System muss diese Sperre erkennen, den Ersatzkeeper-Effekt einrechnen und im Post-Mortem benennen.

---

## Phase 8 – Private App (nur für mich)

### Zugang & Sicherheit
- Nur ein Benutzer (ich). Login mit Passwort + 2FA (TOTP) oder Passkey, keine Registrierung
- HTTPS, Secrets in `.env`, Rate-Limiting, nicht öffentlich indexierbar
- Hosting: kleiner VPS (z. B. Hetzner/Infomaniak) mit Docker Compose, tägliches DB-Backup

### Kanal 1: Telegram-Bot (Push)
- Täglich zu fester Uhrzeit: Tipp-Übersicht des Tages (Einzeltipps + Kombi-Varianten)
- T−60min: Update nach offizieller Aufstellung, **Alarm wenn sich ein Tipp ändert oder gestrichen wird** (z. B. Schlüsselspieler nicht in der Startelf)
- Nach Abpfiff: Ergebnis + Kurz-Post-Mortem
- Befehle: `/heute`, `/kombi`, `/spiel <Team>`, `/bilanz`, `/gesetzt <Tipp-ID> <Einsatz> <Quote>`
- Bot antwortet nur auf meine Telegram-User-ID

### Kanal 2: Web-App (PWA, auf dem iPhone installierbar)
- **Dashboard:** Tipps heute/morgen, Kombi-Varianten (sicher / ausgewogen / hoher EV) mit Wahrscheinlichkeit, Quote, EV, empfohlenem Einsatz
- **Spiel-Detail:** Ausfälle beider Teams (Grund, Dauer, Impact, Ersatz), Top-Faktoren (SHAP), Agenten-Infos mit Quellen, Quotenverlauf
- **Kombi-Ansicht:** Tipps so aufgelistet, dass ich sie schnell bei Sporttip eintippen kann (Spiel, Markt, Tipp, Mindestquote); Hinweis, wenn die aktuelle Quote unter die Mindestquote fällt
- **Wett-Tracking:** gesetzte Wetten erfassen (Einsatz, Quote beim Anbieter), automatische Abrechnung nach Spielende
- **Bilanz:** Bankroll-Verlauf, P/L, ROI, CLV, Trefferquote – getrennt nach Einzeltipps / Kombis / Liga / Markt; Chart pro Monat
- **Post-Mortems:** Liste verlorener Tipps mit Ursache (Pech / Modellfehler / fehlende Info)
- **Einstellungen:** Ligen an/aus, Mindestwahrscheinlichkeit, Mindest-Edge, Mindestquote, Kombi-Grösse, Bankroll, Einsatzlimits pro Tag/Woche, API-Tagesbudget
- **System-Status:** letzte Datenupdates, Fehler, API-Kosten pro Tag

### Nicht bauen
- Keine automatische Wettabgabe bei Buchmachern (keine offizielle API, verstösst gegen AGB). Ich setze manuell.

### Technik
- Backend: FastAPI (bestehendes Projekt), Frontend: React/Next.js als PWA oder schlankes HTMX, Tailwind
- Telegram: `python-telegram-bot`
- Web-Push-Benachrichtigungen zusätzlich zu Telegram (optional)
- Mobile-first Design, Dark Mode

---

## Technik

- Python 3.11+, pandas, scikit-learn, LightGBM, SQLAlchemy
- FastAPI + einfaches Dashboard (oder Streamlit) für Reports
- Scheduler (APScheduler oder Cron) für Datenupdates, T−24h und T−60min
- pytest für Datenpipeline, Sperrlogik und Modell
- Saubere Struktur: `data/`, `features/`, `models/`, `betting/`, `reports/`, `tests/`, `config/`
- README mit Setup, Datenquellen und Bedienung

Beginne mit deinen Rückfragen und einem Architekturvorschlag.
