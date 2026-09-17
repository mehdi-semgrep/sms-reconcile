# Runbook: Azure DevOps auto-enrolment at scale

In our experience, connecting an Azure DevOps project to Managed Scans
enrols every repository in that project, not only the ones you intended to
scan. The Semgrep documentation describes scanning "all the repositories in
batches" after enabling, so treat this as observed behaviour that may change.
This is how to bring a deployment with many ADO repos back to the
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
   `{"value": [...]}`. Any of the list formats in the README work; the point is that
   entries resolve to full names, not bare repo names.

2. Plan, read-only (5,000 projects take 5 list calls at the default page size):

   ```bash
   sms-reconcile plan --list repos.json --only-changes
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
   sms-reconcile apply --list repos.json --concurrency 8 --max-disable 3500 --only-changes
   ```

   `--bulk --batch-size 100` is faster (8 calls for 800 changes instead of
   800) but uses the experimental endpoint; test it on a non-production
   deployment first.

4. Schedule `verify` so re-enrolment is caught. It exits `1` when ADO adds a
   repo that is not on the list, or someone re-enables one:

   ```bash
   sms-reconcile verify --list repos.json
   ```

Verified behaviour at this scale is covered by a mocked rehearsal
(`tests/test_scale.py`): 5,000 ADO-style projects, 4,200 listed, 800
disabled with injected 429s across 8 workers, both apply paths, and drift
detection after a new repo appears. Live runs against Semgrep were done on a
49-project deployment; project names with spaces were exercised only in the
mocked rehearsal because the test deployment has none.
