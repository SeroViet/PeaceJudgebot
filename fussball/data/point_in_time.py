"""Point-in-Time-Abfragen: nur Daten, die zu einem Zeitpunkt bereits bekannt waren.

Jede Feature- oder Backtest-Abfrage läuft über diese Helfer, damit kein
Wissen aus der Zukunft (Data Leakage) ins Modell gelangt.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from fussball.data.schema import Match, Odds


def known_as_of(stmt: Select, model, as_of: datetime) -> Select:
    """Filtert eine Abfrage auf Zeilen mit known_at <= as_of."""
    return stmt.where(model.known_at <= as_of)


def results_as_of(as_of: datetime) -> Select:
    """Alle Spiele, deren Ergebnis zum Zeitpunkt as_of bereits feststand."""
    return select(Match).where(
        Match.status == "finished", Match.known_at.is_not(None), Match.known_at <= as_of
    )


def latest_odds_as_of(session: Session, match_id: int, as_of: datetime) -> list[Odds]:
    """Jeweils die jüngste bekannte Quote je (Buchmacher, Markt, Linie, Tipp)."""
    key = (Odds.bookmaker, Odds.market, Odds.line, Odds.selection)
    latest = (
        select(*key, func.max(Odds.known_at).label("known_at"))
        .where(Odds.match_id == match_id, Odds.known_at <= as_of)
        .group_by(*key)
        .subquery()
    )
    stmt = select(Odds).join(
        latest,
        and_(
            Odds.bookmaker == latest.c.bookmaker,
            Odds.market == latest.c.market,
            Odds.line == latest.c.line,
            Odds.selection == latest.c.selection,
            Odds.known_at == latest.c.known_at,
        ),
    ).where(Odds.match_id == match_id)
    return list(session.scalars(stmt))


def team_matches_before(team_id: int, as_of: datetime) -> Select:
    """Abgeschlossene Spiele eines Teams, deren Ergebnis vor as_of bekannt war."""
    return results_as_of(as_of).where(
        or_(Match.home_team_id == team_id, Match.away_team_id == team_id)
    ).order_by(Match.kickoff_utc)
