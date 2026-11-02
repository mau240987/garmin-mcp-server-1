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
