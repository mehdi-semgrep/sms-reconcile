"""Human-readable table and machine-readable JSON report."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .planner import Plan, PlanItem


def _fmt_bool(value: Optional[bool]) -> str:
    if value is None:
        return "?"
    return "on" if value else "off"


QUIET_ACTIONS = {"no-op", "not-managed", "excluded", "unlisted"}


def render_table(plan: Plan, only_changes: bool = False) -> str:
    rows = [("ID", "PROJECT", "CURRENT (diff/full)", "DESIRED", "ACTION")]
    items = [i for i in plan.items if not only_changes or i.action not in QUIET_ACTIONS]
    if only_changes and not items:
        return "(no changes; every project is already in the desired state)"
    for item in items:
        rows.append(
            (
                str(item.project_id) if item.project_id is not None else "-",
                item.name,
                f"{_fmt_bool(item.current_diff)}/{_fmt_bool(item.current_full)}",
                "-" if item.desired is None else ("on" if item.desired else "off"),
                item.action,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def render_summary(plan: Plan) -> str:
    counts = plan.summary()
    parts = [f"{action}={n}" for action, n in sorted(counts.items())]
    return "summary: " + (", ".join(parts) if parts else "no projects")


class RunReport:
    def __init__(self, command: str, deployment_slug: str, deployment_id: Optional[int], mode: str) -> None:
        self.data: dict[str, Any] = {
            "tool": "sms-reconcile",
            "command": command,
            "mode": mode,
            "status": "started",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "deployment": {"slug": deployment_slug, "id": deployment_id},
            "summary": {},
            "warnings": [],
            "plan": [],
            "applied": [],
            "failed": [],
            "skipped": [],
            "drift": [],
        }

    def set_plan(self, plan: Plan) -> None:
        self.data["plan"] = [i.to_dict() for i in plan.items]
        self.data["warnings"] = list(plan.warnings)
        self.data["summary"] = plan.summary()
        self.data["skipped"] = [i.to_dict() for i in plan.items if i.action not in ("enable", "disable")]

    def add_applied(self, item: PlanItem, detail: str = "") -> None:
        self.data["applied"].append({**item.to_dict(), "detail": detail})

    def add_failed(self, item: PlanItem, error: str) -> None:
        self.data["failed"].append({**item.to_dict(), "error": error})

    def set_drift(self, items: list[PlanItem]) -> None:
        self.data["drift"] = [i.to_dict() for i in items]

    def set_deployment(self, slug: str, deployment_id: int) -> None:
        self.data["deployment"] = {"slug": slug, "id": deployment_id}

    def set_status(self, status: str, error: Optional[str] = None) -> None:
        self.data["status"] = status
        if error:
            self.data["error"] = error

    def write(self, path: Path) -> None:
        self.data["summary"] = {
            **self.data["summary"],
            "applied": len(self.data["applied"]),
            "failed": len(self.data["failed"]),
            "drift": len(self.data["drift"]),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
