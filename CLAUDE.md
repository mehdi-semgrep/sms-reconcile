# sms-reconcile

CLI that reconciles Semgrep Managed Scan settings against a repository list.
Read `README.md` first; the API notes section records live behaviour that
differs from the OpenAPI spec.

## Commands

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest          # all HTTP mocked via respx; never hits the network
.venv/bin/sms-reconcile whoami
.venv/bin/sms-reconcile plan --list examples/repos.csv   # --slug optional when the token reaches one deployment
```

## Conventions

- Dependencies: stdlib, `httpx`, `click`; tests use `pytest` and `respx`. Pin versions.
- The token comes only from `SEMGREP_APP_TOKEN`. Never add a flag or file for it,
  never log it, never write it to a report. `logging_utils.install_redaction`
  scrubs it from every log record; keep that in place.
- Default log lines identify projects by id only. Names appear only behind
  `--verbose-names`. Do not put project names in exception messages that are
  logged at INFO or above.
- Only `https://semgrep.dev` may be contacted. No telemetry.
- Every mutating path must be reachable only from `apply` after the terminal
  confirmation or `--yes`; confirmation must never come from a config file, must skip
  projects already in the desired state, and must be re-read afterwards.
- When the live API contradicts the spec, encode the live behaviour in
  `tests/conftest.py`'s fake and note it in the README API notes.
