"""
Merchant Agent - Account issues collector.
Calls Merchant API accountissues.list for account-level problems.

These are problems affecting the entire account, not individual products:
- Account suspended
- Missing tax settings
- Website verification issues
- Data quality warnings
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

from src.auth import get_credentials

logger = logging.getLogger(__name__)

ISSUES_URL = "https://merchantapi.googleapis.com/accounts/v1/accounts/{merchant_id}/issues"


@dataclass
class AccountIssuesResult:
    """Result of account issues collection."""
    issues: list[dict] = field(default_factory=list)
    critical_count: int = 0
    error_count: int = 0
    suggestion_count: int = 0
    error: Optional[str] = None


def collect_account_issues(merchant_id: str) -> AccountIssuesResult:
    """
    Fetch account-level issues from Merchant API.
    Returns normalized records ready for Supabase.
    """
    result = AccountIssuesResult()
    creds = get_credentials()

    try:
        raw_issues = _fetch_issues(merchant_id, creds.token)
        today = date.today().isoformat()

        for raw in raw_issues:
            severity = _map_severity(raw.get("severity", ""))
            normalized = {
                "client_id": None,  # Set by caller
                "merchant_id": merchant_id,
                "issue_id": raw.get("name", ""),
                "severity": severity,
                "title": raw.get("title", ""),
                "detail": raw.get("detail", ""),
                "snapshot_date": today,
            }
            result.issues.append(normalized)

            if severity == "critical":
                result.critical_count += 1
            elif severity == "error":
                result.error_count += 1
            else:
                result.suggestion_count += 1

        logger.info(
            f"Merchant {merchant_id}: {len(result.issues)} account issues "
            f"({result.critical_count} critical, {result.error_count} errors, "
            f"{result.suggestion_count} suggestions)"
        )

    except Exception as e:
        logger.error(f"Failed to collect account issues for {merchant_id}: {e}")
        result.error = str(e)

    return result


def _fetch_issues(merchant_id: str, token: str) -> list[dict]:
    """Fetch all account issues."""
    all_issues = []
    page_token = None

    with httpx.Client(timeout=30) as client:
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            resp = client.get(
                ISSUES_URL.format(merchant_id=merchant_id),
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            issues = data.get("accountIssues", [])
            all_issues.extend(issues)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return all_issues


def _map_severity(raw_severity: str) -> str:
    """Map Merchant API severity to our schema."""
    mapping = {
        "CRITICAL": "critical",
        "ERROR": "error",
        "SUGGESTION": "suggestion",
    }
    return mapping.get(raw_severity, "suggestion")
