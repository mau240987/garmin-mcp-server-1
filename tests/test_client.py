from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from garmin_coach_mcp.client import GarminSession, map_exception
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import (
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    UpstreamServiceError,
)
from tests.conftest import FakeGarmin


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GarminConnectAuthenticationError("401"), AuthenticationError),
        (GarminConnectTooManyRequestsError("429"), RateLimitError),
        (GarminConnectNotFoundError("Activity client error (404)"), NotFoundError),
        (GarminConnectConnectionError("Upload client error (400): bad"), InvalidRequestError),
        # Real messages from garminconnect's transport for POST/DELETE (not wrapped by
        # _handle_api_errors): "API Error <status> - ...".
        (GarminConnectConnectionError("API Error 400 - bad workout"), InvalidRequestError),
        (GarminConnectConnectionError("API Error 429 - slow down"), RateLimitError),
        (GarminConnectConnectionError("API Error 401 - expired"), AuthenticationError),
        (GarminConnectConnectionError("API Error 404 - gone"), NotFoundError),
        (GarminConnectConnectionError("API Error 502 - bad gateway"), UpstreamServiceError),
        (GarminConnectConnectionError("Server error 503"), UpstreamServiceError),
        (ValueError("bad date"), InvalidRequestError),
        (TimeoutError(), UpstreamServiceError),
        (RuntimeError("boom"), UpstreamServiceError),
    ],
)
def test_map_exception(exc: BaseException, expected: type) -> None:
    mapped = map_exception(exc, "op")
    assert isinstance(mapped, expected)
    assert mapped.remediation
    assert mapped.__cause__ is exc


def test_no_credentials_no_tokens(settings: Settings) -> None:
    s = Settings(token_dir=settings.token_dir)
    with pytest.raises(ConfigurationError):
        GarminSession(s, factory=lambda _s: FakeGarmin()).api()


def test_login_is_lazy_and_cached(
    make_session: Callable[..., GarminSession], fake: FakeGarmin
) -> None:
    session = make_session()
    assert fake.calls == []
    session.call("x", lambda c: c.get_workouts())
    session.call("x", lambda c: c.get_workouts())
    assert [c[0] for c in fake.calls].count("login") == 1
    assert session.settings.token_dir.is_dir()


def test_login_failure_is_mapped(
    make_session: Callable[..., GarminSession], fake: FakeGarmin
) -> None:
    fake.fail["login"] = GarminConnectAuthenticationError("bad creds")
    with pytest.raises(AuthenticationError):
        make_session().api()


def _flaky(times: int, exc: BaseException) -> Callable[[Any], str]:
    state = {"n": 0}

    def fn(_c: Any) -> str:
        state["n"] += 1
        if state["n"] <= times:
            raise exc
        return "ok"

    fn.state = state  # type: ignore[attr-defined]
    return fn


def test_reads_retry_transient(make_session: Callable[..., GarminSession]) -> None:
    fn = _flaky(2, GarminConnectConnectionError("503"))
    assert make_session().call("read", fn) == "ok"
    assert fn.state["n"] == 3  # type: ignore[attr-defined]


def test_reads_give_up_after_max_retries(make_session: Callable[..., GarminSession]) -> None:
    fn = _flaky(10, GarminConnectConnectionError("503"))
    with pytest.raises(UpstreamServiceError):
        make_session(max_retries=1).call("read", fn)
    assert fn.state["n"] == 2  # type: ignore[attr-defined]


def test_writes_never_retried(make_session: Callable[..., GarminSession]) -> None:
    fn = _flaky(1, GarminConnectConnectionError("503"))
    with pytest.raises(UpstreamServiceError):
        make_session().call("create", fn, idempotent=False)
    assert fn.state["n"] == 1  # type: ignore[attr-defined]


def test_non_retryable_not_retried(make_session: Callable[..., GarminSession]) -> None:
    fn = _flaky(5, GarminConnectNotFoundError("404"))
    with pytest.raises(NotFoundError):
        make_session().call("read", fn)
    assert fn.state["n"] == 1  # type: ignore[attr-defined]


def test_auth_error_reauthenticates_once(
    make_session: Callable[..., GarminSession], fake: FakeGarmin
) -> None:
    fn = _flaky(1, GarminConnectAuthenticationError("expired"))
    assert make_session().call("read", fn) == "ok"
    assert [c[0] for c in fake.calls].count("login") == 2


def test_status_taken_from_response_attribute() -> None:
    exc = GarminConnectConnectionError("opaque")
    exc.response = type("R", (), {"status_code": 422})()  # type: ignore[attr-defined]
    assert isinstance(map_exception(exc, "op"), InvalidRequestError)


def test_failed_login_is_not_retried_and_is_cached(
    make_session: Callable[..., GarminSession], fake: FakeGarmin
) -> None:
    fake.fail["login"] = GarminConnectAuthenticationError("bad creds")
    session = make_session()
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            session.call("read", lambda c: c.get_workouts())
    assert [c[0] for c in fake.calls].count("login") == 1


def test_login_network_error_not_retried(
    make_session: Callable[..., GarminSession], fake: FakeGarmin
) -> None:
    fake.fail["login"] = GarminConnectConnectionError("API Error 503 - down")
    with pytest.raises(UpstreamServiceError):
        make_session().call("read", lambda c: c.get_workouts())
    assert [c[0] for c in fake.calls].count("login") == 1


def test_login_retried_after_cooldown(
    make_session: Callable[..., GarminSession], fake: FakeGarmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    import garmin_coach_mcp.client as client_mod

    fake.fail["login"] = GarminConnectAuthenticationError("bad creds")
    session = make_session()
    with pytest.raises(AuthenticationError):
        session.api()
    del fake.fail["login"]
    monkeypatch.setattr(client_mod, "LOGIN_FAILURE_COOLDOWN_S", 0.0)
    session.api()
    assert [c[0] for c in fake.calls].count("login") == 2


def test_unwritable_token_dir(
    make_session: Callable[..., GarminSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import garmin_coach_mcp.client as client_mod

    monkeypatch.setattr(client_mod.os, "access", lambda *_a, **_k: False)
    with pytest.raises(ConfigurationError) as info:
        make_session().api()
    assert "not writable" in info.value.message


def test_token_dir_creation_failure_is_mapped(settings: Settings, tmp_path: Any) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = Settings(email="a@b.invalid", password="p", token_dir=blocker / "tokens")
    with pytest.raises(ConfigurationError):
        GarminSession(s, factory=lambda _s: FakeGarmin()).api()
