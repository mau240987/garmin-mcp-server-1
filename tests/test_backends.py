"""Official Personal backend, capability router and their integration with the tools.

Responses are synthetic and shaped after the Personal API OpenAPI 1.0.0 schemas
(docs/api-coverage.md). No network, no real key, no personal data.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import jsonschema
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from garmin_coach_mcp.backends import GARMIN_ATTRIBUTION, BackendRouter, Capability
from garmin_coach_mcp.backends.official_personal import (
    TOKEN_PATH,
    HttpResponse,
    OfficialPersonalBackend,
    PersonalApiClient,
)
from garmin_coach_mcp.backends.unofficial import UnofficialBackend
from garmin_coach_mcp.cli import build_router
from garmin_coach_mcp.client import GarminSession
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import (
    AuthenticationError,
    CapabilityUnavailableError,
    InvalidRequestError,
    NotFoundError,
    UpstreamServiceError,
)
from garmin_coach_mcp.server import build_server
from tests.conftest import FakeGarmin

KEY = "test-key-SENTINEL"
TOKEN = "tok-SENTINEL"
DAY = dt.date(2026, 10, 30)

OFFICIAL_ACTIVITIES = [
    {
        "activityId": 9001,
        "activityName": "Easy",
        "startTimeLocal": "2026-10-30 07:00:00",
        "activityType": {"typeId": 1, "typeKey": "running"},
        "distance": 8000.0,
        "duration": 2880.0,
        "averageHR": 138.0,
        "hrTimeInZone_2": 2500.0,
        "activityTrainingLoad": 80.0,
    }
]


def j(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})


class FakeTransport:
    """Routes (method, path) to responses; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]], dict[str, str]]] = []
        self.routes: dict[tuple[str, str], list[HttpResponse] | Callable[..., HttpResponse]] = {
            ("POST", TOKEN_PATH): [j({"access_token": TOKEN, "expires_in": 86400})],
            ("GET", "/activitylist-service/activities/search/activities"): [j(OFFICIAL_ACTIVITIES)],
            ("GET", "/usersummary-service/usersummary/daily"): [
                j({"totalSteps": 9000, "restingHeartRate": 47, "bodyBatteryHighestValue": 88})
            ],
            ("GET", "/sleep-service/sleep/dailySleep"): [
                j({"sleepTimeSeconds": 28800, "sleepScores": {"overall": {"value": 85}}})
            ],
            ("GET", f"/hrv-service/hrv/{DAY.isoformat()}"): [
                j({"hrvSummary": {"lastNightAvg": 61, "weeklyAvg": 60, "status": "BALANCED"}})
            ],
            ("GET", "/activity-service/activity/9001"): [
                j(
                    {
                        "activityId": 9001,
                        "activityTypeDTO": {"typeKey": "running"},
                        "summaryDTO": {
                            "startTimeLocal": "2026-10-30T07:00:00.0",
                            "distance": 8000.0,
                            "duration": 2880.0,
                        },
                    }
                )
            ],
        }

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r[0] == method and r[1] == path)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        parts = urlsplit(url)
        self.requests.append((method, parts.path, parse_qs(parts.query), dict(headers)))
        route = self.routes.get((method, parts.path))
        if route is None:
            return HttpResponse(404)
        if callable(route):
            return route(parse_qs(parts.query), dict(headers))
        return route[0] if len(route) == 1 else route.pop(0)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


@pytest.fixture
def official(transport: FakeTransport, clock: list[float]) -> OfficialPersonalBackend:
    client = PersonalApiClient(KEY, transport=transport, clock=lambda: clock[0])
    return OfficialPersonalBackend(client, today=lambda: DAY)


# ------------------------------------------------------------------ client / tokens


def test_token_flow_headers_and_reuse(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    official.daily_summary(DAY)
    official.sleep(DAY)
    official.list_activities(0, 5)
    assert transport.count("POST", TOKEN_PATH) == 1
    token_req = transport.requests[0]
    assert token_req[3]["Authorization"] == f"ApiKey {KEY}"
    for _m, _p, _q, headers in transport.requests[1:]:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert transport.requests[1][2] == {"calendarDate": ["2026-10-30"]}


def test_token_renewed_before_expiry(
    official: OfficialPersonalBackend, transport: FakeTransport, clock: list[float]
) -> None:
    official.daily_summary(DAY)
    clock[0] += 24 * 3600 - 299  # inside the 5-minute refresh margin
    official.daily_summary(DAY)
    assert transport.count("POST", TOKEN_PATH) == 2


def test_401_on_data_call_renews_an_old_token_once(
    official: OfficialPersonalBackend, transport: FakeTransport, clock: list[float]
) -> None:
    official.daily_summary(DAY)  # token issued
    clock[0] += 3600  # an hour later Garmin revokes it early
    transport.routes[("GET", "/usersummary-service/usersummary/daily")] = [
        HttpResponse(401),
        j({"totalSteps": 1}),
    ]
    assert official.daily_summary(DAY) == {"totalSteps": 1}
    assert transport.count("POST", TOKEN_PATH) == 2


def test_persistent_401_does_not_hammer_token_endpoint(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    transport.routes[("GET", "/usersummary-service/usersummary/daily")] = [HttpResponse(401)]
    for _ in range(5):
        with pytest.raises(AuthenticationError) as info:
            official.daily_summary(DAY)
        assert "GARMIN_PERSONAL_API_KEY" in str(info.value)
        assert "GARMIN_PASSWORD" not in str(info.value)
    assert transport.count("POST", TOKEN_PATH) == 1
    assert transport.count("GET", "/usersummary-service/usersummary/daily") == 1


def test_data_403_points_to_the_key(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    transport.routes[("GET", "/usersummary-service/usersummary/daily")] = [HttpResponse(403)]
    with pytest.raises(AuthenticationError) as info:
        official.daily_summary(DAY)
    assert "GARMIN_PERSONAL_API_KEY" in str(info.value)


@pytest.mark.parametrize("status", [301, 302, 307])
def test_redirects_are_errors_not_tokens_or_empty_data(
    official: OfficialPersonalBackend, transport: FakeTransport, status: int
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [HttpResponse(status, b"<html>moved</html>")]
    with pytest.raises(UpstreamServiceError, match="redirect"):
        official.daily_summary(DAY)
    assert transport.count("POST", TOKEN_PATH) == 1  # not retried

    transport.routes[("POST", TOKEN_PATH)] = [j({"access_token": TOKEN})]
    transport.routes[("GET", "/activitylist-service/activities/search/activities")] = [
        HttpResponse(status)
    ]
    with pytest.raises(UpstreamServiceError, match="redirect"):
        official.list_activities(0, 5)


def test_html_token_body_is_rejected(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [
        HttpResponse(200, b"<html><body>Sign in</body></html>", {"Content-Type": "text/html"})
    ]
    with pytest.raises(UpstreamServiceError, match="no recognizable access token"):
        official.daily_summary(DAY)
    assert not any(r[0] == "GET" for r in transport.requests)


def test_transient_token_failure_is_retried_and_not_cached(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [
        HttpResponse(503),
        j({"access_token": TOKEN}),
    ]
    assert official.daily_summary(DAY)["totalSteps"] == 9000
    assert transport.count("POST", TOKEN_PATH) == 2


def test_rejected_key_cooldown_expires(
    official: OfficialPersonalBackend, transport: FakeTransport, clock: list[float]
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [HttpResponse(401), j({"access_token": TOKEN})]
    with pytest.raises(AuthenticationError):
        official.daily_summary(DAY)
    clock[0] += 61
    assert official.daily_summary(DAY)["totalSteps"] == 9000


def test_short_expires_in_is_honoured(
    official: OfficialPersonalBackend, transport: FakeTransport, clock: list[float]
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [j({"access_token": TOKEN, "expires_in": 30})]
    official.daily_summary(DAY)
    clock[0] += 20  # past half of a 30 s lifetime
    official.daily_summary(DAY)
    assert transport.count("POST", TOKEN_PATH) == 2


def test_invalid_header_value_is_mapped_without_echo(official: OfficialPersonalBackend) -> None:
    def bad(*_a: Any) -> HttpResponse:
        raise ValueError("Invalid header value b'Bearer tok-SENTINEL'")

    official.client._transport = bad
    with pytest.raises(UpstreamServiceError) as info:
        official.daily_summary(DAY)
    assert "SENTINEL" not in str(info.value)


def test_rejected_key_fails_fast_and_never_leaks(
    official: OfficialPersonalBackend, transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [HttpResponse(401)]
    for _ in range(3):
        with pytest.raises(AuthenticationError) as info:
            official.daily_summary(DAY)
        assert KEY not in str(info.value) and "Personal API key" in str(info.value)
    assert transport.count("POST", TOKEN_PATH) == 1  # cooldown, no retries on login
    assert KEY not in caplog.text and TOKEN not in caplog.text
    assert KEY not in repr(official.client)


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        (b'{"access_token": "tok-SENTINEL"}', True),
        (b'{"accessToken": "tok-SENTINEL", "expiresIn": 3600}', True),
        (b'{"token": "tok-SENTINEL"}', True),
        (b"tok-SENTINEL", True),
        (b'"tok-SENTINEL"', True),
        (b'{"something": 1}', False),
        (b"", False),
    ],
)
def test_token_response_formats(
    official: OfficialPersonalBackend, transport: FakeTransport, body: bytes, ok: bool
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [HttpResponse(200, body)]
    if ok:
        official.daily_summary(DAY)
        assert transport.requests[-1][3]["Authorization"] == f"Bearer {TOKEN}"
    else:
        with pytest.raises(UpstreamServiceError, match="no recognizable access token"):
            official.daily_summary(DAY)


def test_transient_errors_retried_client_errors_not(
    official: OfficialPersonalBackend, transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "/usersummary-service/usersummary/daily"
    transport.routes[("GET", path)] = [HttpResponse(503), HttpResponse(502), j({"totalSteps": 3})]
    assert official.daily_summary(DAY) == {"totalSteps": 3}
    assert transport.count("GET", path) == 3

    transport.routes[("GET", path)] = [HttpResponse(400)]
    with pytest.raises(InvalidRequestError):
        official.daily_summary(DAY)
    assert transport.count("GET", path) == 4

    with pytest.raises(NotFoundError):
        official.activity(12345)


def test_network_error_is_mapped(official: OfficialPersonalBackend) -> None:
    def boom(*_a: Any) -> HttpResponse:
        raise OSError("unreachable")

    official.client._transport = boom
    with pytest.raises(UpstreamServiceError):
        official.daily_summary(DAY)


def test_hrv_204_is_none(official: OfficialPersonalBackend, transport: FakeTransport) -> None:
    transport.routes[("GET", f"/hrv-service/hrv/{DAY.isoformat()}")] = [HttpResponse(204)]
    assert official.hrv(DAY) is None


def test_sleep_accepts_wrapped_shape(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    transport.routes[("GET", "/sleep-service/sleep/dailySleep")] = [
        j({"dailySleepDTO": {"sleepTimeSeconds": 1}})
    ]
    assert official.sleep(DAY) == {"sleepTimeSeconds": 1}


def test_activities_between_paginates(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    def page(query: dict[str, list[str]], _h: dict[str, str]) -> HttpResponse:
        start = int(query["start"][0])
        n = 100 if start == 0 else 5
        return j([{"activityId": start + i} for i in range(n)])

    transport.routes[("GET", "/activitylist-service/activities/search/activities")] = page
    acts = official.activities_between(dt.date(2026, 8, 1), DAY)
    assert len(acts) == 105
    queries = [r[2] for r in transport.requests if r[0] == "GET"]
    assert queries[0]["startDate"] == ["2026-08-01"] and queries[0]["endDate"] == ["2026-10-30"]
    assert [q["start"] for q in queries] == [["0"], ["100"]]


def test_expired_key_fails_before_network(transport: FakeTransport) -> None:
    client = PersonalApiClient(KEY, transport=transport)
    backend = OfficialPersonalBackend(
        client, key_expires=DAY - dt.timedelta(days=1), today=lambda: DAY
    )
    with pytest.raises(AuthenticationError, match="expired"):
        backend.daily_summary(DAY)
    assert transport.requests == []
    assert "expired" in backend.notices()[0]


def test_expiry_notice_window(transport: FakeTransport) -> None:
    client = PersonalApiClient(KEY, transport=transport)
    soon = OfficialPersonalBackend(
        client, key_expires=DAY + dt.timedelta(days=10), today=lambda: DAY
    )
    later = OfficialPersonalBackend(
        client, key_expires=DAY + dt.timedelta(days=60), today=lambda: DAY
    )
    assert "10 day(s)" in soon.notices()[0]
    assert later.notices() == []


def test_official_declares_only_documented_capabilities() -> None:
    caps = OfficialPersonalBackend(PersonalApiClient(KEY, transport=FakeTransport())).capabilities()
    assert Capability.TRAINING_READINESS not in caps
    assert Capability.WORKOUTS_LIST not in caps
    assert not caps & {
        Capability.WORKOUT_CREATE,
        Capability.WORKOUT_SCHEDULE,
        Capability.WORKOUT_DELETE,
    }


def test_undeclared_method_raises_structured_error(official: OfficialPersonalBackend) -> None:
    with pytest.raises(CapabilityUnavailableError):
        official.training_readiness(DAY)


# ------------------------------------------------------------------------ router


@pytest.fixture
def unofficial(make_session: Callable[..., GarminSession]) -> UnofficialBackend:
    return UnofficialBackend(make_session())


def test_router_prefers_official_for_reads(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend
) -> None:
    router = BackendRouter([unofficial, official])  # order must not matter
    for cap in (Capability.ACTIVITIES_LIST, Capability.DAILY_SUMMARY, Capability.HRV):
        assert router.select(cap) is official


def test_router_unofficial_reads_are_opt_in(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend
) -> None:
    strict = BackendRouter([official, unofficial])
    with pytest.raises(CapabilityUnavailableError) as info:
        strict.select(Capability.TRAINING_READINESS)
    assert "set GARMIN_ALLOW_UNOFFICIAL_READS=true (unofficial" in str(info.value)
    with pytest.raises(CapabilityUnavailableError) as only:
        BackendRouter([official]).select(Capability.TRAINING_READINESS)
    assert "configure the unofficial backend" in str(only.value)
    relaxed = BackendRouter([official, unofficial], allow_unofficial_reads=True)
    assert relaxed.select(Capability.TRAINING_READINESS) is unofficial
    assert relaxed.select(Capability.DAILY_SUMMARY) is official


def test_router_writes_need_a_writer(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend
) -> None:
    assert BackendRouter([official, unofficial]).select(Capability.WORKOUT_CREATE) is unofficial
    with pytest.raises(CapabilityUnavailableError) as info:
        BackendRouter([official]).select(Capability.WORKOUT_CREATE)
    assert "Training API" in str(info.value)


def test_router_unofficial_only_keeps_legacy_behaviour(unofficial: UnofficialBackend) -> None:
    router = BackendRouter([unofficial])
    assert all(router.select(cap) is unofficial for cap in Capability)


def test_build_router_from_settings(tmp_path: Path) -> None:
    def names(**env: str) -> list[str]:
        base = {"GARMIN_TOKEN_DIR": str(tmp_path / "t")}
        return [b.name for b in build_router(Settings.from_env({**base, **env})).backends]

    assert names() == ["unofficial"]
    assert names(GARMIN_PERSONAL_API_KEY="k") == ["official-personal"]
    assert names(GARMIN_PERSONAL_API_KEY="k", GARMIN_EMAIL="a@b.invalid", GARMIN_PASSWORD="p") == [
        "official-personal",
        "unofficial",
    ]
    assert "SENTINEL" not in repr(Settings.from_env({"GARMIN_PERSONAL_API_KEY": "k-SENTINEL"}))


# ---------------------------------------------------------------- tool integration


def call(app: MCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        tools = {t.name: t for t in await app.list_tools()}
        result = await app.call_tool(name, args)
        jsonschema.validate(result.structured_content, tools[name].output_schema)
        return dict(result.structured_content)

    return anyio.run(go)


def test_reads_go_official_and_carry_attribution(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend, fake: FakeGarmin
) -> None:
    app = build_server(BackendRouter([official, unofficial]), sleep=lambda _s: None)
    out = call(app, "get_activities", {"limit": 5})
    assert out["activities"][0]["activity_id"] == 9001
    assert out["sources"] == [
        {"backend": "official-personal", "official": True, "attribution": GARMIN_ATTRIBUTION}
    ]
    assert GARMIN_ATTRIBUTION in out["text"]
    details = call(app, "get_activity_details", {"activity_id": "9001"})
    assert details["summary"]["pace"] == "6:00/km"
    assert fake.calls == []  # unofficial backend never touched, not even a login


def test_health_summary_official_with_readiness_unavailable(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend, fake: FakeGarmin
) -> None:
    app = build_server(BackendRouter([official, unofficial]))
    out = call(app, "get_health_summary", {"date_str": DAY.isoformat()})
    assert out["steps"] == 9000 and out["sleep_hours"] == 8.0 and out["hrv_last_night_avg"] == 61
    assert out["training_readiness"] is None
    assert "Training Readiness is not offered" in out["unavailable"]["training_readiness"]
    assert [s["backend"] for s in out["sources"]] == ["official-personal"]
    assert fake.calls == []


def test_health_summary_readiness_via_opt_in_fallback(
    official: OfficialPersonalBackend, unofficial: UnofficialBackend
) -> None:
    app = build_server(BackendRouter([official, unofficial], allow_unofficial_reads=True))
    out = call(app, "get_health_summary", {"date_str": DAY.isoformat()})
    assert out["training_readiness"] == 72 and out["unavailable"] == {}
    assert {s["backend"] for s in out["sources"]} == {"official-personal", "unofficial"}


def test_training_status_uses_official_date_range(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    app = build_server(BackendRouter([official]))
    out = call(app, "get_training_status", {"days": 7})
    assert out["running_sessions"] == 1 and out["total_distance_km"] == 8.0
    query = transport.requests[-1][2]
    assert "startDate" in query and "endDate" in query


def test_official_only_writes_fail_with_guidance(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    app = build_server(BackendRouter([official]), sleep=lambda _s: None)
    steps = [{"type": "run", "duration_value": 600}]
    for name, args in [
        ("push_workout", {"name": "x", "steps": steps}),
        (
            "push_training_plan",
            {
                "name": "P",
                "start_date": "2026-11-02",
                "workouts": [{"day_offset": 0, "name": "a", "steps": steps}],
            },
        ),
        ("delete_workout", {"workout_id": "1"}),
        ("delete_plan_workouts", {"name_contains": "plan"}),
        ("get_workouts", {}),
    ]:
        with pytest.raises(ToolError) as info:
            anyio.run(app.call_tool, name, args)
        assert "CapabilityUnavailableError" in str(info.value), name
    assert not any(r[0] != "GET" and r[1] != TOKEN_PATH for r in transport.requests)


def test_key_expiry_notice_reaches_tool_output(transport: FakeTransport) -> None:
    client = PersonalApiClient(KEY, transport=transport)
    backend = OfficialPersonalBackend(
        client, key_expires=DAY + dt.timedelta(days=3), today=lambda: DAY
    )
    app = build_server(BackendRouter([backend], notices=backend.notices))
    out = call(app, "get_activities", {})
    assert out["notices"] and "3 day(s)" in out["notices"][0]
    assert "Notice:" in out["text"]


def test_health_summary_official_total_failure_is_tool_error(
    official: OfficialPersonalBackend, transport: FakeTransport
) -> None:
    for path in (
        "/usersummary-service/usersummary/daily",
        "/sleep-service/sleep/dailySleep",
        f"/hrv-service/hrv/{DAY.isoformat()}",
    ):
        transport.routes[("GET", path)] = [HttpResponse(500)]
    app = build_server(BackendRouter([official]))
    with pytest.raises(ToolError):
        anyio.run(app.call_tool, "get_health_summary", {"date_str": DAY.isoformat()})


def test_urllib_transport_does_not_follow_redirects() -> None:
    """Real urllib transport against a local server: redirect returned, header not forwarded."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from garmin_coach_mcp.backends.official_personal import urllib_transport

    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *_a: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        resp = urllib_transport("GET", url, {"Authorization": "Bearer x"}, 5)
    finally:
        server.shutdown()
    assert resp.status == 302
    assert seen == ["Bearer x"]  # only the first request; the redirect was not followed


def _jwt(claims: dict[str, Any]) -> bytes:
    import base64

    def seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.c2lnbmF0dXJl".encode()


def test_real_token_format_plain_text_jwt_with_exp(
    official: OfficialPersonalBackend, transport: FakeTransport, clock: list[float]
) -> None:
    """Shape observed on the real API: text/plain JWT. Renewal follows its `exp` claim."""
    import time as _time

    body = _jwt({"exp": int(_time.time()) + 3600})
    transport.routes[("POST", TOKEN_PATH)] = [
        HttpResponse(200, body, {"Content-Type": "text/plain"})
    ]
    official.daily_summary(DAY)
    sent = transport.requests[-1][3]["Authorization"]
    assert sent == f"Bearer {body.decode()}"
    clock[0] += 3600 - 299  # within 5 min of the JWT's real expiry, well before 24 h
    official.daily_summary(DAY)
    assert transport.count("POST", TOKEN_PATH) == 2


@pytest.mark.parametrize(
    "claims",
    [{}, {"exp": "soon"}, {"exp": True}, {"exp": 1}],  # missing, wrong type, past
)
def test_jwt_without_usable_exp_falls_back_to_24h(
    official: OfficialPersonalBackend,
    transport: FakeTransport,
    clock: list[float],
    claims: dict[str, Any],
) -> None:
    transport.routes[("POST", TOKEN_PATH)] = [HttpResponse(200, _jwt(claims))]
    official.daily_summary(DAY)
    clock[0] += 3600 * 12
    official.daily_summary(DAY)
    assert transport.count("POST", TOKEN_PATH) == 1
