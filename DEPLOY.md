# Online stellen (Render) – Schritt für Schritt

Die App läuft als **ein** Dienst: Web-App + Telegram-Bot + automatische Updates.
Beim ersten Start lädt sie selbst die Historie (ca. 5 Min.) und meldet sich per Telegram.

## 1. Telegram vorbereiten
1. In Telegram **@BotFather** öffnen → `/mybots` → deinen Bot wählen → **API Token** kopieren
   (oder `/newbot`, falls noch keiner existiert).

## 2. Render-Dienst anpassen
1. https://dashboard.render.com → Dienst **pje-buybot** öffnen.
2. **Settings**:
   - *Branch*: `claude/prompt-visibility-k1cb8w` (oder `main`, nachdem du gemergt hast)
   - *Build Command*: `pip install -r requirements.txt`
   - *Start Command*: `python main.py`
   - *Instance Type*: **Starter** (der Free-Plan schläft ein und hat keine Disk)
3. **Disks** → *Add Disk*: Name `fussball-data`, Mount Path `/var/data`, Grösse 1 GB.
4. **Environment** → folgende Variablen anlegen:

| Key | Wert |
|---|---|
| `FUSSBALL_STORAGE_DIR` | `/var/data` |
| `TELEGRAM_TOKEN` | Token von BotFather |
| `ODDS_API_KEY` | Key von the-odds-api.com |
| `APP_PASSWORD_HASH`, `APP_TOTP_SECRET`, `SESSION_SECRET` | siehe Schritt 3 |
| `TELEGRAM_OWNER_ID` | siehe Schritt 4 (zuerst leer lassen) |
| `ANTHROPIC_API_KEY` | für die KI-Agenten (console.anthropic.com), optional |
| `AGENT_DAILY_BUDGET_USD` | Tageslimit Agenten-Kosten, z. B. `1.5` |

5. **Manual Deploy** → *Deploy latest commit*.

## 3. Passwort für die Web-App
Lokal oder in der Render-**Shell**: `python -m fussball set-password` → die drei ausgegebenen
Zeilen als Umgebungsvariablen eintragen. Den 2FA-Schlüssel in Google Authenticator/1Password
hinzufügen.

## 4. Telegram mit dir verbinden
1. Deinem Bot in Telegram `/start` schreiben → er antwortet mit **deiner ID**.
2. Die Zahl als `TELEGRAM_OWNER_ID` bei Render eintragen → speichern (Render startet neu).
3. Der Bot meldet sich mit „✅ PeaceJudge gestartet“.

## Was du dann automatisch bekommst
| Wann | Nachricht |
|---|---|
| täglich 09:00 | Tipps des Tages + Kombis |
| neue/gestrichene Tipps | 🔔 Alarm |
| 60 Min. vor Anpfiff | ⏰ Erinnerung mit Mindestquote und `/gesetzt`-Befehl |
| nach Spielende | 🏁 Ergebnis deiner Wette, P/L, CLV, Bilanz |

Befehle: `/heute` `/kombi` `/spiel Team` `/bilanz` `/gesetzt Nr Einsatz Quote` `/update`

## Kosten
Render Starter ca. 7 $/Monat + Disk ca. 0.25 $/Monat. The Odds API Free (500 Credits/Monat,
1 Abruf pro Tag für 5 Ligen ≈ 300 Credits).
