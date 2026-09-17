from __future__ import annotations

import time
from email.utils import formatdate
from pathlib import Path

import pytest

from sms_reconcile.client import _parse_retry_after, encode_path_segment
from sms_reconcile.planner import bare_name, load_list, normalize_full


def test_encode_path_segment():
    # Slashes stay literal (the live API 307s %2F to the literal path); spaces and reserved chars are encoded.
    assert encode_path_segment("some-org/Some Project/some-repo") == "some-org/Some%20Project/some-repo"
    assert encode_path_segment("plain") == "plain"
    assert encode_path_segment("a+b&c=d") == "a%2Bb%26c%3Dd"
    assert encode_path_segment("org/repo?x#y") == "org/repo%3Fx%23y"


def test_parse_retry_after_seconds_and_http_date():
    assert _parse_retry_after("7") == 7.0
    assert _parse_retry_after(" 0 ") == 0.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("garbage") is None
    delay = _parse_retry_after(formatdate(time.time() + 5, usegmt=True))
    assert delay is not None and 3.0 <= delay <= 5.5


@pytest.mark.parametrize(
    "content,expected",
    [
        ("name\norg/a\nORG/A\n\n# comment\norg/b\n", ["org/a", "org/b"]),
        ("org/a\norg/b\n", ["org/a", "org/b"]),  # no header
        ("id,repository,owner\n1,org/a,x\n2,org/b,y\n", ["org/a", "org/b"]),  # header picks column
        ("﻿name\norg/a\n", ["org/a"]),  # BOM
    ],
)
def test_load_list(tmp_path: Path, content: str, expected: list[str]):
    p = tmp_path / "l.csv"
    p.write_text(content, encoding="utf-8")
    assert load_list(p) == expected


def test_name_normalisation():
    assert normalize_full(" /Org/Repo/ ") == "org/repo"
    assert bare_name("org/sub/Repo") == "repo"
    assert bare_name("Repo") == "repo"


def test_429_pauses_every_worker(monkeypatch):
    """A 429 seen by one request delays the next request on the shared client."""
    import httpx
    import respx

    from sms_reconcile import client as client_mod

    sleeps: list[float] = []
    monkeypatch.setattr(client_mod, "_sleep", lambda s: sleeps.append(s))
    with respx.mock(assert_all_mocked=True) as router:
        first = router.get("https://semgrep.dev/api/v1/deployments").mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json={"deployments": []})]
        )
        other = router.get("https://semgrep.dev/api/v1/deployments/x/projects").mock(return_value=httpx.Response(200, json={"projects": []}))
        c = client_mod.SemgrepClient("tok", retry=client_mod.RetryPolicy(max_retries=2))
        c.list_deployments()
        assert first.call_count == 2 and len(sleeps) == 1 and 3.9 <= sleeps[0] <= 4.0
        # The pause was consumed by the retry; an unrelated request afterwards does not sleep again.
        c.list_projects("x")
        assert other.called and len(sleeps) == 1
        # But a pause set while another worker is mid-flight is honoured by the next request.
        c._pause_all(2.0)
        c.list_projects("x")
        assert len(sleeps) == 2 and 1.9 <= sleeps[1] <= 2.0
