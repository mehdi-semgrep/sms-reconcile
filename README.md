# sms-reconcile

Keep a Semgrep deployment's **Managed Scan** settings in sync with a
source-of-truth list of repositories.

Projects on the list get diff-aware and full scans enabled. Every other
Managed Scan project in the deployment gets both disabled. CI-only projects
and anything matching `--exclude-pattern` are never touched. The tool works on
Semgrep project ids and names only, so it behaves identically for GitHub,
GitLab, Azure DevOps and Bitbucket deployments.

Built for deployments with thousands of projects: every run starts with a
read-only plan, applies are idempotent (already-correct projects are skipped),
429s are honoured, and every run can emit a JSON report.

## Project layout

```
src/sms_reconcile/
  cli.py            click commands: plan / apply / verify, exit codes, mass-disable guard
  client.py         httpx wrapper for the Semgrep API: retries, Retry-After, shared 429 pause
  sources.py        list ingestion: CSV/TSV, JSON, plain text, URL normalisation
  planner.py        matching (full or bare name), include/exclude modes, plan actions
  executor.py       apply via v1 per-project PATCH or experimental v2 bulk PATCH; drift check
  report.py         table rendering and the JSON run report
  logging_utils.py  token redaction, ids-only logging policy
tests/
  conftest.py       stateful fake of the Semgrep API mounted on respx
  test_cli.py       end-to-end CLI behaviour (all HTTP mocked)
  test_sources.py   list formats
  test_scale.py     5,000-project Azure DevOps-style rehearsal
  test_units.py     encoding, Retry-After parsing, shared pause
examples/repos.csv  minimal list file
.env.example        the one environment variable, with an empty value
```

## Install

Requires Python 3.10+. Dependencies are pinned (`httpx`, `click`; `pytest`
and `respx` for tests). Nothing else is contacted but `https://semgrep.dev`.

```bash
cd sms-reconcile
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
```

Or with plain pip:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Export a Semgrep API token that has the **Web API** scope. The token is read
from this environment variable only. There is no CLI flag for it, it is never
written to disk, and it is scrubbed from every log line and report.

```bash
export SEMGREP_APP_TOKEN=...   # from https://semgrep.dev/orgs/-/settings/tokens
```

## Commands

All three commands take the same core options:

| Option | Meaning |
| --- | --- |
| `--slug <slug>` | Deployment slug (Settings > General). Resolved to the numeric id via `GET /api/v1/deployments`. |
| `--list <file>` | Repositories that should have Managed Scans enabled: CSV/TSV, JSON or plain text (formats below). |
| `--list-format`, `--list-field` | Force the list format; name the column or field holding the project name. |
| `--mode include\|exclude` | `include` (default): the list is the complete desired state. `exclude`: the list is what to turn off, everything else is left alone. See below. |
| `--match full\|repo` | Match list entries against full project names (default) or bare repository names. |
| `--exclude-pattern <glob>` | Leave matching projects untouched. Repeatable, case-insensitive, e.g. `local_scan/*`. |
| `--allow-ambiguous` | With `--match repo`, select every project a bare name matches instead of skipping them. |
| `--only-changes` | Hide `no-op`, `not-managed` and `excluded` rows from the table. The summary line stays complete. |
| `--report <path>` | Write a JSON report of the run. Written even when the run fails or is interrupted. |
| `--concurrency N` | Cap on concurrent API calls (default 4). |
| `--max-retries N` | Retries for 429, 5xx and transport errors (default 5). |
| `--verbose-names` | Allow project names in log lines. By default logs carry project ids only. |
| `-v` / `-vv` | INFO / DEBUG logging. |

### Include or exclude

* `--mode include` (default). The list is the **complete** set of projects that
  should have Managed Scans. Listed projects are enabled; every other managed
  project is disabled. Use this when the list is the source of truth.
* `--mode exclude`. The list is what to **turn off**. Listed projects are
  disabled; every other project is reported as `unlisted` and never touched.
  Nothing is ever enabled in this mode. Use this to switch off a known set of
  repos without asserting anything about the rest.

The mass-disable guard only applies in include mode, where a short or empty
list silently means "disable almost everything". In exclude mode each disable
is an explicit entry and an empty list is simply nothing to do.

### `plan` (read-only, exit 0)

```bash
sms-reconcile plan --slug acme --list repos.csv --report plan.json
```

Prints one row per project with current state, desired state and action:

| Action | Meaning |
| --- | --- |
| `enable` | On the list, Managed Scans not fully enabled. |
| `disable` | Not on the list, Managed Scans not fully disabled. |
| `no-op` | Already in the desired state. Never PATCHed. |
| `not-managed` | No Managed Scan configuration (CI-only project). Never PATCHed. |
| `excluded` | Matches an `--exclude-pattern`. Never PATCHed. |
| `ambiguous` | A bare name in the list matches several projects. Never PATCHed unless `--allow-ambiguous`. |
| `unlisted` | Exclude mode only: not on the list, deliberately left alone. |
| `not-found-in-deployment` | A list entry that matches no project. Reported so you can fix the list. |

`plan` makes no mutating calls. It uses `GET .../projects` (paginated) and the
read-only `POST /api/sms/v2/deployments/{id}/project_settings` to learn the
current settings.

### `apply` (refuses without `--yes`)

```bash
sms-reconcile apply --yes --slug acme --list repos.csv --report apply.json
```

Recomputes the plan, prints it, then PATCHes only the `enable` / `disable`
rows. By default it uses the stable per-project endpoint
`PATCH /api/v1/deployments/{slug}/projects/{projectName}/managed-scan`, with
`--concurrency` workers. After applying it re-reads the touched projects'
settings and reports any that did not land (skip with `--no-verify`).

Exit codes: `0` everything applied and verified, `1` any failure or post-apply
drift, `2` usage error (including a missing `--yes` or token), `3` the
mass-disable guard refused the plan.

#### Mass-disable guard

The worst thing this tool can do is disable Managed Scans everywhere because
the list was empty, truncated, or pointed at the wrong deployment. `apply`
therefore refuses, with exit code `3` and nothing changed, when:

* the list has zero entries but the plan would disable anything, or
* the plan would disable more than 20% of managed projects (never fewer
  than 10, so small deployments are not blocked).

`plan` prints a note when it sees a plan that `apply` would refuse. Raise the
limit with `--max-disable N` once you have reviewed the plan, or switch the
guard off entirely with `--allow-mass-disable`.

#### `--bulk` uses an experimental endpoint

```bash
sms-reconcile apply --yes --bulk --batch-size 50 --slug acme --list repos.csv
```

> **Warning.** `--bulk` sends batches to
> `PATCH /api/agent/deployments/{deploymentId}/repos`, which Semgrep marks
> **Experimental** in the v2 API docs: it may change or break without notice.
> The response only lists `updatedRepoNames`; any project in a batch that is
> not named there is recorded as `failed`, and the post-apply verification
> still runs. Prefer the default per-project path unless you have thousands
> of changes and have tested `--bulk` against a non-production deployment.

### `verify` (read-only, exit 1 on drift)

```bash
sms-reconcile verify --slug acme --list repos.csv --report verify.json
```

Re-reads every project's settings and exits `1` if any managed project is not
in the state the list implies (in exclude mode: any listed project that is
still enabled), `0` when clean. List entries not found in the
deployment are printed as a warning but do not by themselves cause a
non-zero exit. Suitable as a scheduled drift check.

## List file formats

`--list` accepts the formats teams already have. The format is taken from the
extension (`.csv`, `.tsv`, `.json`, `.jsonl`, `.txt`, `.list`), otherwise
sniffed from the content; `--list-format csv|tsv|json|text` forces it.
Line endings may be LF, CRLF or CR, a UTF-8 BOM is tolerated, and duplicates
are collapsed case-insensitively.

**Plain text**: one repository per line. Blank lines and `#` comments are
ignored.

```text
acme/payments-api
acme/platform/web-frontend
# decommissioned: acme/legacy-api
```

**CSV / TSV**: a single column, or a header naming the project column. The
first header matching `full_name`, `nameWithOwner`, `path_with_namespace`,
`project`, `repository`, `repo`, `name`, `path` or `url` is used (most
qualified first); `--list-field <column>` overrides. A multi-column file
without a recognised header uses the first column and warns.

```csv
id,repository,owner
1,acme/payments-api,platform
```

**JSON**: any of these shapes, so the raw output of `gh repo list --json
nameWithOwner`, the GitLab projects API, or an Azure DevOps `value` envelope
can be fed in directly. When the detected name field holds a bare repo name
and the object also has a URL field (`remoteUrl`, `webUrl`, `sshUrl`,
`html_url`, `url`, ...), the URL's path is used so entries resolve to full
project names. `--list-field` picks the object field when that is not what
you want. JSON Lines works too.

```json
["acme/a", "acme/b"]
[{"nameWithOwner": "acme/a"}, {"nameWithOwner": "acme/b"}]
{"repos": ["acme/a", "acme/b"]}
{"value": [{"name": "acme/a"}], "count": 1}
```

**URLs** in any format are reduced to their path with a warning: the scheme,
host and `.git` are dropped, Azure DevOps `/_git/` is collapsed
(`https://dev.azure.com/org/proj/_git/repo` becomes `org/proj/repo`) and
web-UI suffixes such as `/-/tree/main` or `/src/main` are removed. Check the
plan for `not-found-in-deployment` rows afterwards, since the Semgrep project
name may still differ from the URL path.

### Matching

Entries are matched case-insensitively (Semgrep treats project names as
case-insensitive unique).

* `--match full` (default): entries are full Semgrep project names as shown on
  the Projects page, e.g. `org/repo` on GitHub or `org/project/repo` on Azure
  DevOps. Leading and trailing slashes are ignored.
* `--match repo`: entries are bare repository names and are compared with the
  last path segment of each project name. A bare name that matches more than
  one project is a warning; those projects are marked `ambiguous` and left
  alone unless you pass `--allow-ambiguous`.

Names with spaces or extra slashes are percent-encoded for the v1 endpoint
with slashes kept literal (`some-org/Some Project/some-repo` becomes
`some-org/Some%20Project/some-repo`). See the API notes for why.

## Runbook: Azure DevOps auto-enrolment at scale

Connecting an Azure DevOps project to Managed Scans enrols every repository in
it. This is how to bring a deployment with thousands of ADO repos back to the
source-of-truth list. Semgrep names ADO projects `org/project/repo`, and
project names may contain spaces.

1. Export the list straight from ADO. Its `name` field is the bare repo name,
   so the loader automatically takes `remoteUrl` instead, which carries the
   full `org/project/repo` path (percent-encoded spaces are decoded for you).
   `--list-field` overrides that choice if you need to:

   ```bash
   az repos list --organization https://dev.azure.com/<org> --project "<project>" -o json > repos.json
   ```

   Repeat per project and concatenate, or use a JSON envelope
   `{"value": [...]}`. Any of the list formats above work; the point is that
   entries resolve to full names, not bare repo names.

2. Plan, read-only, with a larger page size so 5,000 projects take 5 list
   calls instead of 50:

   ```bash
   sms-reconcile plan --slug <slug> --list repos.json --page-size 1000 --only-changes --report plan.json
   ```

   Check `not-found-in-deployment` rows first: they mean the URL path does
   not match the Semgrep project name and the list needs fixing before any
   apply.

3. Apply. The default per-project path is the stable one; `--concurrency 8`
   is a reasonable ceiling. If the plan disables more than 20% of managed
   projects the guard stops it, and that is expected on a first clean-up.
   Confirm the count matches what you reviewed, then raise the limit for that
   run only:

   ```bash
   sms-reconcile apply --yes --slug <slug> --list repos.json --page-size 1000 --concurrency 8 --max-disable 3500 --only-changes --report apply.json
   ```

   `--bulk --batch-size 100` is faster (8 calls for 800 changes instead of
   800) but uses the experimental endpoint; test it on a non-production
   deployment first.

4. Schedule `verify` so re-enrolment is caught. It exits `1` when ADO adds a
   repo that is not on the list, or someone re-enables one:

   ```bash
   sms-reconcile verify --slug <slug> --list repos.json --page-size 1000 --report verify.json
   ```

Verified behaviour at this scale is covered by a mocked rehearsal
(`tests/test_scale.py`): 5,000 ADO-style projects, 4,200 listed, 800
disabled with injected 429s across 8 workers, both apply paths, and drift
detection after a new repo appears. Live runs against Semgrep were done on a
49-project deployment; project names with spaces were exercised only in the
mocked rehearsal because the test deployment has none.

## JSON report

`--report` writes one JSON document per run with `command`, `mode`, `status`
(`completed`, `completed-with-problems`, `refused-by-guard`, `failed`,
`interrupted`), `deployment`, `summary` (per-action counts plus `applied`,
`failed`, `drift`), `warnings`, and full `plan`, `applied`, `failed`,
`skipped` and `drift` item lists. The report is written in a `finally`
block, so a failed or Ctrl-C'd apply still records what was changed before it
stopped. Reports contain project names and ids. They never contain the token.

## Scope and safety rules

* Only projects with a Managed Scan configuration are ever changed: those
  tagged `managed-scan` or reported with settings by the `project_settings`
  endpoint. Everything else is `not-managed`.
* A project whose current state cannot be read (tagged `managed-scan` but
  with no settings returned) is PATCHed to its desired state.
* `apply` never runs without `--yes`.
* The token comes only from `SEMGREP_APP_TOKEN`. Never commit a `.env`; the
  `.gitignore` blocks it.
* Default log lines identify projects by id. Pass `--verbose-names` if you need
  names in logs. httpx's own request logging is silenced because URLs embed
  project names.
* Retries honour `Retry-After` on 429 (seconds or HTTP-date) and otherwise use
  exponential backoff with jitter. A 429 seen by one worker pauses every
  worker sharing the client, so concurrency does not amplify a rate limit.
* Redirect (3xx) responses are failures, never followed, and their messages
  omit the URL so project names stay out of default logs.

## API notes (verified against the OpenAPI specs on 2026-09-17)

* v1 spec: `https://semgrep.dev/api/v1/public_v1.openapi.yaml`.
  v2 spec: `https://semgrep.dev/api/v2/openapi.yaml` (the `public_v2.openapi.yaml`
  path returns 404).
* `GET /api/v1/deployments/{slug}/projects` pages with zero-based `page` and
  `page_size` (the live API rejects values outside 100-3000) and returns no
  total or cursor. The tool reads until an empty
  page rather than trusting a short page, in case the server caps `page_size`.
* The projects list does not include Managed Scan settings, only `tags`.
  Current state comes from `POST /api/sms/v2/deployments/{id}/project_settings`
  (`projectIds` as strings), which returns an empty settings object for
  projects without Managed Scans. Because proto3 JSON may omit
  `enabled: false`, a non-empty settings object with a missing flag is read
  as disabled.
* `PATCH .../projects/{projectName}/managed-scan`: the spec declares
  `projectName` as a `style: simple` path parameter, which would mean `/` is
  sent as `%2F`. The live server answers a `%2F`-encoded name with a **307
  redirect** to the literal-slash path and an empty body. The tool therefore
  keeps slashes literal, treats any 3xx as a failure (it never follows
  redirects, so the bearer token cannot be replayed elsewhere), and tolerates
  the empty 2xx body the endpoint returns on success.
* The bulk endpoint takes `changes[].repoId` (string) and
  `change.managedScans.{diffScan,fullScan}` and returns `updatedRepoNames`.

## Tests

```bash
.venv/bin/python -m pytest
```

All HTTP is mocked with `respx`; the suite never contacts the network. The
GitHub Actions workflow in `.github/workflows/ci.yml` runs the suite on
Python 3.10, 3.12 and 3.13 and smoke-tests the built wheel.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `SEMGREP_APP_TOKEN` | yes | Semgrep API token with the **Web API** scope. The only way the token is accepted: no flag, no config file. Copy `.env.example` to `.env` if you use direnv or `dotenv`; `.env` is git-ignored. |

Nothing else is read from the environment, and only `https://semgrep.dev` is
ever contacted.

## Support

Run `plan` and attach the `--report` JSON when reporting a problem. Reports
contain project ids and names but never the API token. Keep the v1
per-project path for production; treat `--bulk` as a preview.
