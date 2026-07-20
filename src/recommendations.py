"""
Merchant Agent - Recommendation manager.
Handles deduplication, lifecycle, and saving of LLM-generated recommendations.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from src.db import (
    get_active_recommendations,
    upsert_recommendation,
    add_recommendation_event,
)

logger = logging.getLogger(__name__)


def process_recommendations(
    client_id: str,
    merchant_id: str,
    raw_recommendations: list[dict],
) -> tuple[int, int, int]:
    """
    Process raw LLM recommendations: dedupe, save, track events.
    Returns (created, updated, skipped) counts.
    """
    created = 0
    updated = 0
    skipped = 0
    now = datetime.now(UTC)

    for i, raw in enumerate(raw_recommendations):
        try:
            dedupe_suffix = raw.get("dedupe_key_suffix", f"unknown_{i}")
            dedupe_key = f"merchant:{dedupe_suffix}:{merchant_id}"

            # Check existing
            existing = get_active_recommendations(client_id, dedupe_key)

            if not existing:
                # Create new
                rec_id = f"rec_{now.strftime('%Y%m%d')}_{merchant_id}_{i:03d}"
                rec = _build_recommendation(rec_id, client_id, raw, dedupe_key, now)
                upsert_recommendation(rec)
                add_recommendation_event({
                    "recommendation_id": rec_id,
                    "previous_status": "NEW",
                    "new_status": "NEW",
                    "actor_type": "AGENT",
                    "actor_id": "merchant",
                    "source": "agent",
                    "comment": "Created by Merchant Agent LLM synthesis",
                })
                created += 1
                logger.info(f"Created recommendation: {rec['title']}")

            else:
                latest = existing[0]
                status = latest.get("status", "")

                if status in ("NEW", "ACKNOWLEDGED", "APPROVED", "IN_PROGRESS"):
                    # Update existing - bump last_seen_at and occurrence_count
                    upsert_recommendation({
                        "id": latest["id"],
                        "last_seen_at": now.isoformat(),
                        "occurrence_count": latest.get("occurrence_count", 1) + 1,
                        "evidence": raw.get("evidence", latest.get("evidence", [])),
                        "updated_at": now.isoformat(),
                    })
                    updated += 1
                    logger.info(f"Updated existing recommendation: {latest.get('title', '')}")

                elif status in ("COMPLETED", "WITHDRAWN", "EXPIRED"):
                    # Reopen - create new with supersedes link
                    rec_id = f"rec_{now.strftime('%Y%m%d')}_{merchant_id}_{i:03d}"
                    rec = _build_recommendation(rec_id, client_id, raw, dedupe_key, now)
                    rec["supersedes_recommendation_id"] = latest["id"]
                    upsert_recommendation(rec)
                    add_recommendation_event({
                        "recommendation_id": rec_id,
                        "previous_status": "NEW",
                        "new_status": "NEW",
                        "actor_type": "AGENT",
                        "actor_id": "merchant",
                        "source": "agent",
                        "comment": f"Reopened (was {status}): {latest['id']}",
                    })
                    created += 1
                    logger.info(f"Reopened recommendation: {rec['title']}")

                elif status == "REJECTED":
                    # Recently rejected - skip unless 30+ days old
                    updated_at = latest.get("updated_at", "")
                    if updated_at:
                        try:
                            last_update = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                            if (now - last_update).days <= 30:
                                skipped += 1
                                logger.info(f"Skipped recently rejected: {latest.get('title', '')}")
                                continue
                        except (ValueError, TypeError):
                            pass
                    # Old rejection - reopen
                    rec_id = f"rec_{now.strftime('%Y%m%d')}_{merchant_id}_{i:03d}"
                    rec = _build_recommendation(rec_id, client_id, raw, dedupe_key, now)
                    rec["supersedes_recommendation_id"] = latest["id"]
                    upsert_recommendation(rec)
                    created += 1

        except Exception as e:
            logger.error(f"Failed to process recommendation {i}: {e}")
            skipped += 1

    return created, updated, skipped


def _build_recommendation(
    rec_id: str,
    client_id: str,
    raw: dict,
    dedupe_key: str,
    now: datetime,
) -> dict:
    """Build a full recommendation dict from raw LLM output."""
    rec_type = raw.get("type", "RECOMMENDATION")
    expires_at = None
    if rec_type == "INSIGHT":
        expires_at = (now + timedelta(days=14)).isoformat()

    return {
        "id": rec_id,
        "client_id": client_id,
        "source_agent": "merchant",
        "type": rec_type,
        "status": "NEW",
        "title": raw.get("title", "Untitled")[:200],
        "summary": raw.get("summary", "")[:1000],
        "recommended_action": raw.get("recommended_action"),
        "impact": raw.get("impact", "MEDIUM"),
        "confidence": raw.get("confidence", 70),
        "reason_codes": raw.get("reason_codes", []),
        "evidence": raw.get("evidence", []),
        "requires_approval": rec_type == "ACTION",
        "dedupe_key": dedupe_key,
        "subject_key": None,
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "occurrence_count": 1,
        "resolved_at": None,
        "supersedes_recommendation_id": None,
        "expires_at": expires_at,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
