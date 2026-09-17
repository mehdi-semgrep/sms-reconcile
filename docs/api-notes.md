# Semgrep API notes

Live behaviour observed while building sms-reconcile, where it differs from or adds to the OpenAPI specs.


* v1 spec: `https://semgrep.dev/api/v1/public_v1.openapi.yaml`.
  v2 spec: `https://semgrep.dev/api/v2/openapi.yaml` (the `public_v2.openapi.yaml`
  path returns 404).
* `GET /api/v1/deployments/{slug}/projects` pages with zero-based `page` and
  `page_size` and returns no total or cursor. On 2026-09-17 the live API
  rejected `page_size` outside 100-3000 with a 400; the spec does not state
  this, so it may change. The `--page-size` flag enforces the same range. The tool reads until an empty
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
