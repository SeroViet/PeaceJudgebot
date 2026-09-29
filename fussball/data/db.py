"""Engine, Sessions und ein dialektunabhängiger Upsert (SQLite und Postgres)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fussball.config import get_settings
from fussball.data.schema import Base


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine):
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert(
    session: Session,
    model,
    rows: Sequence[dict],
    conflict_cols: Iterable[str],
    update_cols: Iterable[str] | None = None,
    chunk_size: int = 500,
) -> int:
    """INSERT ... ON CONFLICT DO UPDATE. Idempotent: zweimal importieren ergibt
    denselben Datenbestand. Ohne `update_cols` werden alle Nicht-Schlüssel-
    Spalten der Zeilen aktualisiert."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise NotImplementedError(f"Upsert für Dialekt {dialect!r} nicht implementiert")

    conflict_cols = list(conflict_cols)
    if update_cols is None:
        update_cols = [c for c in rows[0] if c not in conflict_cols]
    update_cols = list(update_cols)

    for start in range(0, len(rows), chunk_size):
        stmt = insert(model).values(list(rows[start : start + chunk_size]))
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=conflict_cols,
                set_={c: stmt.excluded[c] for c in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
        session.execute(stmt)
    return len(rows)
