"""
Merchant Agent - Snapshot comparison.
Compares current product status with previous week to detect changes.
Returns structured diff for deterministic checks before LLM synthesis.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SnapshotDiff:
    """Result of comparing two weekly snapshots."""
    has_significant_change: bool = False
    new_disapprovals: list[dict] = field(default_factory=list)
    resolved_disapprovals: list[dict] = field(default_factory=list)
    new_issues: list[dict] = field(default_factory=list)
    resolved_issues: list[dict] = field(default_factory=list)
    total_products_delta: int = 0
    approved_delta: int = 0
    disapproved_delta: int = 0
    summary: str = ""


def compare_snapshots(
    current: list[dict],
    previous: Optional[list[dict]],
) -> SnapshotDiff:
    """
    Compare current and previous product status snapshots.
    Returns a SnapshotDiff describing what changed.
    """
    diff = SnapshotDiff()

    if previous is None:
        # First run - everything is new, always significant
        diff.has_significant_change = True
        diff.summary = "First snapshot - no previous data to compare"
        return diff

    # Build lookup by product_id for previous snapshot
    prev_by_id = {p["product_id"]: p for p in previous}
    curr_by_id = {p["product_id"]: p for p in current}

    # Count status changes
    prev_disapproved = {
        pid for pid, p in prev_by_id.items()
        if _has_disapproval(p)
    }
    curr_disapproved = {
        pid for pid, p in curr_by_id.items()
        if _has_disapproval(p)
    }

    # New disapprovals
    new_disapproved_ids = curr_disapproved - prev_disapproved
    diff.new_disapprovals = [
        curr_by_id[pid] for pid in new_disapproved_ids
    ]

    # Resolved disapprovals
    resolved_ids = prev_disapproved - curr_disapproved
    diff.resolved_disapprovals = [
        prev_by_id[pid] for pid in resolved_ids if pid in prev_by_id
    ]

    # Deltas
    diff.total_products_delta = len(current) - len(previous)
    diff.approved_delta = (
        sum(1 for p in current if not _has_disapproval(p))
        - sum(1 for p in previous if not _has_disapproval(p))
    )
    diff.disapproved_delta = len(curr_disapproved) - len(prev_disapproved)

    # New issues (issue codes that weren't present before)
    prev_issue_keys = _extract_issue_keys(previous)
    curr_issue_keys = _extract_issue_keys(current)
    new_issue_keys = curr_issue_keys - prev_issue_keys
    if new_issue_keys:
        diff.new_issues = [{"issue_key": k} for k in new_issue_keys]

    resolved_issue_keys = prev_issue_keys - curr_issue_keys
    if resolved_issue_keys:
        diff.resolved_issues = [{"issue_key": k} for k in resolved_issue_keys]

    # Determine significance
    diff.has_significant_change = (
        len(diff.new_disapprovals) > 0
        or len(diff.resolved_disapprovals) > 0
        or abs(diff.disapproved_delta) > 0
        or len(diff.new_issues) > 0
    )

    # Build summary
    parts = []
    if diff.new_disapprovals:
        parts.append(f"{len(diff.new_disapprovals)} new disapprovals")
    if diff.resolved_disapprovals:
        parts.append(f"{len(diff.resolved_disapprovals)} resolved")
    if diff.new_issues:
        parts.append(f"{len(diff.new_issues)} new issue types")
    diff.summary = ", ".join(parts) if parts else "No significant changes"

    return diff


def _has_disapproval(product: dict) -> bool:
    """Check if product has any disapproval in any reporting context."""
    contexts = product.get("status_per_reporting_context", [])
    return any(
        c.get("status") in ("NOT_ELIGIBLE_OR_DISAPPROVED", "DISAPPROVED")
        for c in contexts
    )


def _extract_issue_keys(products: list[dict]) -> set[str]:
    """Extract unique issue keys from all products."""
    keys = set()
    for p in products:
        for issue in p.get("issues", []):
            code = issue.get("code") or issue.get("servability") or "unknown"
            keys.add(f"{p.get('product_id', 'unknown')}:{code}")
    return keys
