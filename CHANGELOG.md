# Changelog

## 0.1.0 (2026-09-17)

First release.

- `plan`, `apply`, `verify` commands reconciling Semgrep Managed Scan settings
  (diff-aware and full scans) against a repository list.
- Include mode (list is the complete desired state) and exclude mode (list is
  what to turn off, everything else untouched).
- List ingestion from CSV/TSV, JSON (plain lists, object lists, API envelopes,
  JSON Lines) and plain text; clone/web URLs reduced to project paths,
  including Azure DevOps `_git` and `v3/` forms.
- Matching by full Semgrep project name or bare repository name, case
  insensitive, with ambiguity detection.
- Scope limited to projects with a Managed Scan configuration; CI-only
  projects are never changed.
- Idempotent: current settings are read in bulk before any change, and
  re-read afterwards to verify.
- Mass-disable guard on `apply` (empty list, or more than 20% of managed
  projects) with `--max-disable` / `--allow-mass-disable` overrides.
- Stable per-project v1 endpoint by default; experimental v2 bulk endpoint
  behind `--bulk`.
- Retries with `Retry-After` support, jittered backoff, and a shared pause
  across workers on 429.
- JSON run report, written even on failure or interrupt; token never logged.
