"""End-to-end CLI tests against the respx fake API (all HTTP mocked)."""

from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from conftest import BASE, DEPLOYMENT_ID, SLUG, TOKEN, FakeApi, invoke, write_list


def _projects(*names_and_tags):
    return [{"id": 100 + i, "name": name, "tags": tags} for i, (name, tags) in enumerate(names_and_tags)]


def _rows(output: str) -> dict[str, str]:
    """Map project name -> action from the rendered table."""
    rows = {}
    for line in output.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) == 5 and parts[0] != "ID" and not all(set(c) <= {"-"} for c in parts):
            rows[parts[1]] = parts[4]
    return rows


# 1 ---------------------------------------------------------------------------
def test_plan_makes_no_mutating_calls(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/on-list-off", ["managed-scan"]), ("acme/off-list-on", ["managed-scan"])), {100: (False, False), 101: (True, True)})
    lst = write_list(tmp_path, ["acme/on-list-off"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 0, res.output
    rows = _rows(res.stdout)
    assert rows["acme/on-list-off"] == "enable"
    assert rows["acme/off-list-on"] == "disable"
    assert api.mutating_call_count == 0
    assert api.project_settings.called  # the read-only POST is fine
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["command"] == "plan" and report["applied"] == []


# 2 ---------------------------------------------------------------------------
def test_path_encoding_spaces_and_slashes(router, runner, tmp_path):
    name = "some-org/Some Project/some-repo"
    api = FakeApi(router, _projects((name, ["managed-scan"])), {100: (False, False)})
    lst = write_list(tmp_path, [name])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 1
    request: httpx.Request = api.toggle.calls[0].request
    assert request.url.raw_path == b"/api/v1/deployments/acme/projects/some-org/Some%20Project/some-repo/managed-scan"
    assert json.loads(request.content) == {"diff_scan": {"enabled": True}, "full_scan": {"enabled": True}}


# 3 ---------------------------------------------------------------------------
def test_case_insensitive_full_match(router, runner, tmp_path):
    FakeApi(router, _projects(("Acme/Repo-One", ["managed-scan"])), {100: (True, True)})
    lst = write_list(tmp_path, ["acme/repo-one"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0
    assert _rows(res.stdout)["Acme/Repo-One"] == "no-op"
    assert "not-found" not in res.stdout


def test_ambiguous_bare_name_warns_and_skips(router, runner, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    api = FakeApi(
        router,
        _projects(("org-a/shared", ["managed-scan"]), ("org-b/shared", ["managed-scan"]), ("org-a/unique", ["managed-scan"])),
        {100: (False, False), 101: (False, False), 102: (False, False)},
    )
    lst = write_list(tmp_path, ["Shared", "UNIQUE"])
    res = invoke(runner, "apply", "--yes", "--match", "repo", "--slug", SLUG, "--list", str(lst))
    rows = _rows(res.stdout)
    assert rows["org-a/shared"] == "ambiguous" and rows["org-b/shared"] == "ambiguous"
    assert rows["org-a/unique"] == "enable"
    assert any("ambiguous" in r.getMessage() for r in caplog.records)
    # Only the unambiguous project is touched.
    assert api.toggle.call_count == 1
    assert "unique" in api.toggle.calls[0].request.url.raw_path.decode()


def test_allow_ambiguous_selects_all(router, runner, tmp_path):
    api = FakeApi(router, _projects(("org-a/shared", ["managed-scan"]), ("org-b/shared", ["managed-scan"])), {100: (False, False), 101: (False, False)})
    lst = write_list(tmp_path, ["shared"])
    res = invoke(runner, "apply", "--yes", "--match", "repo", "--allow-ambiguous", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 2


# 4 ---------------------------------------------------------------------------
def test_idempotent_no_patch_when_already_desired(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/on", ["managed-scan"]), ("acme/off", ["managed-scan"])), {100: (True, True), 101: (False, False)})
    lst = write_list(tmp_path, ["acme/on"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 0, res.output
    assert api.mutating_call_count == 0
    assert "nothing to apply" in res.stdout
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["summary"]["no-op"] == 2 and report["applied"] == []


def test_partial_state_is_corrected(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/half", ["managed-scan"])), {100: (True, False)})
    lst = write_list(tmp_path, ["acme/half"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 1
    assert api.settings[100] == (True, True)


# 5 ---------------------------------------------------------------------------
def test_not_managed_projects_never_patched(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/ci-only", []), ("acme/managed", ["managed-scan"])), {100: None, 101: (True, True)})
    lst = write_list(tmp_path, ["acme/nothing-else"])  # neither project is on the list
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    rows = _rows(res.stdout)
    assert rows["acme/ci-only"] == "not-managed"
    assert rows["acme/managed"] == "disable"
    assert rows["acme/nothing-else"] == "not-found-in-deployment"
    assert api.toggle.call_count == 1
    assert "ci-only" not in api.toggle.calls[0].request.url.raw_path.decode()


def test_settings_endpoint_marks_untagged_project_managed(router, runner, tmp_path):
    """A project with settings but no tag is still in scope."""
    api = FakeApi(router, _projects(("acme/untagged", [])), {100: (True, True)})
    lst = write_list(tmp_path, ["acme/other"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert _rows(res.stdout)["acme/untagged"] == "disable"
    assert api.toggle.call_count == 1


# 6 ---------------------------------------------------------------------------
def test_pagination_across_pages(router, runner, tmp_path):
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(5)])
    api = FakeApi(router, projects, {p["id"]: (True, True) for p in projects}, page_size=2)
    lst = write_list(tmp_path, [p["name"] for p in projects])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst), "--page-size", "100")
    assert res.exit_code == 0
    # 3 pages of data plus one empty page: a short page is not trusted as the end.
    assert api.list_projects.call_count == 4
    pages = [c.request.url.params["page"] for c in api.list_projects.calls]
    assert pages == ["0", "1", "2", "3"]
    assert all(c.request.url.params["page_size"] == "100" for c in api.list_projects.calls)
    rows = _rows(res.stdout)
    assert len(rows) == 5 and set(rows.values()) == {"no-op"}


# 7 ---------------------------------------------------------------------------
def test_429_honours_retry_after_then_succeeds(router, runner, tmp_path, no_sleep):
    api = FakeApi(router, _projects(("acme/busy", ["managed-scan"])), {100: (False, False)})
    real = api._toggle
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"})
        return real(request)

    api.toggle.side_effect = flaky
    lst = write_list(tmp_path, ["acme/busy"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 2
    assert len(no_sleep) == 1 and no_sleep[0] == pytest.approx(3.0, abs=0.05)
    assert api.settings[100] == (True, True)


def test_retries_exhausted_reports_failure(router, runner, tmp_path, no_sleep):
    api = FakeApi(router, _projects(("acme/down", ["managed-scan"])), {100: (False, False)})
    api.toggle.side_effect = lambda request: httpx.Response(503)
    lst = write_list(tmp_path, ["acme/down"])
    res = invoke(runner, "apply", "--yes", "--max-retries", "2", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 1
    assert api.toggle.call_count == 3  # 1 + 2 retries
    assert len(no_sleep) == 2
    report = json.loads((tmp_path / "r.json").read_text())
    assert len(report["failed"]) == 1 and "503" in report["failed"][0]["error"]


# 8 ---------------------------------------------------------------------------
def test_apply_without_yes_refuses_and_touches_nothing(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/x", ["managed-scan"])), {100: (False, False)})
    lst = write_list(tmp_path, ["acme/x"])
    res = invoke(runner, "apply", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 2
    assert "--yes" in res.stderr
    assert router.calls.call_count == 0
    assert api.mutating_call_count == 0


# 9 ---------------------------------------------------------------------------
def test_bulk_batches_and_maps_updated_names(router, runner, tmp_path):
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(5)])
    api = FakeApi(router, projects, {p["id"]: (False, False) for p in projects})
    # Pretend the API silently failed to update repo-4 (drop it from updatedRepoNames).
    api.bulk_response_override = lambda changes, names: [n for n in names if n != "acme/repo-4"]
    lst = write_list(tmp_path, [p["name"] for p in projects])
    res = invoke(runner, "apply", "--yes", "--bulk", "--batch-size", "2", "--no-verify", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 1, res.output  # one unconfirmed item
    assert "EXPERIMENTAL" in res.stderr
    assert api.toggle.call_count == 0
    assert api.bulk.call_count == 3
    sizes = [len(json.loads(c.request.content)["changes"]) for c in api.bulk.calls]
    assert sizes == [2, 2, 1]
    first = json.loads(api.bulk.calls[0].request.content)["changes"][0]
    assert first == {"repoId": "100", "change": {"managedScans": {"diffScan": True, "fullScan": True}}}
    report = json.loads((tmp_path / "r.json").read_text())
    assert sorted(a["name"] for a in report["applied"]) == [f"acme/repo-{i}" for i in range(4)]
    assert [f["name"] for f in report["failed"]] == ["acme/repo-4"]
    assert "updatedRepoNames" in report["failed"][0]["error"]


def test_bulk_post_verify_detects_drift(router, runner, tmp_path):
    projects = _projects(("acme/a", ["managed-scan"]))
    api = FakeApi(router, projects, {100: (False, False)})

    def lie(request):  # claims success but does not change state
        return httpx.Response(200, json={"updatedRepoNames": ["acme/a"]})

    api.bulk.side_effect = lie
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--yes", "--bulk", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 1
    assert "post-apply verification: 1 project(s) not in desired state" in res.stderr


# 10 --------------------------------------------------------------------------
def test_verify_exit_codes(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"]), ("acme/b", ["managed-scan"])), {100: (True, True), 101: (True, True)})
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "verify", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 1
    assert "DRIFT: 1" in res.stdout
    assert api.mutating_call_count == 0
    report = json.loads((tmp_path / "r.json").read_text())
    assert [d["name"] for d in report["drift"]] == ["acme/b"]

    api.settings[101] = (False, False)
    res = invoke(runner, "verify", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0
    assert "clean" in res.stdout


# 11 --------------------------------------------------------------------------
def test_token_never_logged_or_reported(router, runner, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (False, False)})
    api.toggle.side_effect = lambda request: httpx.Response(500, text=f"echo {request.headers['Authorization']}")
    lst = write_list(tmp_path, ["acme/a"])
    report_path = tmp_path / "r.json"
    res = invoke(runner, "apply", "--yes", "-vv", "--verbose-names", "--max-retries", "0", "--slug", SLUG, "--list", str(lst), "--report", str(report_path))
    assert res.exit_code == 1
    assert caplog.records, "expected log output at DEBUG"
    assert TOKEN not in caplog.text
    assert TOKEN not in res.stdout and TOKEN not in res.stderr
    assert TOKEN not in report_path.read_text()
    # The error body echoed the header; it must have been scrubbed, not dropped.
    assert "REDACTED" in caplog.text


def test_default_logs_use_ids_not_names(router, runner, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    FakeApi(router, _projects(("acme/secret-name", ["managed-scan"])), {100: (False, False)})
    lst = write_list(tmp_path, ["acme/secret-name"])
    res = invoke(runner, "apply", "--yes", "-vv", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0
    assert "secret-name" not in caplog.text
    assert "project id=100" in caplog.text


def test_exclude_pattern_leaves_projects_untouched(router, runner, tmp_path):
    api = FakeApi(router, _projects(("local_scan/thing", ["managed-scan"]), ("acme/a", ["managed-scan"])), {100: (True, True), 101: (True, True)})
    lst = write_list(tmp_path, ["nothing"])
    res = invoke(runner, "apply", "--yes", "--exclude-pattern", "local_scan/*", "--slug", SLUG, "--list", str(lst))
    rows = _rows(res.stdout)
    assert rows["local_scan/thing"] == "excluded"
    assert api.toggle.call_count == 1


def test_missing_token_is_usage_error(router, runner, tmp_path, monkeypatch):
    monkeypatch.delenv("SEMGREP_APP_TOKEN")
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 2
    assert "SEMGREP_APP_TOKEN" in res.stderr
    assert router.calls.call_count == 0


def test_pagination_stops_when_server_caps_page_size(router, runner, tmp_path):
    """Server returns at most 2 per page even though we ask for 100."""
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(5)])
    api = FakeApi(router, projects, {p["id"]: (True, True) for p in projects})

    def capped(request):
        page = int(request.url.params.get("page", 0))
        return httpx.Response(200, json={"projects": projects[page * 2 : (page + 1) * 2]})

    api.list_projects.side_effect = capped
    lst = write_list(tmp_path, [p["name"] for p in projects])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0
    assert len(_rows(res.stdout)) == 5


def test_missing_enabled_flag_reads_as_disabled(router, runner, tmp_path):
    """proto3 JSON may omit ``enabled: false``; that is a configured-but-off project."""
    api = FakeApi(router, _projects(("acme/a", [])), {})

    def sparse(request):
        return httpx.Response(200, json={"settingsByProject": [{"projectId": "100", "project_managed_scan_settings": {"diff_scan": {}, "full_scan": {}}}]})

    api.project_settings.side_effect = sparse
    lst = write_list(tmp_path, ["nothing"])
    res = invoke(runner, "apply", "--yes", "--no-verify", "--slug", SLUG, "--list", str(lst))
    assert _rows(res.stdout)["acme/a"] == "no-op"
    assert api.mutating_call_count == 0


def test_page_size_below_api_minimum_rejected(router, runner, tmp_path):
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst), "--page-size", "2")
    assert res.exit_code == 2 and router.calls.call_count == 0


def test_empty_patch_body_is_success(router, runner, tmp_path):
    """The live v1 managed-scan PATCH returns 2xx with no body."""
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (False, False)})
    real = api._toggle

    def empty_body(request):
        real(request)  # update fake state
        return httpx.Response(200, content=b"")

    api.toggle.side_effect = empty_body
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 0, res.output
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["applied"] == 1


def test_non_json_2xx_tolerated_and_caught_by_post_verify(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"]), ("acme/b", ["managed-scan"])), {100: (False, False), 101: (False, False)})
    real = api._toggle

    def half_broken(request):
        if "acme/a/" in request.url.raw_path.decode():
            return httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})
        return real(request)

    api.toggle.side_effect = half_broken
    lst = write_list(tmp_path, ["acme/a", "acme/b"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    report = json.loads((tmp_path / "r.json").read_text())
    # non-JSON 2xx is tolerated as success for the PATCH; b applied normally; run completed
    assert report["summary"]["failed"] == 0 and report["summary"]["applied"] == 2
    assert res.exit_code == 1  # post-verify catches that acme/a never actually changed
    assert [d["name"] for d in report["drift"]] == ["acme/a"]


def test_redirect_is_a_failure_not_success(router, runner, tmp_path):
    """A 307 means the request never reached its handler; it must be recorded as failed."""
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (False, False)})
    api.toggle.side_effect = lambda request: httpx.Response(307, headers={"Location": "/elsewhere"})
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 1
    assert api.toggle.call_count == 1  # not retried, not followed
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["summary"]["applied"] == 0 and "307" in report["failed"][0]["error"]
    assert api.settings[100] == (False, False)


# --- mass-disable guard -------------------------------------------------------
def _many(n, on=True):
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(n)])
    return projects, {p["id"]: (on, on) for p in projects}


def test_guard_refuses_empty_list(router, runner, tmp_path):
    projects, settings = _many(3)
    api = FakeApi(router, projects, settings)
    lst = write_list(tmp_path, [])  # header only
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 3
    assert "list is empty" in res.stderr
    assert api.mutating_call_count == 0
    assert json.loads((tmp_path / "r.json").read_text())["status"] == "refused-by-guard"


def test_guard_refuses_mass_disable_and_can_be_raised(router, runner, tmp_path):
    projects, settings = _many(60)
    api = FakeApi(router, projects, settings)
    lst = write_list(tmp_path, [p["name"] for p in projects[:40]])  # would disable 20 of 60 (> 20% = 12)
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 3 and "above the limit of 12" in res.stderr
    assert api.mutating_call_count == 0
    res = invoke(runner, "apply", "--yes", "--max-disable", "20", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 20


def test_guard_floor_allows_small_deployments(router, runner, tmp_path):
    """Disabling 9 of 10 is under the floor of 10, so small test deployments still work."""
    projects, settings = _many(10)
    api = FakeApi(router, projects, settings)
    lst = write_list(tmp_path, [projects[0]["name"]])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 9


def test_allow_mass_disable_overrides_everything(router, runner, tmp_path):
    projects, settings = _many(3)
    api = FakeApi(router, projects, settings)
    lst = write_list(tmp_path, [])
    res = invoke(runner, "apply", "--yes", "--allow-mass-disable", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 3


def test_plan_notes_guard_but_exits_zero(router, runner, tmp_path):
    projects, settings = _many(3)
    FakeApi(router, projects, settings)
    lst = write_list(tmp_path, [])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and "apply would refuse" in res.stderr


# --- report and output ergonomics ------------------------------------------------
def test_report_written_when_run_fails(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (True, True)})
    api.list_projects.side_effect = lambda request: httpx.Response(500, text="boom")
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst), "--max-retries", "0", "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 1 and "error:" in res.stderr
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["status"] == "failed" and "500" in report["error"]
    assert report["deployment"]["id"] == DEPLOYMENT_ID  # resolved before the failure


def test_unexpected_exception_is_clean_error(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (True, True)})
    api.deployments.side_effect = lambda request: httpx.Response(200, json={"deployments": [{"slug": SLUG}]})  # missing id
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 1
    assert "error: KeyError" in res.stderr and "Traceback" not in res.stderr


def test_only_changes_hides_quiet_rows(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/quiet", ["managed-scan"]), ("acme/loud", ["managed-scan"]), ("acme/ci", [])), {100: (True, True), 101: (False, False), 102: None})
    lst = write_list(tmp_path, ["acme/quiet", "acme/loud"])
    res = invoke(runner, "plan", "--only-changes", "--slug", SLUG, "--list", str(lst))
    assert "acme/loud" in res.stdout and "acme/quiet" not in res.stdout and "acme/ci" not in res.stdout
    assert "no-op=1" in res.stdout  # summary still complete


def test_redirect_error_does_not_leak_project_name(router, runner, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    api = FakeApi(router, _projects(("acme/hidden-name", ["managed-scan"])), {100: (False, False)})
    api.toggle.side_effect = lambda request: httpx.Response(307, headers={"Location": "/api/v1/deployments/acme/projects/acme/hidden-name/managed-scan"})
    lst = write_list(tmp_path, ["acme/hidden-name"])
    invoke(runner, "apply", "--yes", "-v", "--slug", SLUG, "--list", str(lst))
    assert "hidden-name" not in caplog.text


def test_cli_accepts_json_list_and_reports_bad_list(router, runner, tmp_path):
    FakeApi(router, _projects(("Acme/A", ["managed-scan"]), ("acme/b", ["managed-scan"])), {100: (True, True), 101: (True, True)})
    lst = tmp_path / "repos.json"
    lst.write_text(json.dumps({"repos": [{"full_name": "acme/a"}, {"full_name": "acme/b"}]}))
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and set(_rows(res.stdout).values()) == {"no-op"}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(bad), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 2 and "could not read --list" in res.stderr
    assert json.loads((tmp_path / "r.json").read_text())["status"] == "failed"


# --- exclude mode ---------------------------------------------------------------
def _exclude_fixture(router):
    return FakeApi(
        router,
        _projects(("acme/listed-on", ["managed-scan"]), ("acme/listed-off", ["managed-scan"]), ("acme/unlisted-on", ["managed-scan"]), ("acme/unlisted-off", ["managed-scan"]), ("acme/ci-only", [])),
        {100: (True, True), 101: (False, False), 102: (True, True), 103: (False, False), 104: None},
    )


def test_exclude_mode_plan(router, runner, tmp_path):
    api = _exclude_fixture(router)
    lst = write_list(tmp_path, ["acme/listed-on", "acme/listed-off", "acme/ci-only", "acme/missing"])
    res = invoke(runner, "plan", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    rows = _rows(res.stdout)
    assert rows == {
        "acme/listed-on": "disable",
        "acme/listed-off": "no-op",
        "acme/unlisted-on": "unlisted",
        "acme/unlisted-off": "unlisted",
        "acme/ci-only": "not-managed",
        "acme/missing": "not-found-in-deployment",
    }
    assert "apply would refuse" not in res.stderr
    assert api.mutating_call_count == 0


def test_exclude_mode_apply_touches_only_listed(router, runner, tmp_path):
    api = _exclude_fixture(router)
    lst = write_list(tmp_path, ["acme/listed-on", "acme/listed-off"])
    res = invoke(runner, "apply", "--yes", "--mode", "exclude", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 1
    assert "listed-on" in api.toggle.calls[0].request.url.raw_path.decode()
    assert api.settings[100] == (False, False) and api.settings[102] == (True, True)  # unlisted-on untouched
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["summary"]["unlisted"] == 2 and report["summary"]["applied"] == 1


def test_exclude_mode_never_enables(router, runner, tmp_path):
    api = _exclude_fixture(router)
    lst = write_list(tmp_path, ["acme/unlisted-off"])  # off already; must not be flipped on
    res = invoke(runner, "apply", "--yes", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and "nothing to apply" in res.stdout
    assert api.mutating_call_count == 0


def test_exclude_mode_empty_list_is_noop_not_refusal(router, runner, tmp_path):
    api = _exclude_fixture(router)
    lst = write_list(tmp_path, [])
    res = invoke(runner, "apply", "--yes", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and "nothing to apply" in res.stdout
    assert api.mutating_call_count == 0


def test_exclude_mode_not_guarded(router, runner, tmp_path):
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(30)])
    api = FakeApi(router, projects, {p["id"]: (True, True) for p in projects})
    lst = write_list(tmp_path, [p["name"] for p in projects])  # disable all 30 deliberately
    res = invoke(runner, "apply", "--yes", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 30


def test_exclude_mode_verify(router, runner, tmp_path):
    api = _exclude_fixture(router)
    lst = write_list(tmp_path, ["acme/listed-on", "acme/listed-off"])
    res = invoke(runner, "verify", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 1 and "DRIFT: 1" in res.stdout and "listed-on" in res.stdout
    api.settings[100] = (False, False)
    res = invoke(runner, "verify", "--mode", "exclude", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and "clean" in res.stdout
    # Unlisted projects being on is not drift in exclude mode.
    assert api.settings[102] == (True, True)


def test_only_changes_hides_unlisted(router, runner, tmp_path):
    _exclude_fixture(router)
    lst = write_list(tmp_path, ["acme/listed-on"])
    res = invoke(runner, "plan", "--mode", "exclude", "--only-changes", "--slug", SLUG, "--list", str(lst))
    assert "unlisted-on" not in res.stdout and "listed-on" in res.stdout


# --- unknown-state ---------------------------------------------------------------
def test_unknown_state_is_visible_and_not_patched(router, runner, tmp_path):
    """Tagged managed-scan but the settings endpoint returns nothing: report, do not write."""
    api = FakeApi(router, _projects(("acme/ghost", ["managed-scan"]), ("acme/normal", ["managed-scan"])), {100: None, 101: (True, True)})
    lst = write_list(tmp_path, ["acme/nothing"])
    res = invoke(runner, "plan", "--slug", SLUG, "--list", str(lst))
    assert _rows(res.stdout)["acme/ghost"] == "unknown-state"
    assert "1 project(s) are unknown-state" in res.stderr
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 1 and "normal" in api.toggle.calls[0].request.url.raw_path.decode()
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["summary"]["unknown-state"] == 1


def test_patch_unknown_opts_in(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/ghost", ["managed-scan"])), {100: None})
    lst = write_list(tmp_path, ["acme/ghost"])
    res = invoke(runner, "apply", "--yes", "--patch-unknown", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output
    assert api.toggle.call_count == 1 and api.settings[100] == (True, True)


def test_unknown_state_not_counted_by_guard(router, runner, tmp_path):
    """Unknown-state projects are neither disables nor part of the managed denominator."""
    projects = _projects(*[(f"acme/repo-{i}", ["managed-scan"]) for i in range(12)])
    settings = {p["id"]: (True, True) for p in projects[:2]}
    settings.update({p["id"]: None for p in projects[2:]})  # 10 unknown
    api = FakeApi(router, projects, settings)
    lst = write_list(tmp_path, ["acme/none"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0, res.output  # 2 disables of 2 managed: under the floor of 10
    assert api.toggle.call_count == 2


def test_settings_read_failure_aborts_before_any_write(router, runner, tmp_path):
    api = FakeApi(router, _projects(("acme/a", ["managed-scan"])), {100: (True, True)})
    api.project_settings.side_effect = lambda request: httpx.Response(503)
    lst = write_list(tmp_path, ["acme/none"])
    res = invoke(runner, "apply", "--yes", "--max-retries", "0", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 1 and "503" in res.stderr
    assert api.mutating_call_count == 0
