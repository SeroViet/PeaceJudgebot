from pathlib import Path

import pytest

from fussball.data.db import init_db, make_engine

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    return engine


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
