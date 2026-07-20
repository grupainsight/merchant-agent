"""
Merchant Agent - Product health collector.
Calls Merchant API to get product statuses and issues.

Flow:
1. products.list with pageSize=250 (gets productStatus + productAttributes)
2. Normalize to merchant_product_status schema (status_per_reporting_context)
3. Return normalized records + aggregate metrics

Uses REST API directly (no Python client library for Merchant API v1).
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

from src.auth import get_credentials

logger = logging.getLogger(__name__)

PRODUCTS_URL = "https://merchantapi.googleapis.com/products/v1/accounts/{merchant_id}/products"
PAGE_SIZE = 250


@dataclass
class ProductHealthResult:
    """Result of product health collection."""
    products: list[dict] = field(default_factory=list)
    total: int = 0
    approved: int = 0
    disapproved: int = 0
    pending: int = 0
    issues_by_code: dict = field(default_factory=dict)
    error: Optional[str] = None


def collect_product_health(merchant_id: str) -> ProductHealthResult:
    """
    Fetch all products with their statuses from Merchant API.
    Paginates through all results.
    Returns normalized records ready for Supabase.
    """
    result = ProductHealthResult()
    creds = get_credentials()

    try:
        all_products = _fetch_all_products(merchant_id, creds.token)
        result.products = [_normalize_product(p, merchant_id) for p in all_products]
        result.total = len(result.products)

        for p in result.products:
            status = _classify_product(p)
            if status == "approved":
                result.approved += 1
            elif status == "disapproved":
                result.disapproved += 1
            else:
                result.pending += 1

            for issue in p.get("issues", []):
                code = issue.get("code", "unknown")
                if code not in result.issues_by_code:
                    result.issues_by_code[code] = {
                        "code": code,
                        "description": issue.get("description", ""),
                        "severity": issue.get("severity", ""),
                        "count": 0,
                        "product_ids": [],
                    }
                result.issues_by_code[code]["count"] += 1
                if len(result.issues_by_code[code]["product_ids"]) < 10:
                    result.issues_by_code[code]["product_ids"].append(p["product_id"])

        logger.info(
            f"Merchant {merchant_id}: {result.total} products "
            f"({result.approved} approved, {result.disapproved} disapproved, {result.pending} pending), "
            f"{len(result.issues_by_code)} issue types"
        )

    except Exception as e:
        logger.error(f"Failed to collect product health for {merchant_id}: {e}")
        result.error = str(e)

    return result


def _fetch_all_products(merchant_id: str, token: str) -> list[dict]:
    """Paginate through all products."""
    all_products = []
    page_token = None

    with httpx.Client(timeout=60) as client:
        while True:
            params = {"pageSize": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token

            resp = client.get(
                PRODUCTS_URL.format(merchant_id=merchant_id),
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            products = data.get("products", [])
            all_products.extend(products)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

            logger.debug(f"Fetched {len(all_products)} products so far, next page...")

    return all_products


def _normalize_product(raw: dict, merchant_id: str) -> dict:
    """
    Normalize raw Merchant API product to our Supabase schema.
    Maps destinationStatuses to status_per_reporting_context.
    """
    attrs = raw.get("productAttributes", {})
    status = raw.get("productStatus", {})
    today = date.today().isoformat()

    # Build status_per_reporting_context from destinationStatuses
    status_per_ctx = []
    for dest in status.get("destinationStatuses", []):
        ctx = dest.get("reportingContext", "UNKNOWN")
        approved = dest.get("approvedCountries", [])
        disapproved = dest.get("disapprovedCountries", [])
        pending = dest.get("pendingCountries", [])

        for country in approved:
            status_per_ctx.append({
                "reporting_context": ctx,
                "country_code": country,
                "status": "APPROVED",
            })
        for country in disapproved:
            status_per_ctx.append({
                "reporting_context": ctx,
                "country_code": country,
                "status": "NOT_ELIGIBLE_OR_DISAPPROVED",
            })
        for country in pending:
            status_per_ctx.append({
                "reporting_context": ctx,
                "country_code": country,
                "status": "PENDING",
            })

    # Normalize issues
    issues = []
    for issue in status.get("itemLevelIssues", []):
        issues.append({
            "code": issue.get("code", ""),
            "severity": issue.get("severity", ""),
            "resolution": issue.get("resolution", ""),
            "attribute": issue.get("attribute", ""),
            "reporting_context": issue.get("reportingContext", ""),
            "description": issue.get("description", ""),
            "detail": issue.get("detail", ""),
            "applicable_countries": issue.get("applicableCountries", []),
        })

    # Extract price
    price_obj = attrs.get("price", {})
    price_micros = price_obj.get("amountMicros")
    price = int(price_micros) / 1_000_000 if price_micros else None
    currency = price_obj.get("currencyCode")

    # Extract GTIN
    gtins = attrs.get("gtins", [])
    gtin = gtins[0] if gtins else None

    return {
        "client_id": None,  # Set by caller
        "merchant_id": merchant_id,
        "product_id": raw.get("name", ""),
        "offer_id": raw.get("offerId", ""),
        "channel": "online",
        "content_language": raw.get("contentLanguage", ""),
        "feed_label": raw.get("feedLabel", ""),
        "title": attrs.get("title", ""),
        "gtin": gtin,
        "product_url": attrs.get("link", ""),
        "status_per_reporting_context": status_per_ctx,
        "issues": issues,
        "price": price,
        "currency": currency,
        "availability": attrs.get("availability", ""),
        "snapshot_date": today,
    }


def _classify_product(product: dict) -> str:
    """
    Classify product as approved/disapproved/pending.
    Disapproved if ANY reporting context (except FREE_LOCAL_LISTINGS) is disapproved.
    """
    dominated_contexts = {"FREE_LOCAL_LISTINGS"}

    has_approval = False
    has_disapproval = False

    for ctx in product.get("status_per_reporting_context", []):
        if ctx["reporting_context"] in dominated_contexts:
            continue
        if ctx["status"] == "NOT_ELIGIBLE_OR_DISAPPROVED":
            has_disapproval = True
        elif ctx["status"] == "APPROVED":
            has_approval = True

    if has_disapproval:
        return "disapproved"
    elif has_approval:
        return "approved"
    return "pending"
