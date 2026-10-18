from pathlib import Path

import pytest

from fussball.data.db import init_db, make_engine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def engine(tmp_path):
    """SQLite; mit TEST_DATABASE_URL (Postgres) läuft dieselbe Testreihe gegen Postgres."""
    import os

    from fussball.data.schema import Base

    url = os.getenv("TEST_DATABASE_URL")
    engine = make_engine(url or f"sqlite:///{tmp_path / 'test.db'}")
    if url:
        Base.metadata.drop_all(engine)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIXTURES / name).read_bytes()


@pytest.fixture
def bundesliga():
    return {"code": "D1", "name": "Bundesliga", "country": "Germany", "tier": 1, "api_football_id": 78}


@pytest.fixture(autouse=True)
def generous_agent_budget(monkeypatch):
    """Tests prüfen Logik, nicht das Budget – einzelne Tests setzen ein knappes Limit selbst."""
    monkeypatch.setenv("AGENT_DAILY_BUDGET_USD", "50")
    monkeypatch.setenv("RESULTS_RESERVE_USD", "0")
