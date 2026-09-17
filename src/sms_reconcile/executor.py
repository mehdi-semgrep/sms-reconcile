"""Apply a plan (v1 per-project or v2 bulk) and verify the outcome."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .client import ApiError, SemgrepClient
from .logging_utils import NamePolicy
from .planner import ENABLE, Plan, PlanItem, bare_name, normalize_full
from .report import RunReport

log = logging.getLogger("sms_reconcile.executor")


@dataclass
class ApplyResult:
    applied: list[PlanItem] = field(default_factory=list)
    failed: list[tuple[PlanItem, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def apply_v1(
    client: SemgrepClient,
    slug: str,
    plan: Plan,
    report: RunReport,
    *,
    concurrency: int = 4,
    names: NamePolicy,
) -> ApplyResult:
    """One PATCH /projects/{name}/managed-scan per mutating plan item."""
    result = ApplyResult()
    items = plan.mutating()
    if not items:
        return result

    def worker(item: PlanItem) -> None:
        assert item.project_id is not None
        client.toggle_managed_scan(slug, item.name, enabled=(item.action == ENABLE))

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(worker, item): item for item in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # one bad project must never abort the run
                msg = str(exc) if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}"
                log.error("%s: %s failed: %s", names.describe(item.project_id, item.name), item.action, msg)
                result.failed.append((item, msg))
                report.add_failed(item, msg)
            else:
                log.info("%s: %s applied", names.describe(item.project_id, item.name), item.action)
                result.applied.append(item)
                report.add_applied(item, "v1 managed-scan PATCH")
    return result


def _map_updated_names(updated: list[str], batch: list[PlanItem]) -> tuple[list[PlanItem], list[PlanItem]]:
    """Map ``updatedRepoNames`` back to plan items (full name first, bare name fallback)."""
    by_full = {normalize_full(i.name): i for i in batch}
    by_bare: dict[str, list[PlanItem]] = {}
    for i in batch:
        by_bare.setdefault(bare_name(i.name), []).append(i)
    confirmed: dict[int, PlanItem] = {}
    for name in updated:
        item = by_full.get(normalize_full(name))
        if item is None:
            candidates = by_bare.get(bare_name(name), [])
            item = candidates[0] if len(candidates) == 1 else None
        if item is not None and item.project_id is not None:
            confirmed[item.project_id] = item
    unconfirmed = [i for i in batch if i.project_id not in confirmed]
    return list(confirmed.values()), unconfirmed


def apply_bulk(
    client: SemgrepClient,
    deployment_id: int,
    plan: Plan,
    report: RunReport,
    *,
    batch_size: int = 50,
    names: NamePolicy,
) -> ApplyResult:
    """EXPERIMENTAL fast path: PATCH /api/agent/deployments/{id}/repos in batches."""
    result = ApplyResult()
    items = plan.mutating()
    batch_size = max(1, batch_size)
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        changes = [(i.project_id, i.action == ENABLE) for i in batch if i.project_id is not None]
        try:
            updated = client.bulk_edit_managed_scans(deployment_id, changes)  # type: ignore[arg-type]
        except Exception as exc:  # one bad batch must never abort the run
            for item in batch:
                log.error("%s: bulk %s failed: %s", names.describe(item.project_id, item.name), item.action, exc)
                result.failed.append((item, str(exc)))
                report.add_failed(item, str(exc))
            continue
        confirmed, unconfirmed = _map_updated_names(updated, batch)
        for item in confirmed:
            log.info("%s: %s applied (bulk)", names.describe(item.project_id, item.name), item.action)
            result.applied.append(item)
            report.add_applied(item, "v2 bulk PATCH; confirmed by updatedRepoNames")
        for item in unconfirmed:
            msg = "not present in updatedRepoNames of bulk response"
            log.error("%s: %s", names.describe(item.project_id, item.name), msg)
            result.failed.append((item, msg))
            report.add_failed(item, msg)
    return result


def find_drift(client: SemgrepClient, deployment_id: int, items: list[PlanItem], *, batch_size: int = 200) -> list[PlanItem]:
    """Re-read settings for ``items`` and return those not in their desired state."""
    ids = [i.project_id for i in items if i.project_id is not None and i.desired is not None]
    if not ids:
        return []
    settings = client.bulk_get_settings(deployment_id, ids, batch_size=batch_size)
    drifted: list[PlanItem] = []
    for item in items:
        if item.project_id is None or item.desired is None:
            continue
        state = settings.get(item.project_id)
        if state is None or not state.configured:
            # Disabled-and-unconfigured is acceptable when desired is off.
            if item.desired:
                drifted.append(item)
            continue
        if state.diff_scan != item.desired or state.full_scan != item.desired:
            item.current_diff = state.diff_scan
            item.current_full = state.full_scan
            drifted.append(item)
    return drifted
