# Changelog

## 0.2.1 (2026-09-17)

Review fixes before wider sharing.

- Projects tagged `managed-scan` whose settings could not be read are now
  reported as `unknown-state` and left alone; `--patch-unknown` restores the
  previous behaviour of enabling/disabling them. Previously they were
  written without being called out.
- README: Python version statement corrected to 3.11+ everywhere; the Azure
  DevOps auto-enrolment behaviour is described as observed rather than
  documented; the page-size limits are labelled as live observation; the
  exclude-mode `no-op` case and the precedence of `--allow-mass-disable`
  over `--max-disable` are spelled out.

## 0.2.0 (2026-09-17)

Fewer steps to run.

- `--slug` is optional: resolved from the token when it reaches exactly one
  deployment.
- `sms-reconcile.toml` in the current directory (or `--config`) supplies
  default options; `sms-reconcile init` writes a starter. `yes` cannot come
  from the file.
- `apply` in a terminal shows the plan and asks for confirmation; `--yes` is
  only required when not interactive (CI, cron). Declining exits 2.
- A JSON report is written by default to
  `sms-reconcile-<command>-<UTC time>.json`; `--no-report` opts out.
- `sms-reconcile whoami` shows the deployment the token reaches and explains
  a missing Web API scope.
- One-line install: `uv tool install git+https://github.com/mehdi-semgrep/sms-reconcile`.
- Default `--page-size` raised to 1000. Requires Python 3.11+.

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
