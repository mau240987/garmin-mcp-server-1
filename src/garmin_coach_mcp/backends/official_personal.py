"""Official Garmin Connect **Personal API** backend (read-only, the user's own data).

Source of truth: the Personal API OpenAPI 1.0.0 spec (docs/api-coverage.md §1.1-§2).
Facts used here:
- token: POST /personalapitoken-service/personal-api/personal-api-key/token with header
  `Authorization: ApiKey <key>`; access token valid 24 h, no refresh token  [DOC]
- data calls are GETs on personal.connectapi.garmin.com (.cn for China)      [SPEC]
Verified with a real call on 2026-11-02 (scripts/verify_personal_api.py):
- the token response is a **plain-text JWT** (text/plain); JSON forms are still accepted
  defensively in case Garmin changes it;
- data calls authenticate with `Authorization: Bearer <token>` (a bare token gets HTTP 400).
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from garmin_coach_mcp.backends.base import BackendBase, Capability
from garmin_coach_mcp.client import error_for_status
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.errors import (
    AuthenticationError,
    ConfigurationError,
    GarminMCPError,
    UpstreamServiceError,
)
from garmin_coach_mcp.logging_utils import get_logger
from garmin_coach_mcp.retry import call_with_retry

log = get_logger("official_personal")

BASE_URLS = {
    "global": "https://personal.connectapi.garmin.com",
    "cn": "https://personal.connectapi.garmin.cn",
}
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"  # noqa: S105
DEFAULT_TOKEN_TTL_S = 24 * 3600
TOKEN_REFRESH_MARGIN_S = 300
KEY_EXPIRY_WARNING_DAYS = 14
TOKEN_FAILURE_COOLDOWN_S = 60.0
#: A token younger than this that is rejected with 401 is not renewed again: the key (or the
#: assumed Bearer scheme) is the problem, and renewing on every call would hammer Garmin.
MIN_TOKEN_AGE_FOR_RENEWAL_S = 60.0
_PLAIN_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,8192}$")

KEY_REMEDIATION = (
    "The Personal API key is wrong, revoked or expired, or does not allow this data. Create a "
    "new key in Garmin Connect Web (Personal API Key page) and update GARMIN_PERSONAL_API_KEY."
)
ACTIVITY_PAGE_SIZE = 100
MAX_ACTIVITY_PAGES = 50


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


#: (method, url, headers, timeout_s) -> response. Network failures raise OSError.
Transport = Callable[[str, str, Mapping[str, str], float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the Authorization header."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    req = urllib.request.Request(url, method=method, headers=dict(headers))  # noqa: S310
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers.items()))


def _jwt_ttl(token: str, now: Callable[[], float] = time.time) -> float | None:
    """Seconds until the JWT's `exp` claim, read WITHOUT verifying the signature.

    Only used to schedule renewal; Garmin validates the token on every call. Verified on a
    real call (2026-11-02): the token endpoint returns a plain-text JWT.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    ttl = float(exp) - now()
    return ttl if ttl > 0 else None


def _extract_token(body: bytes) -> tuple[str | None, float]:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        data: Any = json.loads(text) if text else None
    except ValueError:
        data = text  # plain-text token (format undocumented)
    if isinstance(data, str):
        # Only accept something that looks like a token, never e.g. an HTML page.
        candidate = data.strip()
        if not _PLAIN_TOKEN_RE.match(candidate):
            return None, DEFAULT_TOKEN_TTL_S
        return candidate, min(_jwt_ttl(candidate) or DEFAULT_TOKEN_TTL_S, DEFAULT_TOKEN_TTL_S)
    if isinstance(data, dict):
        token = next(
            (
                data[k]
                for k in ("access_token", "accessToken", "token")
                if isinstance(data.get(k), str) and _PLAIN_TOKEN_RE.match(data[k])
            ),
            None,
        )
        ttl = next(
            (
                float(data[k])
                for k in ("expires_in", "expiresIn")
                if isinstance(data.get(k), (int, float)) and data[k] > 0
            ),
            DEFAULT_TOKEN_TTL_S,
        )
        return token, min(ttl, DEFAULT_TOKEN_TTL_S)
    return None, DEFAULT_TOKEN_TTL_S


class PersonalApiClient:
    """Token lifecycle + GET with error mapping and bounded retries. Thread-safe."""

    def __init__(
        self,
        api_key: str,
        *,
        region: str = "global",
        max_retries: int = 2,
        timeout: float = 30.0,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "GARMIN_PERSONAL_API_KEY is empty.",
                remediation="Create a Personal API key in Garmin Connect Web and set it.",
            )
        self._key = api_key
        self._base = BASE_URLS[region]
        self._retries = max_retries
        self._timeout = timeout
        self._transport = transport or urllib_transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_issued = 0.0
        self._token_failure: tuple[float, GarminMCPError] | None = None

    def __repr__(self) -> str:  # never expose key/token
        return f"PersonalApiClient(base={self._base!r})"

    # -- token ------------------------------------------------------------------------
    def _send(self, method: str, url: str, headers: Mapping[str, str]) -> HttpResponse:
        try:
            resp = self._transport(method, url, headers, self._timeout)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise UpstreamServiceError(
                "Network error talking to the Garmin Personal API.",
                remediation="Check the network connection and retry.",
                cause=exc,
            ) from exc
        except ValueError:  # e.g. an invalid header value; never echo it
            err = UpstreamServiceError(
                "Could not build a valid request to the Garmin Personal API.",
                remediation="Check GARMIN_PERSONAL_API_KEY for stray characters.",
            )
            err.retryable = False
            raise err from None
        if 300 <= resp.status < 400:
            err = UpstreamServiceError(
                f"The Garmin Personal API answered with a redirect (HTTP {resp.status}).",
                remediation=(
                    "Redirects are not followed so credentials are never forwarded. Check "
                    "GARMIN_PERSONAL_API_REGION and run scripts/verify_personal_api.py."
                ),
            )
            err.retryable = False
            raise err
        return resp

    def _fetch_token(self) -> str:
        resp = self._send("POST", self._base + TOKEN_PATH, {"Authorization": f"ApiKey {self._key}"})
        if resp.status in (401, 403):
            raise AuthenticationError(
                f"Garmin rejected the Personal API key (HTTP {resp.status}).",
                remediation=KEY_REMEDIATION,
            )
        if not 200 <= resp.status < 300:
            raise error_for_status(resp.status, None, "Personal API token request")
        token, ttl = _extract_token(resp.body)
        if not token:
            err = UpstreamServiceError(
                "The Personal API token response had no recognizable access token.",
                remediation=(
                    "Run scripts/verify_personal_api.py and report the response keys; the "
                    "token format is not documented by Garmin."
                ),
            )
            err.retryable = False  # the same response would come back
            raise err
        now = self._clock()
        self._token_issued = now
        self._token_deadline = now + ttl - min(TOKEN_REFRESH_MARGIN_S, ttl / 2)
        return token

    def token(self, *, rejected: str | None = None) -> str:
        """Current access token. `rejected`: a token the API just refused with 401."""
        with self._lock:
            now = self._clock()
            if rejected is not None:
                if self._token and self._token != rejected:
                    return self._token  # another thread already renewed it
                if now - self._token_issued < MIN_TOKEN_AGE_FOR_RENEWAL_S:
                    err = AuthenticationError(
                        "Garmin rejected a freshly issued Personal API access token (HTTP 401).",
                        remediation=KEY_REMEDIATION,
                    )
                    err.during_login = True
                    self._token = None
                    self._token_failure = (now, err)
                    raise err
                self._token = None
            if self._token and now < self._token_deadline:
                return self._token
            if self._token_failure and now - self._token_failure[0] < TOKEN_FAILURE_COOLDOWN_S:
                raise self._token_failure[1]
            try:
                self._token = self._fetch_token()
            except AuthenticationError as exc:
                # Only a rejected key is cached; transient errors stay retryable.
                exc.during_login = True
                self._token = None
                self._token_failure = (now, exc)
                raise
            self._token_failure = None
            return self._token

    # -- data -------------------------------------------------------------------------
    def get(self, path: str, params: Mapping[str, Any] | None = None, *, operation: str) -> Any:
        """GET `path` and decode JSON. Returns None on 204. Idempotent: retried on 5xx."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = self._base + path + (f"?{urlencode(query)}" if query else "")

        def once() -> Any:
            token = self.token()
            resp = self._send("GET", url, self._headers(token))
            if resp.status == 401:  # token expired early or revoked: at most one renewal
                resp = self._send("GET", url, self._headers(self.token(rejected=token)))
            if resp.status in (401, 403):
                raise AuthenticationError(
                    f"Garmin refused {operation} with the Personal API token (HTTP {resp.status}).",
                    remediation=KEY_REMEDIATION,
                )
            if resp.status == 204:
                return None
            if resp.status >= 400:
                raise error_for_status(resp.status, None, operation)
            if not resp.body:
                return None
            try:
                return json.loads(resp.body)
            except ValueError as exc:
                raise UpstreamServiceError(
                    f"Garmin returned invalid JSON for {operation}.",
                    remediation="Retry later.",
                    cause=exc,
                ) from exc

        return call_with_retry(once, operation=operation, retries=self._retries)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class OfficialPersonalBackend(BackendBase):
    """Reads from the official Personal API. Declares only what the spec documents."""

    name = "official-personal"
    official = True

    _CAPABILITIES = frozenset(
        {
            Capability.ACTIVITIES_LIST,
            Capability.ACTIVITIES_BY_DATE,
            Capability.ACTIVITY_DETAILS,
            Capability.DAILY_SUMMARY,
            Capability.SLEEP,
            Capability.HRV,
        }
    )

    def __init__(
        self,
        client: PersonalApiClient,
        *,
        key_expires: dt.date | None = None,
        today: Callable[[], dt.date] = dt.date.today,
    ) -> None:
        self.client = client
        self.key_expires = key_expires
        self._today = today
        self._warned = False

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: Transport | None = None
    ) -> OfficialPersonalBackend:
        assert settings.personal_api_key
        client = PersonalApiClient(
            settings.personal_api_key,
            region=settings.personal_api_region,
            max_retries=settings.max_retries,
            transport=transport,
        )
        return cls(client, key_expires=settings.personal_api_key_expires)

    def capabilities(self) -> frozenset[Capability]:
        return self._CAPABILITIES

    def notices(self) -> list[str]:
        """Key-expiry notices to show the user (Garmin also emails ~1 week before)."""
        if self.key_expires is None:
            return []
        days = (self.key_expires - self._today()).days
        if days < 0:
            return [f"The Garmin Personal API key expired on {self.key_expires}."]
        if days <= KEY_EXPIRY_WARNING_DAYS:
            if not self._warned:
                log.warning("Garmin Personal API key expires in %d days", days)
                self._warned = True
            return [
                f"The Garmin Personal API key expires in {days} day(s) ({self.key_expires}): "
                "create a new one in Garmin Connect Web and update GARMIN_PERSONAL_API_KEY."
            ]
        return []

    def _check_key(self) -> None:
        if self.key_expires is not None and self.key_expires < self._today():
            raise AuthenticationError(
                f"The Garmin Personal API key expired on {self.key_expires}.",
                remediation=(
                    "Create a new key in Garmin Connect Web, then update "
                    "GARMIN_PERSONAL_API_KEY and GARMIN_PERSONAL_API_KEY_EXPIRES."
                ),
            )

    def _get(self, path: str, params: Mapping[str, Any] | None, operation: str) -> Any:
        self._check_key()
        return self.client.get(path, params, operation=operation)

    # Reads ---------------------------------------------------------------------------
    def list_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        return _list(
            self._get(
                "/activitylist-service/activities/search/activities",
                {"start": start, "limit": limit},
                "get_activities",
            )
        )

    def activities_between(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(MAX_ACTIVITY_PAGES):
            batch = _list(
                self._get(
                    "/activitylist-service/activities/search/activities",
                    {
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                        "start": page * ACTIVITY_PAGE_SIZE,
                        "limit": ACTIVITY_PAGE_SIZE,
                    },
                    "get_activities_by_date",
                )
            )
            out.extend(batch)
            if len(batch) < ACTIVITY_PAGE_SIZE:
                break
        return out

    def activity(self, activity_id: int) -> dict[str, Any]:
        return _dict(
            self._get(f"/activity-service/activity/{int(activity_id)}", None, "get_activity")
        )

    def daily_summary(self, day: dt.date) -> dict[str, Any]:
        return _dict(
            self._get(
                "/usersummary-service/usersummary/daily",
                {"calendarDate": day.isoformat()},
                "get_daily_summary",
            )
        )

    def sleep(self, day: dt.date) -> dict[str, Any]:
        raw = _dict(
            self._get("/sleep-service/sleep/dailySleep", {"date": day.isoformat()}, "get_sleep")
        )
        # Defensive: accept the wrapped form used by the internal endpoint too.
        return _dict(raw["dailySleepDTO"]) if isinstance(raw.get("dailySleepDTO"), dict) else raw

    def hrv(self, day: dt.date) -> dict[str, Any] | None:
        raw = self._get(f"/hrv-service/hrv/{day.isoformat()}", None, "get_hrv")
        return raw if isinstance(raw, dict) else None
