"""Thin, safety-oriented wrapper around the Semgrep public API.

Only ``https://semgrep.dev`` is ever contacted. Endpoint shapes follow the
published OpenAPI documents:

* v1: https://semgrep.dev/api/v1/public_v1.openapi.yaml
* v2: https://semgrep.dev/api/v2/openapi.yaml
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

import httpx

from .logging_utils import redact

BASE_URL = "https://semgrep.dev"
TOKEN_ENV = "SEMGREP_APP_TOKEN"

log = logging.getLogger("sms_reconcile.client")

# Injectable so tests can run without real sleeping.
_sleep: Callable[[float], None] = time.sleep

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    """Raised when the API returns an error we do not retry, or retries are exhausted."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetriesExhausted(ApiError):
    pass


def encode_path_segment(value: str) -> str:
    """Percent-encode a path parameter the way the live API expects.

    The OpenAPI spec declares ``projectName`` as a ``style: simple`` parameter,
    which would imply encoding ``/`` as ``%2F``. The live server, however,
    answers a ``%2F``-encoded name with a 307 redirect to the literal-slash
    path (verified 2026-09-17), so slashes are kept and everything else,
    including spaces, is percent-encoded:
    ``some-org/Some Project/some-repo`` -> ``some-org/Some%20Project/some-repo``.
    """
    return quote(value, safe="/")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass
class RetryPolicy:
    max_retries: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.25

    def backoff(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2**attempt))
        return delay + random.uniform(0, delay * self.jitter)


@dataclass
class Deployment:
    id: int
    slug: str
    name: str


@dataclass
class Project:
    id: int
    name: str
    tags: list[str] = field(default_factory=list)


@dataclass
class ManagedScanState:
    """Current Managed Scan settings for a project. ``None`` fields mean unknown."""

    diff_scan: Optional[bool]
    full_scan: Optional[bool]
    configured: bool  # False when project_settings returned an empty object


class SemgrepClient:
    def __init__(
        self,
        token: str,
        *,
        retry: Optional[RetryPolicy] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not token:
            raise ValueError(f"{TOKEN_ENV} is not set")
        self.retry = retry or RetryPolicy()
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "sms-reconcile",
            },
            timeout=timeout,
            transport=transport,
        )
        self.mutating_calls = 0
        # When any thread is told to back off (429), every thread waits.
        self._pause_lock = threading.Lock()
        self._pause_until = 0.0

    def _wait_if_paused(self) -> None:
        with self._pause_lock:
            wait = self._pause_until - time.monotonic()
        if wait > 0:
            _sleep(wait)
            with self._pause_lock:
                self._pause_until = 0.0

    def _pause_all(self, delay: float) -> None:
        with self._pause_lock:
            self._pause_until = max(self._pause_until, time.monotonic() + delay)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SemgrepClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if method.upper() in {"PATCH", "POST", "PUT", "DELETE"} and not path.endswith(
            "/project_settings"
        ):
            self.mutating_calls += 1
        attempt = 0
        while True:
            self._wait_if_paused()
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.retry.max_retries:
                    raise RetriesExhausted(redact(f"{method} failed after {attempt} retries: {exc!r}"))
                delay = self.retry.backoff(attempt)
                log.warning("%s transport error (%s); retry %d in %.1fs", method, type(exc).__name__, attempt + 1, delay)
                _sleep(delay)
                attempt += 1
                continue

            if response.status_code < 300:
                return response

            if 300 <= response.status_code < 400:
                # Never count a redirect as success: the request did not reach
                # its handler. We do not follow redirects, so the bearer token
                # can never be replayed to an unexpected location.
                # The path (and Location) embed the project name, so keep them
                # out of the message; default logs must carry ids only.
                raise ApiError(
                    f"{method} returned HTTP {response.status_code} redirect; "
                    "the endpoint path or encoding is wrong (run with -vv for details)",
                    response.status_code,
                )

            if response.status_code in RETRYABLE_STATUS and attempt < self.retry.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else self.retry.backoff(attempt)
                log.warning(
                    "%s returned HTTP %d; retry %d/%d in %.1fs",
                    method,
                    response.status_code,
                    attempt + 1,
                    self.retry.max_retries,
                    delay,
                )
                attempt += 1
                if response.status_code == 429:
                    # Rate limited: make every worker back off, not just this one.
                    self._pause_all(delay)
                else:
                    _sleep(delay)
                continue

            body = redact(response.text[:200].replace("\n", " "))
            log.debug("%s %s -> HTTP %d: %s", method, path, response.status_code, body)
            raise ApiError(f"{method} returned HTTP {response.status_code}: {body}", response.status_code)

    @staticmethod
    def _json_or_empty(response: httpx.Response) -> Any:
        """Decode a JSON body, treating an empty or non-JSON 2xx body as ``{}``.

        The live v1 managed-scan PATCH returns a success status with no body
        even though the spec documents a response object.
        """
        if not response.content.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            log.debug("HTTP %d response was not JSON (%d bytes)", response.status_code, len(response.content))
            return {}

    def _get_json(self, path: str, **kwargs: Any) -> Any:
        return self._json_or_empty(self._request("GET", path, **kwargs))

    # -- v1 ------------------------------------------------------------------

    def list_deployments(self) -> list[Deployment]:
        data = self._get_json("/api/v1/deployments")
        return [
            Deployment(id=int(d["id"]), slug=str(d["slug"]), name=str(d.get("name", "")))
            for d in data.get("deployments", [])
        ]

    def resolve_deployment(self, slug: str) -> Deployment:
        deployments = self.list_deployments()
        for d in deployments:
            if d.slug.lower() == slug.lower():
                return d
        known = ", ".join(d.slug for d in deployments) or "<none>"
        raise ApiError(f"deployment slug {slug!r} not accessible with this token (accessible: {known})")

    def list_projects(self, slug: str, page_size: int = 100) -> list[Project]:
        """Iterate every page of GET /deployments/{slug}/projects.

        The response carries no total or cursor. We deliberately do not stop on
        a "short" page because the server may cap ``page_size`` below what we
        asked for; instead we read until a page is empty or adds no new ids.
        """
        projects: list[Project] = []
        seen: set[int] = set()
        page = 0
        while True:
            data = self._get_json(
                f"/api/v1/deployments/{encode_path_segment(slug)}/projects",
                params={"page": page, "page_size": page_size},
            )
            batch = data.get("projects", []) or []
            new = 0
            for raw in batch:
                pid = int(raw["id"])
                if pid in seen:
                    continue
                seen.add(pid)
                new += 1
                tags = raw.get("tags") or []
                if isinstance(tags, str):
                    tags = [tags]
                projects.append(Project(id=pid, name=str(raw["name"]), tags=[str(t) for t in tags]))
            log.debug("projects page %d returned %d records (%d new)", page, len(batch), new)
            if not batch or new == 0:
                break
            page += 1
        return projects

    def toggle_managed_scan(self, slug: str, project_name: str, enabled: bool) -> dict[str, Any]:
        path = (
            f"/api/v1/deployments/{encode_path_segment(slug)}"
            f"/projects/{encode_path_segment(project_name)}/managed-scan"
        )
        body = {"diff_scan": {"enabled": enabled}, "full_scan": {"enabled": enabled}}
        response = self._request("PATCH", path, json=body)
        log.debug("managed-scan PATCH -> HTTP %d (%d bytes)", response.status_code, len(response.content))
        return self._json_or_empty(response)

    # -- v2 ------------------------------------------------------------------

    def bulk_get_settings(self, deployment_id: int, project_ids: Iterable[int], batch_size: int = 200) -> dict[int, ManagedScanState]:
        """POST /api/sms/v2/deployments/{id}/project_settings in batches.

        Read-only despite being a POST. Projects without Managed Scans set up
        come back with an empty settings object; we surface that as
        ``configured=False``.
        """
        ids = list(project_ids)
        result: dict[int, ManagedScanState] = {}
        for start in range(0, len(ids), batch_size):
            chunk = ids[start : start + batch_size]
            data = self._json_or_empty(
                self._request(
                    "POST",
                    f"/api/sms/v2/deployments/{deployment_id}/project_settings",
                    json={"projectIds": [str(i) for i in chunk]},
                )
            )
            for entry in data.get("settingsByProject", []) or []:
                pid = int(entry["projectId"])
                settings = entry.get("project_managed_scan_settings") or entry.get("projectManagedScanSettings") or {}
                diff = settings.get("diff_scan") or settings.get("diffScan") or {}
                full = settings.get("full_scan") or settings.get("fullScan") or {}
                # An empty object means "no Managed Scan configuration". Inside a
                # non-empty object, proto3 JSON may omit ``enabled`` when it is
                # false, so a missing flag reads as disabled, not unknown.
                configured = bool(settings)
                result[pid] = ManagedScanState(
                    diff_scan=bool(diff.get("enabled", False)) if configured else None,
                    full_scan=bool(full.get("enabled", False)) if configured else None,
                    configured=configured,
                )
            # Projects absent from the response are simply not configured.
            for pid in chunk:
                result.setdefault(pid, ManagedScanState(None, None, configured=False))
        return result

    def bulk_edit_managed_scans(self, deployment_id: int, changes: list[tuple[int, bool]]) -> list[str]:
        """PATCH /api/agent/deployments/{id}/repos (EXPERIMENTAL).

        ``changes`` is a list of ``(project_id, enabled)``. Returns the
        ``updatedRepoNames`` reported by the API.
        """
        body = {
            "changes": [
                {
                    "repoId": str(pid),
                    "change": {"managedScans": {"diffScan": enabled, "fullScan": enabled}},
                }
                for pid, enabled in changes
            ]
        }
        data = self._json_or_empty(self._request("PATCH", f"/api/agent/deployments/{deployment_id}/repos", json=body))
        return [str(n) for n in data.get("updatedRepoNames", []) or []]
