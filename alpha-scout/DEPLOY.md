# Alpha Scout online stellen (Render) – Schritt für Schritt

Gleich wie beim Fussball-Bot, aber als **eigener, neuer Dienst**. Den bestehenden Dienst
`pje-buybot` (Fussball) **nicht** ändern.

## 1. Schlüssel bereitlegen

| Wert | Woher |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys → „Create Key“ (Guthaben aufladen) |
| `TELEGRAM_BOT_TOKEN` | Telegram **@BotFather** → `/mybots` → @scout_aktien_krypto_bot → „API Token“ |
| `TELEGRAM_CHAT_ID` | Telegram **@userinfobot** anschreiben → zeigt deine ID (oder aus dem Bybit-Bot) |
| `SEC_EDGAR_USER_AGENT` | dein Name + E-Mail, z. B. `Max Muster max@example.ch` (Pflicht der SEC, kein Konto) |

Wichtig: Es muss der Token von **@scout_aktien_krypto_bot** sein, nicht der vom Fussball-Bot.

## 2. Neuen Dienst anlegen

1. https://dashboard.render.com → **New +** → **Background Worker**
2. Repository **SeroViet/PeaceJudgebot** wählen
3. Einstellungen:
   - *Name*: `alpha-scout`
   - *Branch*: `claude/fervent-maxwell-txbjx0` (oder `main`, nachdem du den PR gemergt hast)
   - *Root Directory*: `alpha-scout`
   - *Runtime / Language*: **Docker** (das Dockerfile wird automatisch gefunden)
   - *Instance Type*: **Starter** (Free schläft ein und hat keine Disk)
4. **Environment Variables**:

| Key | Wert |
|---|---|
| `ANTHROPIC_API_KEY` | aus Schritt 1 |
| `TELEGRAM_BOT_TOKEN` | aus Schritt 1 |
| `TELEGRAM_CHAT_ID` | aus Schritt 1 |
| `SEC_EDGAR_USER_AGENT` | aus Schritt 1 |
| `DAILY_LLM_BUDGET_USD` | `3` |
| `CONFIG_DIR` | `/app/data/config` |

5. **Advanced → Add Disk**: Name `alpha-scout-data`, Mount Path **`/app/data`**, Grösse 1 GB.
   (Dort liegen Datenbank, Watchlist und Bestand – sie bleiben bei Neustarts erhalten.)
6. **Create Background Worker**.

## 3. Prüfen

- Im Render-Log erscheint `Alpha Scout läuft`.
- In Telegram meldet sich der Bot mit „🟢 Alpha Scout gestartet“.
- `/status` schreiben → LLM „aktiv“, SEC User-Agent „gesetzt“.
- Nach ca. 1 Minute läuft die erste Analyse-Runde; danach zeigen `/top` und `/watchlist` Werte.

## 4. Bestand und Watchlist ändern

- Bestand per Telegram: `/buy MSFT 10 410 qualitaet Cloud-Marktführer`, `/sell MSFT 5 450`
- Watchlist/Regeln: In Render → Dienst → **Shell** öffnen und z. B. `nano /app/data/config/watchlist.yaml`
  bearbeiten, danach **Manual Deploy → Restart**. (Änderungen im Git-Ordner `config/` gelten nur für neue,
  noch nicht vorhandene Dateien auf der Disk.)

## Kosten

Render Starter ca. 7 $/Monat + Disk ca. 0.25 $/Monat. Claude-API ca. 0.40–0.80 $/Tag,
hart begrenzt auf `DAILY_LLM_BUDGET_USD`. Alle Datenquellen sind gratis.
