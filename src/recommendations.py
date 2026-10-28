"""
Merchant Agent - Recommendation manager.
Handles deduplication, lifecycle, and saving of LLM-generated recommendations.

The model chooses which problems to describe; it does not decide identity.
Every recommendation must name a subject that exists in the collected data
(a product issue code, an account issue or a fixed topic), and the dedupe key
is built from that subject with autonoma_contracts.build_dedupe_key, so the
same issue gets the same key every week. Numbers in evidence come from the
data, not from the model. Dedupe decisions use autonoma_contracts.check_dedupe.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from autonoma_contracts.dedupe import build_dedupe_key, check_dedupe
from autonoma_contracts.models import Impact, RecommendationStatus, RecommendationType

from src.fix_paths import fix_path
from src.db import (
    get_active_recommendations,
    insert_recommendation,
    update_recommendation,
    add_recommendation_event,
)

logger = logging.getLogger(__name__)

AGENT = "merchant"
TOPICS = frozenset({"performance", "catalog"})
# ACTION means "executable change after approval" in the contract; this agent
# executes nothing, so it only produces INSIGHT and RECOMMENDATION.
ALLOWED_TYPES = frozenset({RecommendationType.INSIGHT.value, RecommendationType.RECOMMENDATION.value})
INSIGHT_TTL = timedelta(days=14)

# Marks recommendations built on the corrected data (stage A, 2026-10). Older
# merchant recommendations have ids "rec_<date>_<merchant>_<n>", keys chosen by
# the model and no such evidence entry.
DATA_VERSION_EVIDENCE = {"source": "merchant_agent", "data": {
    "data_version": 2,
    "note": "deterministic issue keys, products counted once, full previous snapshot",
}}


@dataclass
class Subject:
    issue_type: str        # second segment of the dedupe key
    subject_key: str
    scope: tuple = ()
    evidence: list = field(default_factory=list)


@dataclass
class ProcessResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    invalid: list = field(default_factory=list)   # (title, reason)
    errors: list = field(default_factory=list)    # (title, error)
    retyped: list = field(default_factory=list)   # titles of ACTIONs saved as RECOMMENDATION


def new_recommendation_id() -> str:
    return f"rec_{AGENT}_{uuid.uuid4().hex}"


def resolve_subject(
    raw: dict,
    merchant_id: str,
    snapshot_date: str,
    issues_by_code: dict,
    account_issues: list[dict],
) -> Optional[Subject]:
    """Map the model's issue_code to a subject present in the data, or None."""
    code = (raw.get("issue_code") or "").strip()
    if code in issues_by_code:
        data = issues_by_code[code]
        return Subject(
            issue_type=code,
            subject_key=f"issue:{code}",
            evidence=[{"source": "merchant_product_status", "data": {
                "snapshot_date": snapshot_date,
                "issue_code": code,
                "products": data["products"],
                "occurrences": data["occurrences"],
                "disapproving_products": data.get("disapproving_products", 0),
                "severity": data.get("severity", ""),
                "by_context": data.get("by_context", {}),
                "sample_offer_ids": data.get("sample_offer_ids", []),
                "fix_path": fix_path(code),
                # The full product list of this issue is the snapshot itself: products of
                # this merchant on snapshot_date whose issues contain issue_code.
                "products_ref": {"table": "merchant_product_status", "merchant_id": merchant_id,
                                 "snapshot_date": snapshot_date, "issue_code": code},
            }}],
        )
    if code.startswith("account:"):
        wanted = code.split(":", 1)[1]
        for issue in account_issues:
            short_id = (issue.get("issue_id") or "").rstrip("/").rsplit("/", 1)[-1]
            if short_id and short_id == wanted:
                return Subject(
                    issue_type="account_issue",
                    subject_key=f"account_issue:{short_id}",
                    scope=(short_id,),
                    evidence=[{"source": "merchant_account_issues", "data": {
                        "snapshot_date": snapshot_date,
                        "issue_id": issue.get("issue_id"),
                        "severity": issue.get("severity"),
                        "title": issue.get("title"),
                    }}],
                )
        return None
    if code.startswith("topic:") and code.split(":", 1)[1] in TOPICS:
        topic = code.split(":", 1)[1]
        return Subject(issue_type=f"topic_{topic}", subject_key=f"topic:{topic}")
    return None


def _validate(raw: dict) -> Optional[str]:
    if raw.get("type") not in RecommendationType._value2member_map_:
        return f"unknown type {raw.get('type')!r}"
    if raw.get("type") not in ALLOWED_TYPES:
        return f"type {raw.get('type')} not produced by this agent"
    if raw.get("impact") not in Impact._value2member_map_:
        return f"unknown impact {raw.get('impact')!r}"
    if not (raw.get("title") or "").strip():
        return "missing title"
    return None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def process_recommendations(
    client_id: str,
    merchant_id: str,
    raw_recommendations: list[dict],
    snapshot_date: str,
    issues_by_code: dict,
    account_issues: list[dict],
    now: Optional[datetime] = None,
) -> ProcessResult:
    """
    Process raw LLM recommendations: validate, dedupe, save, track events.
    """
    now = now or datetime.now(UTC)
    result = ProcessResult()
    seen_keys: set[str] = set()

    for raw in raw_recommendations:
        title = str(raw.get("title", ""))[:200]
        if raw.get("type") == RecommendationType.ACTION.value:
            raw = {**raw, "type": RecommendationType.RECOMMENDATION.value}
            result.retyped.append(title)
            logger.warning(f"ACTION from the model saved as RECOMMENDATION: {title!r}")
        try:
            problem = _validate(raw)
            subject = None if problem else resolve_subject(raw, merchant_id, snapshot_date, issues_by_code, account_issues)
            if problem is None and subject is None:
                problem = f"issue_code {raw.get('issue_code')!r} not found in this run's data"
            if problem:
                result.invalid.append((title, problem))
                logger.warning(f"Rejected recommendation {title!r}: {problem}")
                continue

            dedupe_key = build_dedupe_key(AGENT, subject.issue_type, merchant_id, *subject.scope)
            if dedupe_key in seen_keys:
                result.invalid.append((title, f"second recommendation for {dedupe_key} in one run"))
                continue
            seen_keys.add(dedupe_key)

            existing = get_active_recommendations(client_id, dedupe_key)
            latest = existing[0] if existing else None
            decision = check_dedupe(
                dedupe_key,
                [SimpleNamespace(
                    status=RecommendationStatus(latest["status"]),
                    updated_at=_parse_ts(latest.get("updated_at")),
                )] if latest else [],
                now=now,
            )

            if decision.action == "update":
                last_seen = _parse_ts(latest.get("last_seen_at"))
                values = {"evidence": subject.evidence + [DATA_VERSION_EVIDENCE], "last_seen_at": now.isoformat()}
                # A rerun on the same day is not another occurrence.
                if not last_seen or last_seen.date() != now.date():
                    values["occurrence_count"] = (latest.get("occurrence_count") or 1) + 1
                update_recommendation(latest["id"], values)
                result.updated += 1
                logger.info(f"Updated existing recommendation {latest['id']} ({dedupe_key})")
            elif decision.action == "skip":
                result.skipped += 1
                logger.info(f"Skipped recently rejected {latest['id']} ({dedupe_key})")
            else:
                rec = _build_recommendation(new_recommendation_id(), client_id, raw, dedupe_key, subject, now)
                comment = "Created by Merchant Agent LLM synthesis"
                if decision.action == "reopen":
                    rec["supersedes_recommendation_id"] = latest["id"]
                    comment = f"Reopened (was {latest['status']}): {latest['id']}"
                insert_recommendation(rec)
                add_recommendation_event({
                    "recommendation_id": rec["id"],
                    "previous_status": "NEW",
                    "new_status": "NEW",
                    "actor_type": "AGENT",
                    "actor_id": AGENT,
                    "source": "agent",
                    "comment": comment,
                })
                result.created += 1
                logger.info(f"Created recommendation {rec['id']} ({dedupe_key}): {rec['title']}")

        except Exception as e:
            logger.error(f"Failed to process recommendation {title!r}: {e}")
            result.errors.append((title, f"{type(e).__name__}: {e}"[:300]))

    return result


def _build_recommendation(
    rec_id: str,
    client_id: str,
    raw: dict,
    dedupe_key: str,
    subject: Subject,
    now: datetime,
) -> dict:
    """Build a full recommendation dict from raw LLM output and the resolved subject."""
    rec_type = raw["type"]
    try:
        confidence = max(0, min(100, int(raw.get("confidence", 70))))
    except (TypeError, ValueError):
        confidence = 70

    return {
        "id": rec_id,
        "client_id": client_id,
        "source_agent": AGENT,
        "type": rec_type,
        "status": "NEW",
        "title": raw["title"][:200],
        "summary": (raw.get("summary") or "")[:1000],
        "recommended_action": (raw.get("recommended_action") or None) and str(raw["recommended_action"])[:1000],
        "impact": raw["impact"],
        "confidence": confidence,
        "reason_codes": raw.get("reason_codes") or [],
        "evidence": subject.evidence + [DATA_VERSION_EVIDENCE],
        "requires_approval": False,
        "dedupe_key": dedupe_key,
        "subject_key": subject.subject_key,
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "occurrence_count": 1,
        "resolved_at": None,
        "supersedes_recommendation_id": None,
        "expires_at": (now + INSIGHT_TTL).isoformat() if rec_type == "INSIGHT" else None,
        "created_at": now.isoformat(),
    }
