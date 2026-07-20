"""
Merchant Agent - Quotas collector.
Fetches real account quotas instead of assuming fixed values.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from src.auth import get_credentials

logger = logging.getLogger(__name__)

QUOTAS_URL = "https://merchantapi.googleapis.com/quota/v1/accounts/{merchant_id}/quotas"


@dataclass
class QuotasResult:
    """Result of quotas collection."""
    quotas: dict = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.quotas is None:
            self.quotas = {}


def collect_quotas(merchant_id: str) -> QuotasResult:
    """Fetch current quota usage and limits."""
    result = QuotasResult()
    creds = get_credentials()

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                QUOTAS_URL.format(merchant_id=merchant_id),
                headers={"Authorization": f"Bearer {creds.token}"},
            )
            resp.raise_for_status()
            data = resp.json()

            for quota in data.get("quotaGroups", []):
                name = quota.get("name", "unknown")
                result.quotas[name] = {
                    "name": name,
                    "quota_usage": quota.get("quotaUsage", 0),
                    "quota_limit": quota.get("quotaLimit", 0),
                    "methods": [m.get("method", "") for m in quota.get("methodDetails", [])],
                }

        logger.info(f"Merchant {merchant_id}: {len(result.quotas)} quota groups fetched")

    except Exception as e:
        logger.warning(f"Failed to collect quotas for {merchant_id}: {e}")
        result.error = str(e)

    return result
