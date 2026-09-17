# sms-reconcile

Keeps a Semgrep deployment's Managed Scan settings in sync with a list of repositories. Works on Semgrep project names, so it is the same for GitHub, GitLab, Azure DevOps and Bitbucket.

## Setup

```bash
uv tool install git+https://github.com/mehdi-semgrep/sms-reconcile   # or pipx install git+https://github.com/mehdi-semgrep/sms-reconcile
export SEMGREP_APP_TOKEN=...
```

Token scope required: **Web API** (create one at semgrep.dev → Settings → Tokens). `sms-reconcile whoami` confirms the token and shows which deployment it reaches. Python 3.11+.

## Usage

```
sms-reconcile plan   --list FILE [options]     # read-only: show what would change
sms-reconcile apply  --list FILE [options]     # apply it (asks for confirmation; --yes in CI)
sms-reconcile verify --list FILE [options]     # exit 1 if the deployment has drifted from the list
```

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `--list FILE` | Repositories the list refers to (formats below) | required |
| `--mode include\|exclude` | `include`: listed repos get Managed Scans on, every other managed repo off. `exclude`: listed repos off, nothing else touched | `include` |
| `--match full\|repo` | Match on full project name (`org/repo`, `org/project/repo`) or bare repo name | `full` |
| `--exclude-pattern GLOB` | Never touch projects matching this glob (repeatable) | — |
| `--slug SLUG` | Deployment slug | resolved from the token |
| `--only-changes` | Hide rows that need no action | off |
| `--report FILE` / `--no-report` | JSON report of the run | `sms-reconcile-<cmd>-<time>.json` |
| `--concurrency N` | Parallel API calls | 4 |
| `--yes` | Skip the confirmation prompt (`apply`; required when not in a terminal) | — |
| `--max-disable N` / `--allow-mass-disable` | Raise or remove the safety limit on disables (`apply`) | 20% of managed projects, min 10 |
| `--bulk` | Use Semgrep's **experimental** bulk endpoint (`apply`) | off |
| `--patch-unknown` | Also change projects whose current settings could not be read | off |
| `--list-format`, `--list-field` | Force the list format; name the column or field holding the project name | auto |

Options can be kept in `sms-reconcile.toml` in the working directory; `sms-reconcile init` writes a starter. Flags override the file.

## List file formats

Plain text (one repo per line), CSV/TSV (single column or a header with a `name`/`repo`/`full_name`-style column), or JSON (array of strings, array of objects, or an object with a `repos`/`value`/`items` key). Clone and web URLs are reduced to the project path, including Azure DevOps `_git` URLs. Matching is case-insensitive. Raw `gh repo list --json`, GitLab API and `az repos list` output all work as-is.

## How it works

1. Lists every project in the deployment and reads their current Managed Scan settings in bulk
2. Matches the list against project names and computes an action per project: `enable`, `disable`, `no-op`, `not-managed`, `excluded`, `unlisted`, `unknown-state`, `not-found-in-deployment`
3. `apply` shows the plan, asks for confirmation, changes only the `enable`/`disable` rows, then re-reads them to verify
4. Refuses to apply if the list is empty or the plan would disable more than 20% of managed projects (include mode), unless told otherwise

Projects without a Managed Scan configuration are never changed. Projects already in the desired state are skipped. Only `semgrep.dev` is contacted; the token is never logged or written to a report.

## Examples

```bash
sms-reconcile plan --list repos.csv --only-changes
sms-reconcile apply --list repos.csv
sms-reconcile apply --list decommissioned.txt --mode exclude
sms-reconcile apply --list az-repos.json --exclude-pattern "local_scan/*" --max-disable 500 --yes
sms-reconcile verify --list repos.csv            # schedule this to catch re-enrolment
```

## Output

Table on stdout, one row per project, plus a JSON report:

```json
{
  "command": "apply",
  "status": "completed",
  "deployment": {"slug": "my-org", "id": 123},
  "summary": {"no-op": 77, "disable": 1, "excluded": 3, "applied": 1, "failed": 0, "drift": 0},
  "plan": [{"project_id": 456, "name": "my-org/payments-api", "current_diff": true, "current_full": true, "desired": false, "action": "disable"}],
  "applied": ["..."], "failed": [], "drift": []
}
```

Exit codes: `0` ok, `1` failure or drift, `2` not confirmed or usage error, `3` safety limit refused the plan.

## Notes

- `--bulk` uses an endpoint Semgrep marks experimental; prefer the default per-project path in production.
- Azure DevOps: connecting a project to Managed Scans has, in our experience, enrolled every repository in it. See [docs/azure-devops-runbook.md](docs/azure-devops-runbook.md).
- Behaviour of the Semgrep API that differs from its spec is recorded in [docs/api-notes.md](docs/api-notes.md).

## Development

```bash
git clone https://github.com/mehdi-semgrep/sms-reconcile && cd sms-reconcile
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest      # all HTTP mocked; no network needed
```
