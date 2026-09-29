"""Zentrale Einstellungen. Werte kommen aus Umgebungsvariablen bzw. `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str
    storage_dir: Path
    api_football_key: str | None
    api_football_per_minute: int
    api_football_daily_reserve: int


def get_settings() -> Settings:
    storage_dir = Path(os.getenv("FUSSBALL_STORAGE_DIR", PROJECT_ROOT / "storage"))
    return Settings(
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{storage_dir / 'fussball.db'}"),
        storage_dir=storage_dir,
        # Optional: In der Claude-Cloud-Umgebung wird der Header per Proxy injiziert.
        api_football_key=os.getenv("API_FOOTBALL_KEY") or None,
        api_football_per_minute=int(os.getenv("API_FOOTBALL_PER_MINUTE", "10")),
        api_football_daily_reserve=int(os.getenv("API_FOOTBALL_DAILY_RESERVE", "5")),
    )


def load_leagues(path: Path | None = None) -> dict[str, dict]:
    """Liga-Konfiguration, indexiert nach football-data.co.uk-Code (z. B. 'D1')."""
    with open(path or CONFIG_DIR / "leagues.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {code: {"code": code, **cfg} for code, cfg in data["leagues"].items()}
