"""
Merchant Agent - Performance collector.
Uses Merchant Reports API to get product performance data.

Queries product_performance_view for impressions, clicks, CTR.
Note: Reports only work from individual subaccounts, not MCA directly.

Query syntax is Merchant API v1 (fields without the "metrics."/"segments."
prefixes of the old Content API). Any HTTP error is a failure, never "no data":
"no data" means the API answered 200 with no rows.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import httpx

from src.auth import get_credentials

logger = logging.getLogger(__name__)

REPORTS_URL = "https://merchantapi.googleapis.com/reports/v1/accounts/{merchant_id}/reports:search"
PAGE_SIZE = 1000


@dataclass
class PerformanceResult:
    """
    Result of performance collection.
    status: "ok" (rows returned), "no_data" (HTTP 200, no rows) or "error".
    """
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    period_days: int = 7
    date_from: str = ""
    date_to: str = ""
    rows: int = 0
    status: str = "ok"
    error: Optional[str] = None


def build_query(date_from: str, date_to: str) -> str:
    return (
        "SELECT impressions, clicks, click_through_rate "
        "FROM product_performance_view "
        f"WHERE date BETWEEN '{date_from}' AND '{date_to}'"
    )


def collect_performance(merchant_id: str, days: int = 7, today: Optional[date] = None) -> PerformanceResult:
    """
    Fetch aggregated performance metrics for the last N full days.
    Uses product_performance_view from Merchant Reports API.
    """
    today = today or date.today()
    result = PerformanceResult(
        period_days=days,
        date_from=(today - timedelta(days=days)).isoformat(),
        date_to=(today - timedelta(days=1)).isoformat(),
    )

    try:
        creds = get_credentials()
        query = build_query(result.date_from, result.date_to)
        page_token = None
        with httpx.Client(timeout=60) as client:
            while True:
                body = {"query": query, "pageSize": PAGE_SIZE}
                if page_token:
                    body["pageToken"] = page_token
                resp = client.post(
                    REPORTS_URL.format(merchant_id=merchant_id),
                    headers={"Authorization": f"Bearer {creds.token}"},
                    json=body,
                )
                if resp.status_code != 200:
                    result.status = "error"
                    result.error = f"HTTP {resp.status_code}: {_error_message(resp)}"
                    logger.error(f"Performance query failed for {merchant_id}: {result.error}")
                    return result
                data = resp.json()
                for row in data.get("results", []):
                    view = row.get("productPerformanceView", {})
                    result.impressions += int(view.get("impressions", 0) or 0)
                    result.clicks += int(view.get("clicks", 0) or 0)
                    result.rows += 1
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        if result.rows == 0:
            result.status = "no_data"
        if result.impressions > 0:
            result.ctr = round(result.clicks / result.impressions, 4)

        logger.info(
            f"Merchant {merchant_id}: {result.impressions} impressions, "
            f"{result.clicks} clicks, {result.ctr:.2%} CTR "
            f"({result.date_from}..{result.date_to}, {result.rows} rows, {result.status})"
        )

    except Exception as e:
        logger.error(f"Failed to collect performance for {merchant_id}: {e}")
        result.status = "error"
        result.error = f"{type(e).__name__}: {e}"[:500]

    return result


def _error_message(resp: httpx.Response) -> str:
    try:
        return (resp.json().get("error", {}).get("message") or resp.text)[:300]
    except ValueError:
        return resp.text[:300]
