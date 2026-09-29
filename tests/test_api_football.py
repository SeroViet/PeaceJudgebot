import pytest

from fussball.data.api_football import ApiFootballClient, ApiFootballError, QuotaExhausted


class FakeResponse:
    def __init__(self, status=200, payload=None, remaining="90"):
        self.status_code = status
        self._payload = payload if payload is not None else {"errors": [], "results": 0, "response": []}
        self.headers = {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def make_client(responses, **kw):
    sleeps = []
    clock = iter(range(0, 10_000, 1))
    client = ApiFootballClient(
        api_key=kw.pop("api_key", "test-key"),
        per_minute=kw.pop("per_minute", 10),
        daily_reserve=kw.pop("daily_reserve", 5),
        session=FakeSession(responses),
        sleep=sleeps.append,
        clock=lambda: float(next(clock)),
        **kw,
    )
    return client, sleeps


def test_sends_key_header_and_returns_payload():
    client, _ = make_client([FakeResponse(payload={"errors": [], "results": 1, "response": [{"id": 1}]})])
    assert client.get("fixtures", league=78, season=2024)["response"] == [{"id": 1}]
    url, params, headers = client.session.calls[0]
    assert url.endswith("/fixtures") and params == {"league": 78, "season": 2024}
    assert headers == {"x-apisports-key": "test-key"}
    assert client.remaining_today == 90


def test_without_key_no_header_is_sent():
    client, _ = make_client([FakeResponse()], api_key="")
    client.get("status")
    assert client.session.calls[0][2] == {}


def test_retries_on_429_then_succeeds():
    client, sleeps = make_client([FakeResponse(status=429), FakeResponse()])
    client.get("fixtures")
    assert len(client.session.calls) == 2
    assert 2 in sleeps  # exponentieller Backoff


def test_plan_error_raises():
    err = {"plan": "Free plans do not have access to this season, try from 2022 to 2024."}
    client, _ = make_client([FakeResponse(payload={"errors": err, "results": 0, "response": []})])
    with pytest.raises(ApiFootballError, match="Free plans"):
        client.get("fixtures", league=78, season=2026)


def test_stops_when_daily_reserve_reached():
    client, _ = make_client([FakeResponse(remaining="5")])
    client.get("fixtures")
    with pytest.raises(QuotaExhausted):
        client.get("fixtures")


def test_throttles_to_per_minute_limit():
    client, sleeps = make_client([FakeResponse(), FakeResponse()], per_minute=10)
    client.get("a")
    client.get("b")
    assert sleeps and sleeps[0] == pytest.approx(5.0)  # 6s Intervall, 1s vergangen


def test_get_all_follows_pages():
    pages = [
        FakeResponse(payload={"errors": [], "response": [1, 2], "paging": {"current": 1, "total": 2}}),
        FakeResponse(payload={"errors": [], "response": [3], "paging": {"current": 2, "total": 2}}),
    ]
    client, _ = make_client(pages)
    assert client.get_all("players", team=157, season=2024) == [1, 2, 3]
    assert client.session.calls[1][1]["page"] == 2
