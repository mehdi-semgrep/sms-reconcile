"""sms-reconcile command line interface."""

from __future__ import annotations

import logging
import math
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import click

from . import __version__
from .client import TOKEN_ENV, ApiError, Deployment, RetryPolicy, SemgrepClient
from .executor import apply_bulk, apply_v1, find_drift
from .logging_utils import NamePolicy, configure_logging, install_redaction
from .planner import DISABLE, ENABLE, EXCLUDED, MODES, NOT_FOUND, NOT_MANAGED, UNLISTED, Plan, build_plan, load_list
from .report import RunReport, render_summary, render_table
from .sources import FORMATS, ListFormatError

log = logging.getLogger("sms_reconcile")

EXIT_OK = 0
EXIT_DRIFT_OR_FAILURE = 1
EXIT_USAGE = 2
EXIT_GUARD = 3
EXIT_INTERRUPTED = 130

# Mass-disable guard defaults: refuse to disable more than 20% of managed
# projects, and never fewer than 10, unless the operator raises the limit.
GUARD_FRACTION = 0.20
GUARD_FLOOR = 10

CONFIG_FILENAME = "sms-reconcile.toml"

# Config-file keys -> click parameter names. Anything not listed is rejected,
# and `yes` is deliberately absent: confirmation cannot come from a file.
CONFIG_KEYS = {
    "slug": "slug",
    "list": "list_path",
    "list_format": "list_format",
    "list_field": "list_field",
    "mode": "mode",
    "match": "match",
    "exclude_pattern": "exclude_patterns",
    "exclude_patterns": "exclude_patterns",
    "allow_ambiguous": "allow_ambiguous",
    "only_changes": "only_changes",
    "report": "report_path",
    "no_report": "no_report",
    "concurrency": "concurrency",
    "max_retries": "max_retries",
    "settings_batch_size": "settings_batch_size",
    "page_size": "page_size",
    "verbose_names": "verbose_names",
    "verbose": "verbose",
    # apply only
    "bulk": "bulk",
    "batch_size": "batch_size",
    "no_verify": "no_verify",
    "max_disable": "max_disable",
    "allow_mass_disable": "allow_mass_disable",
}
COMMANDS_WITH_OPTIONS = ("plan", "apply", "verify")


def _token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise click.UsageError(f"{TOKEN_ENV} is not set. Export a Semgrep API token with the 'Web API' scope.")
    return token


# --------------------------------------------------------------------------- config file


def _load_config(ctx: click.Context, _param: click.Parameter, value: Optional[Path]) -> Optional[Path]:
    """Eager callback: load sms-reconcile.toml (explicit or from cwd) into click's default_map."""
    path = value
    if path is None:
        candidate = Path.cwd() / CONFIG_FILENAME
        if candidate.is_file():
            path = candidate
    if path is None:
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.UsageError(f"cannot read config {path}: {exc}")

    def translate(section: dict[str, Any], where: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in section.items():
            if key == "yes":
                raise click.UsageError(f"{path}: 'yes' is not allowed in the config file; pass --yes on the command line")
            if key not in CONFIG_KEYS:
                raise click.UsageError(f"{path}: unknown key {key!r} in {where}")
            param = CONFIG_KEYS[key]
            if param == "exclude_patterns" and isinstance(val, str):
                val = [val]
            out[param] = val
        return out

    common = translate({k: v for k, v in data.items() if not isinstance(v, dict)}, "top level")
    default_map: dict[str, dict[str, Any]] = {}
    for cmd in COMMANDS_WITH_OPTIONS:
        merged = dict(common)
        merged.update(translate(data.get(cmd, {}), f"[{cmd}]"))
        default_map[cmd] = merged
    for key, val in data.items():
        if isinstance(val, dict) and key not in COMMANDS_WITH_OPTIONS:
            raise click.UsageError(f"{path}: unknown section [{key}]")
    ctx.default_map = default_map
    ctx.obj = {"config_path": path}
    return path


# --------------------------------------------------------------------------- options


common_options = [
    click.option("--slug", default=None, help="Deployment slug. Optional: resolved from the token when it can reach exactly one deployment."),
    click.option("--list", "list_path", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Repositories that should have Managed Scans enabled: CSV/TSV, JSON, or one name per line."),
    click.option("--list-format", type=click.Choice(list(FORMATS)), default="auto", show_default=True, help="Force the list format instead of detecting it from the extension/content."),
    click.option("--list-field", default=None, help="Column or object field holding the project name (default: auto-detect name/full_name/repo/...)."),
    click.option("--mode", type=click.Choice(list(MODES)), default="include", show_default=True, help="include: listed projects enabled, all other managed projects disabled. exclude: listed projects disabled, everything else untouched."),
    click.option("--match", type=click.Choice(["full", "repo"]), default="full", show_default=True, help="Match list entries against full Semgrep project names or bare repository names."),
    click.option("--exclude-pattern", "exclude_patterns", multiple=True, help="Glob of project names to leave untouched (repeatable), e.g. 'local_scan/*'."),
    click.option("--allow-ambiguous", is_flag=True, help="With --match repo, select every project a bare name matches instead of skipping them."),
    click.option("--only-changes", is_flag=True, help="Print only rows that need attention (hide no-op, not-managed, excluded, unlisted)."),
    click.option("--report", "report_path", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Where to write the JSON run report. Default: sms-reconcile-<command>-<UTC time>.json in the current directory."),
    click.option("--no-report", is_flag=True, help="Do not write a JSON run report."),
    click.option("--concurrency", type=click.IntRange(1, 32), default=4, show_default=True, help="Maximum concurrent API calls."),
    click.option("--max-retries", type=click.IntRange(0, 20), default=5, show_default=True, help="Retries for 429/5xx/transport errors."),
    click.option("--settings-batch-size", type=click.IntRange(1, 1000), default=200, show_default=True, help="Project ids per project_settings read."),
    click.option("--page-size", type=click.IntRange(100, 3000), default=1000, show_default=True, help="Page size for the projects listing (the API accepts 100-3000)."),
    click.option("--verbose-names", is_flag=True, help="Allow project names in log lines (default: ids only)."),
    click.option("-v", "--verbose", count=True, help="-v for INFO, -vv for DEBUG logging."),
]


def add_options(options):
    def wrap(fn):
        for opt in reversed(options):
            fn = opt(fn)
        return fn
    return wrap


# --------------------------------------------------------------------------- shared driver


class Context:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)
        configure_logging(logging.WARNING - 10 * min(self.verbose, 2))
        self.token = _token()
        install_redaction(self.token)
        self.names = NamePolicy(self.verbose_names)
        self.client = SemgrepClient(self.token, retry=RetryPolicy(max_retries=self.max_retries))
        self.list_entry_count = 0

    def resolve_deployment(self) -> Deployment:
        if self.slug:
            return self.client.resolve_deployment(self.slug)
        deployments = self.client.list_deployments()
        if len(deployments) == 1:
            log.info("no --slug given; using the only deployment this token can reach")
            return deployments[0]
        if not deployments:
            raise ApiError("the token cannot reach any deployment; check it has the 'Web API' scope")
        raise ApiError("the token reaches several deployments; pass --slug (one of: " + ", ".join(d.slug for d in deployments) + ")")

    def compute_plan(self, report: RunReport) -> tuple[Plan, Deployment]:
        if self.list_path is None:
            raise click.UsageError("--list is required (or set `list = \"...\"` in sms-reconcile.toml)")
        deployment = self.resolve_deployment()
        report.set_deployment(deployment.slug, deployment.id)
        log.info("deployment slug=%s id=%d", deployment.slug, deployment.id)
        entries = load_list(self.list_path, fmt=self.list_format, field=self.list_field)
        log.info("loaded %d list entries", len(entries))
        projects = self.client.list_projects(deployment.slug, page_size=self.page_size)
        log.info("deployment has %d projects", len(projects))
        settings = self.client.bulk_get_settings(deployment.id, [p.id for p in projects], batch_size=self.settings_batch_size)
        plan = build_plan(
            projects,
            settings,
            entries,
            match=self.match,
            exclude_patterns=list(self.exclude_patterns),
            allow_ambiguous=self.allow_ambiguous,
            mode=self.mode,
        )
        report.set_plan(plan)
        self.list_entry_count = len(entries)
        return plan, deployment

    def show(self, plan: Plan) -> None:
        click.echo(render_table(plan, only_changes=self.only_changes))
        click.echo(render_summary(plan))

    def close(self) -> None:
        self.client.close()


def _default_report_path(command: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"sms-reconcile-{command}-{stamp}.json"


def _run(command: str, mode: str, kw: dict, body: Callable[[Context, RunReport], int]) -> None:
    """Shared driver: build the context, run the body, always write the report, map errors to exit codes."""
    ctx = Context(**kw)
    report = RunReport(command, kw["slug"], None, mode=mode)
    report_path: Optional[Path] = None if kw["no_report"] else (kw["report_path"] or _default_report_path(command))
    code = EXIT_DRIFT_OR_FAILURE
    try:
        code = body(ctx, report)
        if report.data["status"] == "started":  # body may have set a more specific status
            report.set_status("completed" if code == EXIT_OK else "completed-with-problems")
    except ListFormatError as exc:
        report.set_status("failed", f"list: {exc}")
        click.echo(f"error: could not read --list: {exc}", err=True)
        code = EXIT_USAGE
    except click.UsageError as exc:
        report.set_status("failed", str(exc))
        click.echo(f"error: {exc}", err=True)
        code = EXIT_USAGE
    except ApiError as exc:
        report.set_status("failed", str(exc))
        log.error("API error: %s", exc)
        click.echo(f"error: {exc}", err=True)
    except KeyboardInterrupt:
        report.set_status("interrupted")
        click.echo("interrupted; check the report for what was applied before the interrupt", err=True)
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last line of defence, never a traceback with secrets in it
        report.set_status("failed", f"{type(exc).__name__}: {exc}")
        log.error("unexpected error: %s: %s", type(exc).__name__, exc)
        log.debug("traceback", exc_info=True)
        click.echo(f"error: {type(exc).__name__}: {exc} (run with -vv for the traceback)", err=True)
    finally:
        if report_path:
            try:
                report.write(report_path)
                click.echo(f"report: {report_path}", err=True)
            except OSError as exc:
                click.echo(f"error: could not write report: {exc}", err=True)
        ctx.close()
    sys.exit(code)


def _guard_mass_disable(plan: Plan, list_entries: int, max_disable: Optional[int], allow: bool, mode: str = "include") -> Optional[str]:
    """Return a refusal message if the plan looks like a runaway disable, else None.

    Only include mode is guarded: there a short or empty list means "disable
    almost everything". In exclude mode every disable is an explicit entry.
    """
    if allow or mode == "exclude":
        return None
    disables = len(plan.by_action(DISABLE))
    managed = sum(1 for i in plan.items if i.action not in (NOT_MANAGED, NOT_FOUND, EXCLUDED, UNLISTED))
    if list_entries == 0 and disables:
        return (
            f"the list is empty but the plan would disable {disables} project(s). "
            "An empty list disables Managed Scans everywhere; pass --allow-mass-disable if that is intended"
        )
    limit = max_disable if max_disable is not None else max(GUARD_FLOOR, math.ceil(GUARD_FRACTION * managed))
    if disables > limit:
        return (
            f"the plan would disable {disables} of {managed} managed project(s), above the limit of {limit}. "
            "Check the list is complete, then re-run with --max-disable N or --allow-mass-disable"
        )
    return None


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------- commands


@click.group()
@click.version_option(__version__, prog_name="sms-reconcile")
@click.option("--config", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, is_eager=True, expose_value=False, callback=_load_config, help=f"Config file with default options (default: ./{CONFIG_FILENAME} if present).")
def main() -> None:
    """Reconcile Semgrep Managed Scan settings with a source-of-truth repository list.

    The API token is read from the SEMGREP_APP_TOKEN environment variable only.
    Options can be set once in ./sms-reconcile.toml (see `sms-reconcile init`).
    """


@main.command()
@add_options(common_options)
def plan(**kw) -> None:
    """Show what apply would do. Read-only; exits 0."""

    def body(ctx: Context, report: RunReport) -> int:
        plan_, _ = ctx.compute_plan(report)
        ctx.show(plan_)
        msg = _guard_mass_disable(plan_, ctx.list_entry_count, None, False, ctx.mode)
        if msg:
            click.echo(f"note: apply would refuse this plan by default: {msg}", err=True)
        return EXIT_OK

    _run("plan", "read-only", kw, body)


@main.command()
@add_options(common_options)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt. Required when not running in a terminal (CI, cron).")
@click.option("--bulk", is_flag=True, help="EXPERIMENTAL: use the v2 bulk endpoint in batches instead of one v1 call per project.")
@click.option("--batch-size", type=click.IntRange(1, 500), default=50, show_default=True, help="Projects per bulk PATCH (with --bulk).")
@click.option("--no-verify", is_flag=True, help="Skip the post-apply settings re-read.")
@click.option("--max-disable", type=click.IntRange(0), default=None, help=f"Refuse if the plan disables more than N projects (default: {int(GUARD_FRACTION * 100)}%% of managed projects, at least {GUARD_FLOOR}).")
@click.option("--allow-mass-disable", is_flag=True, help="Disable the mass-disable guard, including the empty-list refusal (include mode only; exclude mode is never guarded).")
def apply(yes: bool, bulk: bool, batch_size: int, no_verify: bool, max_disable: Optional[int], allow_mass_disable: bool, **kw) -> None:
    """Apply the plan. Shows it and asks for confirmation in a terminal; needs --yes otherwise.

    Exit 1 on any failure or post-apply drift, 2 if not confirmed, 3 if the safety guard trips.
    """
    if not yes and not _interactive():
        click.echo("refusing to apply without --yes when not running in a terminal (run 'plan' first to review the changes)", err=True)
        sys.exit(EXIT_USAGE)
    mode = "bulk-v2-experimental" if bulk else "per-project-v1"

    def body(ctx: Context, report: RunReport) -> int:
        plan_, deployment = ctx.compute_plan(report)
        ctx.show(plan_)
        refusal = _guard_mass_disable(plan_, ctx.list_entry_count, max_disable, allow_mass_disable, ctx.mode)
        if refusal:
            click.echo(f"refusing to apply: {refusal}", err=True)
            report.set_status("refused-by-guard", refusal)
            return EXIT_GUARD
        mutating = plan_.mutating()
        if not mutating:
            click.echo("nothing to apply")
            return EXIT_OK
        if not yes:
            enables = len(plan_.by_action(ENABLE))
            disables = len(plan_.by_action(DISABLE))
            if not click.confirm(f"Apply {len(mutating)} change(s) to deployment {deployment.slug!r} ({enables} enable, {disables} disable)?", default=False):
                click.echo("not confirmed; nothing changed", err=True)
                report.set_status("not-confirmed")
                return EXIT_USAGE
        if bulk:
            click.echo("WARNING: --bulk uses PATCH /api/agent/deployments/{id}/repos, marked EXPERIMENTAL by Semgrep", err=True)
            result = apply_bulk(ctx.client, deployment.id, plan_, report, batch_size=batch_size, names=ctx.names)
        else:
            result = apply_v1(ctx.client, deployment.slug, plan_, report, concurrency=kw["concurrency"], names=ctx.names)
        click.echo(f"applied {len(result.applied)}, failed {len(result.failed)}")
        exit_code = EXIT_OK if result.ok else EXIT_DRIFT_OR_FAILURE
        if not no_verify and result.applied:
            drift = find_drift(ctx.client, deployment.id, result.applied, batch_size=kw["settings_batch_size"])
            report.set_drift(drift)
            if drift:
                click.echo(f"post-apply verification: {len(drift)} project(s) not in desired state", err=True)
                for item in drift:
                    click.echo(f"  {item.project_id}  {item.name}  diff={item.current_diff} full={item.current_full} desired={item.desired}", err=True)
                exit_code = EXIT_DRIFT_OR_FAILURE
            else:
                click.echo("post-apply verification: all applied projects match desired state")
        return exit_code

    _run("apply", mode, kw, body)


@main.command()
@add_options(common_options)
def verify(**kw) -> None:
    """Re-read settings and report drift from the list. Exit 1 on drift, 0 when clean."""

    def body(ctx: Context, report: RunReport) -> int:
        plan_, _ = ctx.compute_plan(report)
        drift = [i for i in plan_.items if i.action in (ENABLE, DISABLE)]
        missing = plan_.by_action(NOT_FOUND)
        report.set_drift(drift)
        if drift:
            click.echo(f"DRIFT: {len(drift)} project(s) differ from the list")
            click.echo(render_table(Plan(items=drift)))
        else:
            click.echo("clean: every managed project matches the list")
        if missing:
            click.echo(f"warning: {len(missing)} list entr{'y' if len(missing) == 1 else 'ies'} not found in the deployment", err=True)
        return EXIT_DRIFT_OR_FAILURE if drift else EXIT_OK

    _run("verify", "read-only", kw, body)


@main.command()
@click.option("-v", "--verbose", count=True, help="-v for INFO, -vv for DEBUG logging.")
def whoami(verbose: int) -> None:
    """Show which deployment the token reaches and whether it has the Web API scope. Read-only."""
    configure_logging(logging.WARNING - 10 * min(verbose, 2))
    token = _token()
    install_redaction(token)
    with SemgrepClient(token) as client:
        try:
            deployments = client.list_deployments()
        except ApiError as exc:
            if exc.status_code == 404:
                click.echo("token is valid for Semgrep but lacks the 'Web API' scope (v1 API answers 404). Create a token with that scope at https://semgrep.dev/orgs/-/settings/tokens", err=True)
                sys.exit(EXIT_DRIFT_OR_FAILURE)
            click.echo(f"error: {exc}", err=True)
            sys.exit(EXIT_DRIFT_OR_FAILURE)
    if not deployments:
        click.echo("token is accepted but reaches no deployment", err=True)
        sys.exit(EXIT_DRIFT_OR_FAILURE)
    for d in deployments:
        click.echo(f"deployment: slug={d.slug} id={d.id} name={d.name!r}")
    click.echo("scope: Web API (ok for plan/apply/verify)")
    sys.exit(EXIT_OK)


INIT_TEMPLATE = '''# sms-reconcile configuration. Command-line flags override these values.
# The API token is never read from this file: export SEMGREP_APP_TOKEN instead.

# Deployment slug. Leave commented to use the only deployment the token can reach.
# slug = "your-deployment"

# Repositories that should have Managed Scans enabled (CSV/TSV, JSON or one name per line).
list = "repos.csv"

# include: the list is the complete desired state (unlisted managed projects are disabled).
# exclude: the list is what to turn off; everything else is left alone.
mode = "include"

# full: entries are full Semgrep project names (org/repo, org/project/repo).
# repo: entries are bare repository names.
match = "full"

# Projects to leave untouched in either direction (globs, case-insensitive).
exclude_patterns = []

# Show only rows that need attention.
only_changes = true

# Page size for the projects listing; the API accepts 100-3000.
page_size = 1000

[apply]
# Refuse if a run would disable more than this many projects (default: 20% of managed projects, min 10).
# max_disable = 100
# bulk = false   # EXPERIMENTAL v2 endpoint
'''


@main.command()
@click.option("--force", is_flag=True, help=f"Overwrite an existing {CONFIG_FILENAME}.")
def init(force: bool) -> None:
    """Write a starter sms-reconcile.toml in the current directory."""
    path = Path.cwd() / CONFIG_FILENAME
    if path.exists() and not force:
        click.echo(f"{path} already exists (use --force to overwrite)", err=True)
        sys.exit(EXIT_USAGE)
    path.write_text(INIT_TEMPLATE, encoding="utf-8")
    click.echo(f"wrote {path}. Edit `list`, then run: sms-reconcile plan")


if __name__ == "__main__":  # pragma: no cover
    main()
