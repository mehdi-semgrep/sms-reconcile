"""Scale rehearsal: a 5,000-project Azure DevOps-style deployment, fully mocked.

Models the client scenario: connecting ADO projects to Managed Scans auto-enrolled
every repo (all on/on); the source of truth lists only a subset. Names carry the
``org/project/repo`` shape with spaces and ``&`` in project names. Sporadic 429s
are injected to exercise retries under concurrency.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx
import pytest

from conftest import SLUG, FakeApi, invoke

ORG = "contoso"
PROJECTS = ["Platform Engineering", "Payments", "Data & Analytics", "Mobile Apps", "Infra"]
N = 5000
LISTED = 4200  # 800 to disable: under the 20% guard (1000) so apply proceeds


def _deployment():
    projects = [
        {"id": 1_000_000 + i, "name": f"{ORG}/{PROJECTS[i % len(PROJECTS)]}/repo-{i:04d}", "tags": ["managed-scan"]}
        for i in range(N)
    ]
    settings = {p["id"]: (True, True) for p in projects}  # auto-enrolled
    return projects, settings


def _az_repos_list(projects):
    """Shape of `az repos list -o json`, one entry per listed repo, with encoded URLs."""
    out = []
    for p in projects[:LISTED]:
        org, proj, repo = p["name"].split("/")
        out.append(
            {
                "name": repo,
                "project": {"name": proj},
                "remoteUrl": f"https://{org}@dev.azure.com/{org}/{quote(proj)}/_git/{repo}",
                "isDisabled": False,
            }
        )
    return out


def _inject_429s(route, every: int):
    """Wrap a route's handler so every N-th call is rate-limited first."""
    real = route.side_effect
    counter = {"n": 0, "limited": 0}

    def handler(request):
        counter["n"] += 1
        if counter["n"] % every == 0:
            counter["limited"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        return real(request)

    route.side_effect = handler
    return counter


@pytest.fixture
def scale(router, tmp_path):
    projects, settings = _deployment()
    api = FakeApi(router, projects, settings, page_size=1000)  # live API accepts 100-3000
    lst = tmp_path / "az-repos.json"
    lst.write_text(json.dumps(_az_repos_list(projects)))
    return api, lst, projects


def _common(lst, page_size=1000):
    # No --list-field: the loader must pick remoteUrl over the bare `name` on its own.
    return ["--slug", SLUG, "--list", str(lst), "--page-size", str(page_size), "--only-changes"]


def test_plan_5000_projects(scale, runner, tmp_path):
    api, lst, projects = scale
    t0 = time.perf_counter()
    res = invoke(runner, "plan", *_common(lst), "--report", str(tmp_path / "plan.json"))
    elapsed = time.perf_counter() - t0
    assert res.exit_code == 0, res.output
    report = json.loads((tmp_path / "plan.json").read_text())
    assert report["summary"]["no-op"] == LISTED and report["summary"]["disable"] == N - LISTED
    assert "enable" not in report["summary"] and "not-found-in-deployment" not in report["summary"]
    assert api.mutating_call_count == 0
    # 5 data pages of 1000 + 1 empty page; 25 settings batches of 200.
    assert api.list_projects.call_count == 6
    assert api.project_settings.call_count == 25
    assert all(len(json.loads(c.request.content)["projectIds"]) <= 200 for c in api.project_settings.calls)
    assert elapsed < 15, f"plan took {elapsed:.1f}s"
    # spaces and & survived the URL round-trip
    names = {i["name"] for i in report["plan"]}
    assert f"{ORG}/Data & Analytics/repo-0002" in names


def test_apply_v1_800_disables_with_429s(scale, runner, tmp_path, no_sleep):
    api, lst, projects = scale
    limited = _inject_429s(api.toggle, every=50)
    t0 = time.perf_counter()
    res = invoke(runner, "apply", "--yes", "--concurrency", "8", *_common(lst), "--report", str(tmp_path / "apply.json"))
    elapsed = time.perf_counter() - t0
    assert res.exit_code == 0, res.output[-2000:]
    report = json.loads((tmp_path / "apply.json").read_text())
    assert report["status"] == "completed"
    assert report["summary"]["applied"] == N - LISTED and report["summary"]["failed"] == 0 and report["summary"]["drift"] == 0
    assert limited["limited"] >= 15 and len(no_sleep) >= limited["limited"]
    assert api.toggle.call_count == (N - LISTED) + limited["limited"]
    # Every PATCH path keeps slashes literal and encodes spaces/& only.
    paths = {c.request.url.raw_path.decode() for c in api.toggle.calls}
    assert any("/contoso/Platform%20Engineering/repo-" in p for p in paths)
    assert any("/contoso/Data%20%26%20Analytics/repo-" in p for p in paths)
    assert not any("%2F" in p for p in paths)
    # State: exactly the unlisted repos are off.
    off = {pid for pid, st in api.settings.items() if st == (False, False)}
    assert off == {p["id"] for p in projects[LISTED:]}
    assert elapsed < 30, f"apply took {elapsed:.1f}s"


def test_apply_bulk_800_disables(scale, runner, tmp_path):
    api, lst, projects = scale
    res = invoke(runner, "apply", "--yes", "--bulk", "--batch-size", "100", *_common(lst), "--report", str(tmp_path / "bulk.json"))
    assert res.exit_code == 0, res.output[-2000:]
    assert api.bulk.call_count == 8
    assert all(len(json.loads(c.request.content)["changes"]) <= 100 for c in api.bulk.calls)
    report = json.loads((tmp_path / "bulk.json").read_text())
    assert report["summary"]["applied"] == N - LISTED and report["summary"]["failed"] == 0 and report["summary"]["drift"] == 0


def test_guard_trips_above_20_percent(scale, runner, tmp_path):
    api, lst, projects = scale
    short = tmp_path / "short.json"
    short.write_text(json.dumps(_az_repos_list(projects)[: N - 1001]))  # would disable 1001 > 1000
    res = invoke(runner, "apply", "--yes", *_common(short))
    assert res.exit_code == 3 and "above the limit of 1000" in res.stderr
    assert api.mutating_call_count == 0


def test_verify_after_apply_then_drift_reintroduced(scale, runner, tmp_path):
    api, lst, projects = scale
    invoke(runner, "apply", "--yes", "--bulk", *_common(lst))
    assert invoke(runner, "verify", *_common(lst)).exit_code == 0
    # ADO auto-enrolls a new repo and someone re-enables a disabled one: both must show up as drift.
    reenabled = projects[-1]
    api.settings[reenabled["id"]] = (True, True)
    api.projects.append({"id": 9_999_999, "name": f"{ORG}/Payments/brand-new-repo", "tags": ["managed-scan"]})
    api.settings[9_999_999] = (True, True)
    res = invoke(runner, "verify", *_common(lst), "--report", str(tmp_path / "v.json"))
    assert res.exit_code == 1
    drift = {d["name"] for d in json.loads((tmp_path / "v.json").read_text())["drift"]}
    assert drift == {reenabled["name"], f"{ORG}/Payments/brand-new-repo"}
