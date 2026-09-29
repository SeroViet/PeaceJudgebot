"""Datenbankschema (SQLAlchemy 2.0). Läuft auf SQLite und ist Postgres-fähig.

Konventionen:
- Alle Zeitstempel sind naive UTC-Datetimes.
- `known_at` ("bekannt ab") ist der früheste Zeitpunkt, zu dem die Information
  öffentlich verfügbar war. Backtests dürfen nur Zeilen mit
  `known_at <= Prognosezeitpunkt` verwenden (siehe `point_in_time.py`).
- `source` nennt die Datenquelle, damit Widersprüche nachvollziehbar bleiben.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Competition(TimestampMixin, Base):
    __tablename__ = "competitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # z. B. "D1", "UNL"
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="league")  # league | cup | international
    tier: Mapped[int | None] = mapped_column(Integer)
    api_football_id: Mapped[int | None] = mapped_column(Integer, unique=True)


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    country: Mapped[str | None] = mapped_column(String(64))
    is_national: Mapped[bool] = mapped_column(Boolean, default=False)
    api_football_id: Mapped[int | None] = mapped_column(Integer, unique=True)


class TeamAlias(Base):
    """Namensvarianten je Quelle ("M'gladbach" bei football-data usw.)."""

    __tablename__ = "team_aliases"
    __table_args__ = (UniqueConstraint("source", "alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    source: Mapped[str] = mapped_column(String(32))
    alias: Mapped[str] = mapped_column(String(128))


class Match(TimestampMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("competition_id", "season", "home_team_id", "away_team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"), index=True)
    season: Mapped[str] = mapped_column(String(9))  # "2024-25"
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    kickoff_time_known: Mapped[bool] = mapped_column(Boolean, default=True)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    neutral_venue: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled | finished
    referee: Mapped[str | None] = mapped_column(String(64))

    ft_home: Mapped[int | None] = mapped_column(Integer)
    ft_away: Mapped[int | None] = mapped_column(Integer)
    ht_home: Mapped[int | None] = mapped_column(Integer)
    ht_away: Mapped[int | None] = mapped_column(Integer)
    xg_home: Mapped[float | None] = mapped_column(Float)
    xg_away: Mapped[float | None] = mapped_column(Float)
    shots_home: Mapped[int | None] = mapped_column(Integer)
    shots_away: Mapped[int | None] = mapped_column(Integer)
    shots_on_target_home: Mapped[int | None] = mapped_column(Integer)
    shots_on_target_away: Mapped[int | None] = mapped_column(Integer)
    fouls_home: Mapped[int | None] = mapped_column(Integer)
    fouls_away: Mapped[int | None] = mapped_column(Integer)
    corners_home: Mapped[int | None] = mapped_column(Integer)
    corners_away: Mapped[int | None] = mapped_column(Integer)
    yellow_home: Mapped[int | None] = mapped_column(Integer)
    yellow_away: Mapped[int | None] = mapped_column(Integer)
    red_home: Mapped[int | None] = mapped_column(Integer)
    red_away: Mapped[int | None] = mapped_column(Integer)

    api_football_fixture_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    # Bezieht sich auf Ergebnis und Spielstatistik, nicht auf die Ansetzung.
    known_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32))


class Player(TimestampMixin, Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    birth_date: Mapped[date | None] = mapped_column(Date)
    nationality: Mapped[str | None] = mapped_column(String(64))
    position: Mapped[str | None] = mapped_column(String(16))  # G | D | M | F
    api_football_id: Mapped[int | None] = mapped_column(Integer, unique=True)


class Lineup(Base):
    __tablename__ = "lineups"
    __table_args__ = (UniqueConstraint("match_id", "player_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    is_starter: Mapped[bool] = mapped_column(Boolean)
    position: Mapped[str | None] = mapped_column(String(16))
    grid: Mapped[str | None] = mapped_column(String(8))  # z. B. "4:2" bei API-Football
    shirt_number: Mapped[int | None] = mapped_column(Integer)
    is_predicted: Mapped[bool] = mapped_column(Boolean, default=False)  # erwartete vs. offizielle Elf
    known_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32))


class PlayerMatchStat(Base):
    __tablename__ = "player_match_stats"
    __table_args__ = (UniqueConstraint("match_id", "player_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    minutes: Mapped[int | None] = mapped_column(Integer)
    goals: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    xg: Mapped[float | None] = mapped_column(Float)
    xa: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[int | None] = mapped_column(Integer)
    key_passes: Mapped[int | None] = mapped_column(Integer)
    tackles: Mapped[int | None] = mapped_column(Integer)
    interceptions: Mapped[int | None] = mapped_column(Integer)
    duels_won: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    goals_conceded: Mapped[int | None] = mapped_column(Integer)
    psxg: Mapped[float | None] = mapped_column(Float)  # Post-Shot xG gegen (Torhüter)
    rating: Mapped[float | None] = mapped_column(Float)
    known_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32))


class Injury(Base):
    """Eine Meldung zu einem Ausfall. Widersprüchliche Meldungen bleiben als
    eigene Zeilen erhalten; `probability_out` fasst die Unsicherheit zusammen."""

    __tablename__ = "injuries"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    kind: Mapped[str] = mapped_column(String(32))  # injury | illness | not_selected | rotation | personal | other
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))  # doubtful | out | returned_not_fit
    start_date: Mapped[date | None] = mapped_column(Date)
    expected_return: Mapped[date | None] = mapped_column(Date)
    probability_out: Mapped[float | None] = mapped_column(Float)
    credibility: Mapped[float | None] = mapped_column(Float)
    known_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (UniqueConstraint("match_id", "player_id", "card_type", "minute"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    card_type: Mapped[str] = mapped_column(String(16))  # yellow | second_yellow | red
    minute: Mapped[int | None] = mapped_column(Integer)
    known_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(32))


class Suspension(Base):
    __tablename__ = "suspensions"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    competition_id: Mapped[int] = mapped_column(ForeignKey("competitions.id"))
    reason: Mapped[str] = mapped_column(String(24))  # red | second_yellow | yellow_accumulation | other
    matches_banned: Mapped[int] = mapped_column(Integer)
    triggered_by_match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"))
    served_matches: Mapped[int] = mapped_column(Integer, default=0)
    rule_reference: Mapped[str | None] = mapped_column(Text)  # Quelle der Sperrregel
    known_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(64))


class Odds(Base):
    __tablename__ = "odds"
    __table_args__ = (
        UniqueConstraint("match_id", "bookmaker", "market", "line", "selection", "is_closing", "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    bookmaker: Mapped[str] = mapped_column(String(32))  # Kürzel, z. B. "B365", "PS", "Avg"
    market: Mapped[str] = mapped_column(String(8))  # 1X2 | OU | AH
    line: Mapped[float] = mapped_column(Float, default=0.0)  # 2.5 bei OU, Handicap (Heim) bei AH
    selection: Mapped[str] = mapped_column(String(4))  # H D A | O U
    price: Mapped[float] = mapped_column(Float)
    is_closing: Mapped[bool] = mapped_column(Boolean, default=False)
    known_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(32))
    run_type: Mapped[str] = mapped_column(String(16))  # T-24h | T-60m | backtest
    market: Mapped[str] = mapped_column(String(8))
    line: Mapped[float] = mapped_column(Float, default=0.0)
    selection: Mapped[str] = mapped_column(String(4))
    probability: Mapped[float] = mapped_column(Float)
    fair_odds: Mapped[float | None] = mapped_column(Float)
    expected_goals_home: Mapped[float | None] = mapped_column(Float)
    expected_goals_away: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict | None] = mapped_column(JSON)
    explanation: Mapped[dict | None] = mapped_column(JSON)  # SHAP-Werte, Top-Faktoren
    known_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    bet_type: Mapped[str] = mapped_column(String(16), default="single")  # single | combo | system
    combo_group: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(8))
    line: Mapped[float] = mapped_column(Float, default=0.0)
    selection: Mapped[str] = mapped_column(String(4))
    odds_taken: Mapped[float] = mapped_column(Float)
    stake: Mapped[float] = mapped_column(Float)
    bookmaker: Mapped[str | None] = mapped_column(String(32))
    placed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | won | lost | void | half_won | half_lost
    pnl: Mapped[float | None] = mapped_column(Float)
    closing_odds: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)  # odds_taken / closing_fair_odds - 1


class PostMortem(Base):
    __tablename__ = "post_mortems"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    bet_id: Mapped[int | None] = mapped_column(ForeignKey("bets.id"))
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    category: Mapped[str] = mapped_column(String(16))  # bad_luck | model_error | missing_info
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IngestLog(Base):
    __tablename__ = "ingest_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    resource: Mapped[str] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | error
    rows: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


class AppSetting(Base):
    """Vom Benutzer in der App geänderte Einstellungen (überschreiben config/*.yaml)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | float | str | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
