"""Optional --slug, config file, default report, interactive confirm, whoami, init."""

from __future__ import annotations

import json
import sys

import httpx

from conftest import BASE, DEPLOYMENT_ID, SLUG, FakeApi, invoke, write_list
from sms_reconcile.cli import main


def _one(router, on=(False, False)):
    return FakeApi(router, [{"id": 100, "name": "acme/a", "tags": ["managed-scan"]}], {100: on})


# --- slug resolution ------------------------------------------------------------------
def test_slug_optional_when_token_reaches_one_deployment(router, runner, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO)
    api = _one(router)
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--list", str(lst), "-v")
    assert res.exit_code == 0, res.output
    assert "acme/a" in res.stdout and "using the only deployment" in caplog.text
    assert api.list_projects.called


def test_slug_required_when_token_reaches_several(router, runner, tmp_path):
    api = _one(router)
    api.deployments.return_value = httpx.Response(200, json={"deployments": [{"id": 1, "slug": "one", "name": "1"}, {"id": 2, "slug": "two", "name": "2"}]})
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "plan", "--list", str(lst))
    assert res.exit_code == 1 and "pass --slug" in res.stderr and "one, two" in res.stderr
    assert not api.list_projects.called


def test_list_required_message(router, runner):
    _one(router)
    res = invoke(runner, "plan")
    assert res.exit_code == 2 and "--list is required" in res.stderr


# --- config file -----------------------------------------------------------------------
def test_config_file_supplies_defaults_and_cli_overrides(router, runner, tmp_path, monkeypatch):
    api = FakeApi(router, [{"id": 100, "name": "acme/a", "tags": ["managed-scan"]}, {"id": 101, "name": "local_scan/x", "tags": ["managed-scan"]}], {100: (True, True), 101: (True, True)})
    lst = write_list(tmp_path, ["acme/a"])
    (tmp_path / "sms-reconcile.toml").write_text(
        f'slug = "{SLUG}"\nlist = "{lst}"\nexclude_patterns = ["local_scan/*"]\nonly_changes = true\npage_size = 500\n\n[apply]\nmax_disable = 3\n'
    )
    monkeypatch.chdir(tmp_path)
    res = invoke(runner, "plan")  # everything from the config
    assert res.exit_code == 0, res.output
    assert "excluded=1" in res.stdout and "no changes" in res.stdout
    assert api.list_projects.calls[0].request.url.params["page_size"] == "500"
    res = invoke(runner, "plan", "--page-size", "200")  # CLI wins over config
    assert api.list_projects.calls[-1].request.url.params["page_size"] == "200"


def test_config_file_explicit_path_and_rejections(router, runner, tmp_path):
    _one(router)
    lst = write_list(tmp_path, ["acme/a"])
    good = tmp_path / "custom.toml"
    good.write_text(f'list = "{lst}"\n')
    res = runner.invoke(main, ["--config", str(good), "plan", "--no-report"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    bad_yes = tmp_path / "yes.toml"
    bad_yes.write_text(f'list = "{lst}"\n[apply]\nyes = true\n')
    res = runner.invoke(main, ["--config", str(bad_yes), "plan", "--no-report"], catch_exceptions=False)
    assert res.exit_code == 2 and "'yes' is not allowed" in res.stderr
    bad_key = tmp_path / "key.toml"
    bad_key.write_text('slgu = "typo"\n')
    res = runner.invoke(main, ["--config", str(bad_key), "plan", "--no-report"], catch_exceptions=False)
    assert res.exit_code == 2 and "unknown key 'slgu'" in res.stderr


# --- default report -----------------------------------------------------------------------
def test_report_written_by_default(router, runner, tmp_path, monkeypatch):
    _one(router, on=(True, True))
    lst = write_list(tmp_path, ["acme/a"])
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(main, ["verify", "--slug", SLUG, "--list", str(lst)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    reports = list(tmp_path.glob("sms-reconcile-verify-*.json"))
    assert len(reports) == 1 and f"report: {reports[0]}" in res.stderr
    assert json.loads(reports[0].read_text())["status"] == "completed"
    res = runner.invoke(main, ["verify", "--slug", SLUG, "--list", str(lst), "--no-report"], catch_exceptions=False)
    assert len(list(tmp_path.glob("sms-reconcile-verify-*.json"))) == 1


# --- interactive confirmation --------------------------------------------------------------
def _tty(monkeypatch):
    monkeypatch.setattr("sms_reconcile.cli._interactive", lambda: True)


def test_apply_prompts_in_terminal_and_applies_on_yes(router, runner, tmp_path, monkeypatch):
    api = _one(router)
    _tty(monkeypatch)
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--slug", SLUG, "--list", str(lst), input="y\n")
    assert res.exit_code == 0, res.output
    assert "Apply 1 change(s) to deployment 'acme' (1 enable, 0 disable)?" in res.stdout
    assert api.toggle.call_count == 1


def test_apply_prompt_declined_changes_nothing(router, runner, tmp_path, monkeypatch):
    api = _one(router)
    _tty(monkeypatch)
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--slug", SLUG, "--list", str(lst), "--report", str(tmp_path / "r.json"), input="n\n")
    assert res.exit_code == 2 and "not confirmed" in res.stderr
    assert api.mutating_call_count == 0
    assert json.loads((tmp_path / "r.json").read_text())["status"] == "not-confirmed"


def test_apply_non_interactive_still_needs_yes(router, runner, tmp_path):
    api = _one(router)
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 2 and "--yes" in res.stderr and router.calls.call_count == 0


def test_apply_yes_skips_prompt_in_terminal(router, runner, tmp_path, monkeypatch):
    api = _one(router)
    _tty(monkeypatch)
    lst = write_list(tmp_path, ["acme/a"])
    res = invoke(runner, "apply", "--yes", "--slug", SLUG, "--list", str(lst))
    assert res.exit_code == 0 and "Apply 1 change" not in res.stdout and api.toggle.call_count == 1


# --- whoami / init ---------------------------------------------------------------------------
def test_whoami_ok(router, runner):
    _one(router)
    res = runner.invoke(main, ["whoami"], catch_exceptions=False)
    assert res.exit_code == 0 and f"slug={SLUG} id={DEPLOYMENT_ID}" in res.stdout and "Web API" in res.stdout


def test_whoami_explains_missing_scope(router, runner):
    router.get(f"{BASE}/api/v1/deployments").mock(return_value=httpx.Response(404, json={"error": "not found"}))
    res = runner.invoke(main, ["whoami"], catch_exceptions=False)
    assert res.exit_code == 1 and "lacks the 'Web API' scope" in res.stderr


def test_init_writes_config_once(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(main, ["init"], catch_exceptions=False)
    assert res.exit_code == 0 and (tmp_path / "sms-reconcile.toml").exists()
    import tomllib

    cfg = tomllib.loads((tmp_path / "sms-reconcile.toml").read_text())
    assert cfg["list"] == "repos.csv" and cfg["mode"] == "include" and "max_disable" not in cfg.get("apply", {})
    res = runner.invoke(main, ["init"], catch_exceptions=False)
    assert res.exit_code == 2 and "already exists" in res.stderr
    res = runner.invoke(main, ["init", "--force"], catch_exceptions=False)
    assert res.exit_code == 0
