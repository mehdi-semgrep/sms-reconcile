"""List ingestion: CSV/TSV, JSON, plain text, line endings, URLs, field selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms_reconcile.sources import ListFormatError, load_entries

EXPECTED = ["org/a", "org/b", "org/sub/c"]


def _write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    p = tmp_path / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


# --- plain text -----------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        "org/a\norg/b\norg/sub/c\n",  # LF
        "org/a\r\norg/b\r\norg/sub/c\r\n",  # CRLF
        "org/a\rorg/b\rorg/sub/c",  # classic Mac CR, no trailing newline
        "﻿org/a\n\n# comment\n  org/b  \nORG/A\norg/sub/c\n",  # BOM, blanks, comment, whitespace, dup
    ],
)
def test_text_line_endings_and_noise(tmp_path, content):
    entries, warnings = load_entries(_write(tmp_path, "repos.txt", content))
    assert entries == EXPECTED
    assert warnings == []


def test_text_without_extension_is_sniffed(tmp_path):
    entries, _ = load_entries(_write(tmp_path, "repos", "org/a\norg/b\norg/sub/c\n"))
    assert entries == EXPECTED


# --- CSV / TSV --------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,content",
    [
        ("r.csv", "name\norg/a\norg/b\norg/sub/c\n"),
        ("r.csv", "org/a\norg/b\norg/sub/c\n"),  # no header
        ("r.csv", "id,repository,owner\n1,org/a,x\n2,org/b,y\n3,org/sub/c,z\n"),  # named column
        ("r.csv", "name,full_name\nwrong,org/a\nwrong,org/b\nwrong,org/sub/c\n"),  # prefers most-qualified field
        ("r.csv", 'name\n"org/a"\n"org/b"\n"org/sub/c"\n'),  # quoted
        ("r.csv", "name\r\norg/a\r\norg/b\r\norg/sub/c\r\n"),  # CRLF csv (Excel)
        ("r.tsv", "id\tname\n1\torg/a\n2\torg/b\n3\torg/sub/c\n"),
        ("r.csv", "name\n# skipped\norg/a\norg/b\norg/sub/c\n"),
    ],
)
def test_csv_variants(tmp_path, name, content):
    entries, _ = load_entries(_write(tmp_path, name, content))
    assert entries == EXPECTED


def test_csv_unknown_header_uses_first_column_with_warning(tmp_path):
    entries, warnings = load_entries(_write(tmp_path, "r.csv", "org/a,foo\norg/b,bar\norg/sub/c,baz\n"))
    assert entries == EXPECTED
    assert warnings and "first column" in warnings[0]


def test_csv_list_field_override(tmp_path):
    p = _write(tmp_path, "r.csv", "name,target\nx,org/a\ny,org/b\nz,org/sub/c\n")
    entries, _ = load_entries(p, field="target")
    assert entries == EXPECTED
    with pytest.raises(ListFormatError, match="not found in header"):
        load_entries(p, field="nope")


def test_unknown_extension_with_commas_is_csv(tmp_path):
    entries, _ = load_entries(_write(tmp_path, "export.dat", "name,size\norg/a,1\norg/b,2\norg/sub/c,3\n"))
    assert entries == EXPECTED


# --- JSON -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data",
    [
        ["org/a", "org/b", "org/sub/c"],
        [{"name": "org/a"}, {"name": "org/b"}, {"name": "org/sub/c"}],
        [{"nameWithOwner": "org/a", "name": "a"}, {"nameWithOwner": "org/b", "name": "b"}, {"nameWithOwner": "org/sub/c", "name": "c"}],  # gh repo list --json
        [{"path_with_namespace": "org/a", "name": "a"}, {"path_with_namespace": "org/b"}, {"path_with_namespace": "org/sub/c"}],  # GitLab API
        {"repos": ["org/a", "org/b", "org/sub/c"]},
        {"value": [{"name": "org/a"}, {"name": "org/b"}, {"name": "org/sub/c"}], "count": 3},  # Azure DevOps API envelope
        {"anything": ["org/a", "org/b", "org/sub/c"]},  # single list value
        {"data": {"items": ["org/a", "org/b", "org/sub/c"]}},  # nested envelope
        {"org/a": {"enabled": True}, "org/b": {}, "org/sub/c": None},  # mapping keyed by name
    ],
)
def test_json_shapes(tmp_path, data):
    entries, _ = load_entries(_write(tmp_path, "r.json", json.dumps(data)))
    assert entries == EXPECTED


def test_json_lines(tmp_path):
    content = "\n".join(json.dumps({"name": n}) for n in EXPECTED) + "\n"
    entries, _ = load_entries(_write(tmp_path, "r.jsonl", content))
    assert entries == EXPECTED


def test_json_sniffed_without_extension(tmp_path):
    entries, _ = load_entries(_write(tmp_path, "repos", '  ["org/a","org/b","org/sub/c"]'))
    assert entries == EXPECTED


def test_json_field_override_and_errors(tmp_path):
    p = _write(tmp_path, "r.json", json.dumps([{"slug": "org/a", "name": "a"}, {"slug": "org/b", "name": "b"}, {"slug": "org/sub/c", "name": "c"}]))
    entries, _ = load_entries(p, field="slug")
    assert entries == EXPECTED
    with pytest.raises(ListFormatError, match="cannot find a project name"):
        load_entries(_write(tmp_path, "bad.json", json.dumps([{"foo": "bar"}])))
    with pytest.raises(ListFormatError, match="invalid JSON"):
        load_entries(_write(tmp_path, "broken.json", '[{"name": "x"'))
    with pytest.raises(ListFormatError, match="cannot find the repository list"):
        load_entries(_write(tmp_path, "obj.json", json.dumps({"a": 1, "b": 2})))
    with pytest.raises(ListFormatError, match="expected a list"):
        load_entries(_write(tmp_path, "num.json", "42"))


def test_json_field_choice_is_reported(tmp_path):
    _, warnings = load_entries(_write(tmp_path, "r.json", json.dumps([{"repo": "org/a"}])))
    assert any("'repo'" in w for w in warnings)


# --- URL normalisation ------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/org/a", "org/a"),
        ("https://github.com/org/a.git", "org/a"),
        ("git@github.com:org/a.git", "org/a"),
        ("https://gitlab.example.com/org/sub/c/", "org/sub/c"),
        ("https://gitlab.com/org/sub/c/-/tree/main", "org/sub/c"),
        ("https://dev.azure.com/org/project/_git/repo", "org/project/repo"),
        ("https://org@dev.azure.com/org/project/_git/repo", "org/project/repo"),
        ("https://bitbucket.org/ws/repo/src/main/", "ws/repo"),
        ("ssh://git@bitbucket.example.com:7999/proj/repo.git", "proj/repo"),
    ],
)
def test_urls_reduced_to_paths(tmp_path, url, expected):
    entries, warnings = load_entries(_write(tmp_path, "r.txt", url + "\n"))
    assert entries == [expected]
    assert warnings and "URLs" in warnings[0]


def test_plain_names_are_not_treated_as_urls(tmp_path):
    entries, warnings = load_entries(_write(tmp_path, "r.txt", "org/a\nsome-repo\n"))
    assert entries == ["org/a", "some-repo"] and warnings == []


# --- format forcing ----------------------------------------------------------------
def test_forced_format_and_bad_format(tmp_path):
    p = _write(tmp_path, "list.json", "org/a\norg/b\norg/sub/c\n")  # misnamed: actually text
    with pytest.raises(ListFormatError):
        load_entries(p)
    entries, _ = load_entries(p, fmt="text")
    assert entries == EXPECTED
    with pytest.raises(ListFormatError, match="unknown list format"):
        load_entries(p, fmt="xml")


# --- Azure DevOps specifics ---------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://dev.azure.com/contoso/Platform%20Engineering/_git/payments-api", "contoso/Platform Engineering/payments-api"),
        ("https://contoso@dev.azure.com/contoso/Platform%20Engineering/_git/payments-api", "contoso/Platform Engineering/payments-api"),
        ("https://contoso.visualstudio.com/Platform%20Engineering/_git/payments-api", "Platform Engineering/payments-api"),
        ("git@ssh.dev.azure.com:v3/contoso/Platform Engineering/payments-api", "contoso/Platform Engineering/payments-api"),
        ("https://dev.azure.com/contoso/Data%20%26%20Analytics/_git/etl", "contoso/Data & Analytics/etl"),
    ],
)
def test_ado_urls(tmp_path, url, expected):
    entries, _ = load_entries(_write(tmp_path, "r.txt", url + "\n"))
    assert entries == [expected]


def test_az_repos_list_json_with_list_field(tmp_path):
    """Raw `az repos list -o json` output: bare `name` plus remoteUrl/webUrl/sshUrl."""
    data = [
        {
            "name": "payments-api",
            "project": {"name": "Platform Engineering"},
            "remoteUrl": "https://contoso@dev.azure.com/contoso/Platform%20Engineering/_git/payments-api",
            "webUrl": "https://dev.azure.com/contoso/Platform%20Engineering/_git/payments-api",
            "sshUrl": "git@ssh.dev.azure.com:v3/contoso/Platform%20Engineering/payments-api",
            "isDisabled": False,
        }
    ]
    p = _write(tmp_path, "az.json", json.dumps(data))
    # Default: `name` is bare, so the loader falls back to the URL field and gets the full path.
    entries, warnings = load_entries(p)
    assert entries == ["contoso/Platform Engineering/payments-api"]
    assert any("'remoteUrl'" in w for w in warnings)
    for field in ("remoteUrl", "webUrl", "sshUrl"):
        assert load_entries(p, field=field)[0] == ["contoso/Platform Engineering/payments-api"], field
    # Explicit --list-field name still yields the bare name for --match repo users.
    assert load_entries(p, field="name")[0] == ["payments-api"]


def test_bare_name_kept_when_no_url_field(tmp_path):
    entries, _ = load_entries(_write(tmp_path, "r.json", json.dumps([{"name": "payments-api", "size": 3}])))
    assert entries == ["payments-api"]


def test_gh_repo_list_with_name_and_url(tmp_path):
    """`gh repo list --json name,url` gives a bare name plus a URL: the URL wins."""
    entries, _ = load_entries(_write(tmp_path, "gh.json", json.dumps([{"name": "a", "url": "https://github.com/org/a"}])))
    assert entries == ["org/a"]
