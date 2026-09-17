"""Shared fixtures: a stateful fake of the Semgrep API mounted on respx."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import httpx
import pytest
import respx
from click.testing import CliRunner

from sms_reconcile import client as client_mod
from sms_reconcile.cli import main

TOKEN = "sgp_test_token_DO_NOT_LOG_0123456789"
SLUG = "acme"
DEPLOYMENT_ID = 4242
BASE = "https://semgrep.dev"


class FakeApi:
    """Minimal in-memory Semgrep deployment.

    ``projects``: list of dicts with ``id``, ``name``, ``tags``.
    ``settings``: id -> (diff, full) tuple, or None for "no Managed Scan config".
    Mutating routes update ``settings`` so post-apply verification is realistic.
    """

    def __init__(self, router: respx.MockRouter, projects: list[dict], settings: dict[int, Optional[tuple[bool, bool]]], page_size: int = 100):
        self.router = router
        self.projects = projects
        self.settings = dict(settings)
        self.page_size = page_size
        self.bulk_response_override = None  # callable(request_changes) -> list[str]

        self.deployments = router.get(f"{BASE}/api/v1/deployments").mock(
            return_value=httpx.Response(200, json={"deployments": [{"id": DEPLOYMENT_ID, "slug": SLUG, "name": "Acme"}]})
        )
        self.list_projects = router.get(f"{BASE}/api/v1/deployments/{SLUG}/projects").mock(side_effect=self._list_projects)
        self.project_settings = router.post(f"{BASE}/api/sms/v2/deployments/{DEPLOYMENT_ID}/project_settings").mock(side_effect=self._project_settings)
        self.toggle = router.patch(url__regex=rf"{BASE}/api/v1/deployments/{SLUG}/projects/.*/managed-scan$").mock(side_effect=self._toggle)
        self.bulk = router.patch(f"{BASE}/api/agent/deployments/{DEPLOYMENT_ID}/repos").mock(side_effect=self._bulk)

    # -- handlers --------------------------------------------------------------

    def _list_projects(self, request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 0))
        # Real API: page_size must be 100-3000; the fake additionally caps at self.page_size.
        size = min(int(request.url.params.get("page_size", 100)), self.page_size)
        chunk = self.projects[page * size : (page + 1) * size]
        return httpx.Response(200, json={"projects": chunk})

    def _project_settings(self, request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["projectIds"]
        out = []
        for pid in ids:
            state = self.settings.get(int(pid))
            entry = {"projectId": str(pid), "project_managed_scan_settings": {}}
            if state is not None:
                entry["project_managed_scan_settings"] = {"diff_scan": {"enabled": state[0]}, "full_scan": {"enabled": state[1]}}
            out.append(entry)
        return httpx.Response(200, json={"settingsByProject": out})

    def _project_by_name(self, name: str) -> Optional[dict]:
        for p in self.projects:
            if p["name"].lower() == name.lower():
                return p
        return None

    def _toggle(self, request: httpx.Request) -> httpx.Response:
        encoded = request.url.raw_path.decode().split("/projects/", 1)[1].rsplit("/managed-scan", 1)[0]
        name = unquote(encoded)
        body = json.loads(request.content)
        project = self._project_by_name(name)
        if project is None:
            return httpx.Response(404, json={"error": "not found"})
        self.settings[project["id"]] = (body["diff_scan"]["enabled"], body["full_scan"]["enabled"])
        return httpx.Response(200, json={"project": {"id": project["id"], "name": project["name"], "tags": project["tags"]}})

    def _bulk(self, request: httpx.Request) -> httpx.Response:
        changes = json.loads(request.content)["changes"]
        names = []
        for ch in changes:
            pid = int(ch["repoId"])
            ms = ch["change"]["managedScans"]
            self.settings[pid] = (ms["diffScan"], ms["fullScan"])
            names.append(next(p["name"] for p in self.projects if p["id"] == pid))
        if self.bulk_response_override:
            names = self.bulk_response_override(changes, names)
        return httpx.Response(200, json={"updatedRepoNames": names})

    # -- helpers ---------------------------------------------------------------

    @property
    def mutating_call_count(self) -> int:
        return self.toggle.call_count + self.bulk.call_count


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv(client_mod.TOKEN_ENV, TOKEN)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(client_mod, "_sleep", lambda s: calls.append(s))
    return calls


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def runner():
    return CliRunner(mix_stderr=False)


def write_list(tmp_path: Path, entries: list[str], header: Optional[str] = "name") -> Path:
    path = tmp_path / "repos.csv"
    lines = ([header] if header else []) + entries
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def invoke(runner: CliRunner, *args: str):
    result = runner.invoke(main, list(args), catch_exceptions=False)
    return result
