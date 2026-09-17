"""Load the source-of-truth repository list from CSV/TSV, JSON or plain text.

Every format ends up as a flat list of project-name strings. Detection order:
an explicit ``fmt``, then the file extension, then a sniff of the content.
Standard library only.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote

FORMATS = ("auto", "csv", "tsv", "json", "text")

# Keys that name a project, most-qualified first. Used for CSV headers and
# JSON object fields. Matched case-insensitively.
NAME_FIELDS = (
    "full_name",
    "fullname",
    "namewithowner",
    "path_with_namespace",
    "project_name",
    "projectname",
    "project",
    "repository",
    "repo",
    "repo_name",
    "name",
    "path",
    "url",
    "html_url",
    "web_url",
    "clone_url",
    "ssh_url",
    "remoteurl",
)
# Keys under which a top-level object may hold the list.
LIST_KEYS = ("repos", "repositories", "projects", "items", "data", "names", "list", "value", "values")

EXT_FORMATS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".txt": "text",
    ".list": "text",
    ".lst": "text",
}

_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://[^/]+/|git@[^:]+:)(.+)$", re.IGNORECASE)


class ListFormatError(ValueError):
    pass


# --------------------------------------------------------------------------- public


def load_entries(path: Path, fmt: str = "auto", field: Optional[str] = None) -> tuple[list[str], list[str]]:
    """Return ``(entries, warnings)``. Entries are de-duplicated case-insensitively, order kept."""
    raw = path.read_text(encoding="utf-8-sig")
    # Normalise every line-ending style (\\r\\n Windows, \\r classic Mac, \\n) up front.
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if fmt not in FORMATS:
        raise ListFormatError(f"unknown list format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if fmt == "auto":
        fmt = EXT_FORMATS.get(path.suffix.lower()) or _sniff(text)

    warnings: list[str] = []
    if fmt == "json":
        values = _from_structure(_parse_json(text), field, warnings)
    elif fmt in ("csv", "tsv"):
        values = _from_csv(text, "\t" if fmt == "tsv" else ",", field, warnings)
    else:
        values = _from_text(text)

    entries: list[str] = []
    seen: set[str] = set()
    url_count = 0
    for value in values:
        value = value.strip()
        if not value:
            continue
        normalised, was_url = _normalise_entry(value)
        url_count += was_url
        key = normalised.strip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(normalised)
    if url_count:
        warnings.append(f"{url_count} list entr{'y' if url_count == 1 else 'ies'} were URLs and were reduced to their path (host and .git removed); check the plan for not-found rows")
    return entries, warnings


# --------------------------------------------------------------------------- detection


def _sniff(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return "text"
    if stripped[0] in "[{":
        return "json"
    lines = [ln for ln in stripped.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return "text"
    if all("\t" in ln for ln in lines[:5]):
        return "tsv"
    if any("," in ln for ln in lines[:5]):
        return "csv"
    return "text"


# --------------------------------------------------------------------------- per-format readers


def _from_text(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _from_csv(text: str, delimiter: str, field: Optional[str], warnings: list[str]) -> list[str]:
    rows = [r for r in csv.reader(text.splitlines(), delimiter=delimiter) if r and any(c.strip() for c in r)]
    rows = [r for r in rows if not r[0].lstrip().startswith("#")]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    col = 0
    if field:
        if field.lower() not in header:
            raise ListFormatError(f"--list-field {field!r} not found in header {header}")
        col = header.index(field.lower())
        rows = rows[1:]
    else:
        matches = [f for f in NAME_FIELDS if f in header]
        if matches:
            col = header.index(matches[0])
            rows = rows[1:]
        elif len(header) > 1:
            warnings.append(f"no recognised header in {header}; using the first column")
    return [r[col] for r in rows if col < len(r)]


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # JSON Lines: one document per line.
    docs = []
    for i, line in enumerate(stripped.splitlines(), 1):
        if not line.strip():
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ListFormatError(f"invalid JSON (line {i}): {exc.msg}") from None
    return docs


# --------------------------------------------------------------------------- structure -> names


def _from_structure(data: Any, field: Optional[str], warnings: list[str]) -> list[str]:
    if data is None:
        return []
    if isinstance(data, dict):
        data = _unwrap_dict(data)
    if isinstance(data, str):
        return [data]
    if not isinstance(data, list):
        raise ListFormatError(f"expected a list of repositories, got {type(data).__name__}")
    out: list[str] = []
    chosen: Optional[str] = None
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (int, float)):
            out.append(str(item))
        elif isinstance(item, dict):
            key = _pick_field(item, field)
            if key is None:
                raise ListFormatError(f"cannot find a project name in object with keys {sorted(item)}; pass --list-field")
            value = item[key]
            if not isinstance(value, str):
                raise ListFormatError(f"field {key!r} is not a string")
            if not field and "/" not in value:
                # A bare repo name (Azure DevOps `az repos list`, GitHub `name`) cannot
                # match a full Semgrep project name. Prefer a URL field that carries
                # the full org/project/repo path when the object has one.
                url_key = _pick_url_field(item)
                if url_key is not None:
                    key, value = url_key, item[url_key]
            chosen = chosen or key
            out.append(value)
        else:
            raise ListFormatError(f"unsupported list item of type {type(item).__name__}")
    if chosen and not field:
        warnings.append(f"took project names from field {chosen!r}")
    return out


def _unwrap_dict(data: dict) -> Any:
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in LIST_KEYS:
        if key in lowered and isinstance(lowered[key], (list, dict)):
            inner = lowered[key]
            return _unwrap_dict(inner) if isinstance(inner, dict) else inner
    lists = [v for v in data.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    # A mapping of name -> anything (e.g. name: {enabled: true}) is also a list of names.
    if data and all(isinstance(k, str) and "/" in k for k in data):
        return list(data.keys())
    raise ListFormatError(f"cannot find the repository list inside object with keys {sorted(map(str, data))}; use one of {LIST_KEYS} or pass a plain list")


URL_FIELDS = ("remoteurl", "weburl", "sshurl", "html_url", "web_url", "clone_url", "ssh_url", "http_url_to_repo", "ssh_url_to_repo", "url")


def _pick_url_field(item: dict) -> Optional[str]:
    lowered = {str(k).lower(): k for k in item}
    for candidate in URL_FIELDS:
        key = lowered.get(candidate)
        if key is None or not isinstance(item[key], str):
            continue
        path, was_url = _normalise_entry(item[key])
        if was_url and "/" in path:
            return key
    return None


def _pick_field(item: dict, field: Optional[str]) -> Optional[str]:
    lowered = {str(k).lower(): k for k in item}
    if field:
        return lowered.get(field.lower())
    for candidate in NAME_FIELDS:
        # Skip keys whose value is not a string, e.g. Azure DevOps' project: {name: ...}.
        if candidate in lowered and isinstance(item[lowered[candidate]], str):
            return lowered[candidate]
    return None


def _normalise_entry(value: str) -> tuple[str, bool]:
    """Reduce a clone/web URL to its path; leave plain names untouched."""
    m = _URL_RE.match(value)
    if not m:
        return value, False
    # URLs percent-encode spaces ("Platform%20Engineering"); project names do not.
    path = unquote(m.group(1)).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    # Azure DevOps SSH form: git@ssh.dev.azure.com:v3/org/project/repo
    if path.startswith("v3/") and "ssh.dev.azure.com" in value:
        path = path[3:]
    # Azure DevOps web/clone URLs: org/project/_git/repo -> org/project/repo
    path = path.replace("/_git/", "/")
    # Bitbucket web URLs: workspace/repo/src/... -> keep the first two segments
    parts = path.split("/")
    cut = [i for i, seg in enumerate(parts) if i >= 2 and seg in ("src", "tree", "blob", "-", "browse", "commits", "pull", "pulls", "merge_requests")]
    if cut:
        parts = parts[: min(cut)]
    return "/".join(parts), True
