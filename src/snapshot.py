"""
Merchant Agent - Snapshot comparison.
Compares current product status with previous week to detect changes.
Returns structured diff for deterministic checks before LLM synthesis.
"""

from dataclasses import dataclass, field
from typing import Optional

from src.product_status import is_disapproved, issue_product_counts


@dataclass
class PreviousSnapshot:
    """
    The snapshot the current run is compared with.
    status: "ok" (complete snapshot found), "none" (no earlier run),
            "incomplete" (earlier runs exist but none has a complete snapshot),
            "error" (reading failed).
    """
    status: str
    snapshot_date: Optional[str] = None
    products: list[dict] = field(default_factory=list)
    raw_rows: int = 0
    expected_products: Optional[int] = None
    skipped_dates: list[dict] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SnapshotDiff:
    """Result of comparing two weekly snapshots."""
    comparable: bool = False
    previous_status: str = "none"
    previous_date: Optional[str] = None
    has_significant_change: bool = False
    new_disapprovals: list[dict] = field(default_factory=list)
    resolved_disapprovals: list[dict] = field(default_factory=list)
    new_issues: list[dict] = field(default_factory=list)
    resolved_issues: list[dict] = field(default_factory=list)
    issue_changes: list[dict] = field(default_factory=list)
    total_products_delta: int = 0
    approved_delta: int = 0
    disapproved_delta: int = 0
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "comparable": self.comparable,
            "previous_status": self.previous_status,
            "previous_date": self.previous_date,
            "has_significant_change": self.has_significant_change,
            "summary": self.summary,
            "new_disapprovals_count": len(self.new_disapprovals),
            "resolved_disapprovals_count": len(self.resolved_disapprovals),
            "disapproved_delta": self.disapproved_delta,
            "approved_delta": self.approved_delta,
            "total_products_delta": self.total_products_delta,
            "new_issue_codes": [i["code"] for i in self.new_issues],
            "resolved_issue_codes": [i["code"] for i in self.resolved_issues],
            "issue_changes": self.issue_changes,
        }


_NOT_COMPARABLE = {
    "none": "First snapshot - no previous data to compare",
    "incomplete": "No complete previous snapshot - comparison skipped",
    "error": "Previous snapshot could not be read - comparison skipped",
}


def compare_snapshots(
    current: list[dict],
    previous: PreviousSnapshot,
) -> SnapshotDiff:
    """
    Compare current and previous product status snapshots.
    Returns a SnapshotDiff describing what changed. Without a complete previous
    snapshot the diff is marked not comparable and reports no changes.
    """
    diff = SnapshotDiff(previous_status=previous.status, previous_date=previous.snapshot_date)

    if previous.status != "ok":
        # Nothing to compare with: synthesis still runs, on the current state only.
        diff.has_significant_change = True
        diff.summary = _NOT_COMPARABLE.get(previous.status, "Comparison skipped")
        return diff

    diff.comparable = True
    prev_by_id = {p["product_id"]: p for p in previous.products}
    curr_by_id = {p["product_id"]: p for p in current}

    prev_disapproved = {pid for pid, p in prev_by_id.items() if is_disapproved(p)}
    curr_disapproved = {pid for pid, p in curr_by_id.items() if is_disapproved(p)}

    diff.new_disapprovals = [curr_by_id[pid] for pid in curr_disapproved - prev_disapproved]
    diff.resolved_disapprovals = [prev_by_id[pid] for pid in prev_disapproved - curr_disapproved]

    diff.total_products_delta = len(curr_by_id) - len(prev_by_id)
    diff.approved_delta = (
        sum(1 for p in curr_by_id.values() if not is_disapproved(p))
        - sum(1 for p in prev_by_id.values() if not is_disapproved(p))
    )
    diff.disapproved_delta = len(curr_disapproved) - len(prev_disapproved)

    # Issue codes, counted in unique products.
    prev_counts = issue_product_counts(prev_by_id.values())
    curr_counts = issue_product_counts(curr_by_id.values())
    diff.new_issues = [{"code": c, "products": curr_counts[c]} for c in sorted(curr_counts.keys() - prev_counts.keys())]
    diff.resolved_issues = [{"code": c, "products": prev_counts[c]} for c in sorted(prev_counts.keys() - curr_counts.keys())]
    diff.issue_changes = sorted(
        (
            {"code": c, "before": prev_counts.get(c, 0), "after": curr_counts.get(c, 0)}
            for c in curr_counts.keys() | prev_counts.keys()
            if prev_counts.get(c, 0) != curr_counts.get(c, 0)
        ),
        key=lambda x: abs(x["after"] - x["before"]),
        reverse=True,
    )

    diff.has_significant_change = (
        len(diff.new_disapprovals) > 0
        or len(diff.resolved_disapprovals) > 0
        or len(diff.new_issues) > 0
    )

    parts = []
    if diff.new_disapprovals:
        parts.append(f"{len(diff.new_disapprovals)} new disapprovals")
    if diff.resolved_disapprovals:
        parts.append(f"{len(diff.resolved_disapprovals)} resolved")
    if diff.new_issues:
        parts.append(f"{len(diff.new_issues)} new issue types")
    diff.summary = ", ".join(parts) if parts else "No significant changes"
    return diff
