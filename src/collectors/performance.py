"""
Merchant Agent - Performance collector.
Uses Merchant Reports API to get product performance data.

Queries product_performance_view for impressions, clicks, CTR.
Note: Reports only work from individual subaccounts, not MCA directly.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.auth import get_credentials

logger = logging.getLogger(__name__)

REPORTS_URL = "https://merchantapi.googleapis.com/reports/v1/accounts/{merchant_id}/reports:search"


@dataclass
class PerformanceResult:
    """Result of performance collection."""
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    period_days: int = 7
    error: Optional[str] = None


def collect_performance(merchant_id: str, days: int = 7) -> PerformanceResult:
    """
    Fetch aggregated performance metrics for the last N days.
    Uses product_performance_view from Merchant Reports API.
    """
    result = PerformanceResult(period_days=days)
    creds = get_credentials()

    query = f"""
        SELECT
            metrics.impressions,
            metrics.clicks,
            metrics.ctr
        FROM product_performance_view
        WHERE segments.date BETWEEN '{_days_ago(days)}' AND '{_days_ago(1)}'
    """

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                REPORTS_URL.format(merchant_id=merchant_id),
                headers={"Authorization": f"Bearer {creds.token}"},
                json={"query": query},
            )
            resp.raise_for_status()
            data = resp.json()

            for row in data.get("results", []):
                metrics = row.get("metrics", {})
                result.impressions += int(metrics.get("impressions", 0))
                result.clicks += int(metrics.get("clicks", 0))

            if result.impressions > 0:
                result.ctr = round(result.clicks / result.impressions, 4)

        logger.info(
            f"Merchant {merchant_id}: {result.impressions} impressions, "
            f"{result.clicks} clicks, {result.ctr:.2%} CTR (last {days} days)"
        )

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.warning(f"Performance data not available for {merchant_id} (possibly no Shopping campaigns)")
            result.error = "no_data"
        else:
            logger.error(f"Failed to collect performance for {merchant_id}: {e}")
            result.error = str(e)
    except Exception as e:
        logger.error(f"Failed to collect performance for {merchant_id}: {e}")
        result.error = str(e)

    return result


def _days_ago(n: int) -> str:
    """Return date string N days ago."""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()
