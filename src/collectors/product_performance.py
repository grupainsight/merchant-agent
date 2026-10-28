"""
Merchant Agent - Weekly product performance collector (paid / free per offer).

Two Reports API v1 queries per Monday-Sunday week, both paginated, never one
call per offer:
  Q1  offer_id x marketing_method -> impressions, clicks   (per-offer rows)
  Q2  marketing_method            -> impressions, clicks   (channel totals)
The API leaves part of the traffic out when it splits by offer, so Q2 is the
complete total and Q1 a lower bound per offer; both are kept (contract
autonoma_contracts.merchant_performance). Any HTTP error fails the whole week:
a partial week is never returned.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from src.auth import get_credentials
from src.collectors.performance import REPORTS_URL, _error_message

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000
METHODS = ("ADS", "ORGANIC")


class WeekFetchError(RuntimeError):
    """The week could not be read completely."""


@dataclass
class WeekLoad:
    period_start: date
    period_end: date
    # {(offer_id, method): [impressions, clicks]}
    offers: dict = field(default_factory=dict)
    # {method: [impressions, clicks]} from Q2
    totals: dict = field(default_factory=lambda: {m: [0, 0] for m in METHODS})
    rows_without_offer: int = 0
    rows_unknown_method: int = 0
    api_calls: int = 0

    def by_offer(self, method: str) -> tuple[int, int]:
        impressions = sum(v[0] for (_, m), v in self.offers.items() if m == method)
        clicks = sum(v[1] for (_, m), v in self.offers.items() if m == method)
        return impressions, clicks


def offer_query(start: date, end: date) -> str:
    return (
        "SELECT offer_id, marketing_method, impressions, clicks "
        "FROM product_performance_view "
        f"WHERE date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )


def totals_query(start: date, end: date) -> str:
    return (
        "SELECT marketing_method, impressions, clicks "
        "FROM product_performance_view "
        f"WHERE date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )


def fetch_week(merchant_id: str, period_start: date) -> WeekLoad:
    """Read one Monday-Sunday week completely or raise WeekFetchError."""
    if period_start.weekday() != 0:
        raise ValueError(f"{period_start} is not a Monday")
    load = WeekLoad(period_start=period_start, period_end=period_start + timedelta(days=6))
    token = get_credentials().token
    with httpx.Client(timeout=120) as client:
        for row in _search(client, token, merchant_id, offer_query(load.period_start, load.period_end), load):
            method = row.get("marketingMethod")
            offer_id = row.get("offerId") or ""
            if method not in METHODS:
                load.rows_unknown_method += 1
                continue
            if not offer_id:
                load.rows_without_offer += 1
                continue
            value = load.offers.setdefault((offer_id, method), [0, 0])
            value[0] += int(row.get("impressions", 0) or 0)
            value[1] += int(row.get("clicks", 0) or 0)
        for row in _search(client, token, merchant_id, totals_query(load.period_start, load.period_end), load):
            method = row.get("marketingMethod")
            if method in METHODS:
                load.totals[method][0] += int(row.get("impressions", 0) or 0)
                load.totals[method][1] += int(row.get("clicks", 0) or 0)
    logger.info(
        f"Week {load.period_start}: {len(load.offers)} offer rows, totals {load.totals}, "
        f"{load.api_calls} API calls"
    )
    return load


def _search(client: httpx.Client, token: str, merchant_id: str, query: str, load: WeekLoad):
    page_token = None
    while True:
        body = {"query": query, "pageSize": PAGE_SIZE}
        if page_token:
            body["pageToken"] = page_token
        resp = client.post(
            REPORTS_URL.format(merchant_id=merchant_id),
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        load.api_calls += 1
        if resp.status_code != 200:
            raise WeekFetchError(f"week {load.period_start}: HTTP {resp.status_code}: {_error_message(resp)}")
        data = resp.json()
        for item in data.get("results", []):
            yield item.get("productPerformanceView", {})
        page_token = data.get("nextPageToken")
        if not page_token:
            return
