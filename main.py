"""Startpunkt für Render/Docker: Web-App + Telegram-Bot + Scheduler in einem Prozess.

Lokal:  python main.py        (Port aus $PORT, Standard 8000)
"""

import sys

from fussball.cli import main

if __name__ == "__main__":
    sys.exit(main(["serve"]))
