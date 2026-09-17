"""Build a reconciliation plan from the deployment's projects and the desired list.

Everything here is SCM-agnostic: it only ever sees Semgrep project ids, names
and tags.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .client import ManagedScanState, Project
from .sources import load_entries

log = logging.getLogger("sms_reconcile.planner")

MANAGED_SCAN_TAG = "managed-scan"

# Actions
ENABLE = "enable"
DISABLE = "disable"
NOOP = "no-op"
NOT_MANAGED = "not-managed"
NOT_FOUND = "not-found-in-deployment"
EXCLUDED = "excluded"
AMBIGUOUS = "ambiguous"
UNLISTED = "unlisted"  # exclude mode only: not on the list, deliberately left alone
UNKNOWN_STATE = "unknown-state"  # tagged managed-scan but the settings endpoint returned nothing

MODES = ("include", "exclude")

MUTATING_ACTIONS = {ENABLE, DISABLE}

@dataclass
class PlanItem:
    project_id: Optional[int]
    name: str
    current_diff: Optional[bool]
    current_full: Optional[bool]
    desired: Optional[bool]  # True=enable both, False=disable both, None=leave alone
    action: str
    reason: str = ""
    matched_entries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    items: list[PlanItem]
    warnings: list[str] = field(default_factory=list)

    def by_action(self, action: str) -> list[PlanItem]:
        return [i for i in self.items if i.action == action]

    def mutating(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in MUTATING_ACTIONS]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.action] = counts.get(item.action, 0) + 1
        return counts


def normalize_full(name: str) -> str:
    return name.strip().strip("/").lower()


def bare_name(name: str) -> str:
    return normalize_full(name).rsplit("/", 1)[-1]


def load_list(path: Path, fmt: str = "auto", field: Optional[str] = None) -> list[str]:
    """Read the source-of-truth list in CSV/TSV, JSON or plain-text form.

    Thin wrapper over :func:`sms_reconcile.sources.load_entries` that logs the
    loader's warnings. Duplicates are collapsed case-insensitively.
    """
    entries, warnings = load_entries(path, fmt=fmt, field=field)
    for w in warnings:
        # Field auto-detection is informational; everything else deserves attention.
        (log.info if w.startswith("took project names") else log.warning)("list: %s", w)
    return entries


def is_excluded(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


def build_plan(
    projects: list[Project],
    settings: dict[int, ManagedScanState],
    desired_entries: list[str],
    *,
    match: str = "full",
    exclude_patterns: Optional[list[str]] = None,
    allow_ambiguous: bool = False,
    mode: str = "include",
    patch_unknown: bool = False,
) -> Plan:
    """Build the plan.

    ``mode="include"``: listed projects are enabled, every other managed project
    is disabled (the list is the complete desired state).
    ``mode="exclude"``: listed projects are disabled, every other project is left
    untouched (``unlisted``).

    A project tagged ``managed-scan`` whose settings could not be read is
    reported as ``unknown-state`` and left alone unless ``patch_unknown`` is
    set, in which case it is enabled/disabled like any other managed project.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    exclude_patterns = exclude_patterns or []
    warnings: list[str] = []

    # Index projects by full and bare name (both lowercased).
    by_full: dict[str, list[Project]] = {}
    by_bare: dict[str, list[Project]] = {}
    for p in projects:
        by_full.setdefault(normalize_full(p.name), []).append(p)
        by_bare.setdefault(bare_name(p.name), []).append(p)

    for key, dupes in by_full.items():
        if len(dupes) > 1:
            warnings.append(
                f"deployment has {len(dupes)} projects whose names collide case-insensitively "
                f"({', '.join(str(d.id) for d in dupes)}); all will be treated alike"
            )

    wanted: dict[int, list[str]] = {}  # project_id -> list entries that selected it
    ambiguous_ids: set[int] = set()
    not_found: list[str] = []

    for entry in desired_entries:
        if match == "full":
            candidates = by_full.get(normalize_full(entry), [])
        else:
            if "/" in entry.strip("/"):
                warnings.append(f"list entry {entry!r} contains '/' but --match repo is set; using its last segment")
            candidates = by_bare.get(bare_name(entry), [])
            if len(candidates) > 1:
                names = ", ".join(sorted(c.name for c in candidates))
                if allow_ambiguous:
                    warnings.append(f"bare name {entry!r} matches {len(candidates)} projects ({names}); --allow-ambiguous set, all selected")
                else:
                    warnings.append(f"bare name {entry!r} is ambiguous: matches {len(candidates)} projects ({names}); left untouched")
                    ambiguous_ids.update(c.id for c in candidates)
        if not candidates:
            not_found.append(entry)
            continue
        for c in candidates:
            wanted.setdefault(c.id, []).append(entry)

    items: list[PlanItem] = []
    for p in sorted(projects, key=lambda x: x.name.lower()):
        state = settings.get(p.id, ManagedScanState(None, None, configured=False))
        managed = state.configured or MANAGED_SCAN_TAG in {t.lower() for t in p.tags}
        entries = wanted.get(p.id, [])
        listed = bool(entries)
        desired: Optional[bool] = listed if mode == "include" else (False if listed else None)

        if is_excluded(p.name, exclude_patterns):
            items.append(PlanItem(p.id, p.name, state.diff_scan, state.full_scan, None, EXCLUDED, "matches --exclude-pattern", entries))
            continue
        if p.id in ambiguous_ids and not allow_ambiguous:
            items.append(PlanItem(p.id, p.name, state.diff_scan, state.full_scan, None, AMBIGUOUS, "ambiguous bare-name match", entries))
            continue
        if not managed:
            items.append(PlanItem(p.id, p.name, state.diff_scan, state.full_scan, None, NOT_MANAGED, "no Managed Scan configuration (CI-only)", entries))
            continue
        if desired is None:  # exclude mode, not listed
            items.append(PlanItem(p.id, p.name, state.diff_scan, state.full_scan, None, UNLISTED, "not on exclude list; left untouched", entries))
            continue
        if not state.configured and not patch_unknown:
            items.append(PlanItem(p.id, p.name, None, None, desired, UNKNOWN_STATE, "tagged managed-scan but settings unreadable; pass --patch-unknown to act", entries))
            continue

        current_on = state.diff_scan is True and state.full_scan is True
        current_off = state.diff_scan is False and state.full_scan is False
        why = "on list" if (listed or mode == "include") else "not on list"
        if desired and current_on:
            action, reason = NOOP, "already enabled"
        elif desired:
            action, reason = ENABLE, why if state.configured else f"{why} (current state unknown)"
        elif current_off:
            action, reason = NOOP, "already disabled"
        else:
            why = "on exclude list" if mode == "exclude" else "not on list"
            action, reason = DISABLE, why if state.configured else f"{why} (current state unknown)"
        items.append(PlanItem(p.id, p.name, state.diff_scan, state.full_scan, desired, action, reason, entries))

    for entry in not_found:
        items.append(PlanItem(None, entry, None, None, mode == "include", NOT_FOUND, "list entry matches no project in the deployment", [entry]))
        warnings.append(f"list entry {entry!r} was not found in the deployment")

    for w in warnings:
        log.warning("%s", w)
    return Plan(items=items, warnings=warnings)
