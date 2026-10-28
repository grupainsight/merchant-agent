"""
Merchant Agent - Shopping ads eligibility for the Ads agent (M3).

Facts the Google Ads API does not have: which products can serve in Shopping
ads (SHOPPING_ADS reporting context), why the others cannot, and which products
had paid clicks in the previous weeks but are disapproved now. Paid traffic,
cost and conversions per product stay in the Ads agent's own API. Every entry
keeps the identifiers a later "approve and fix" flow needs: issue code, dedupe
key of the Merchant recommendation, fix path, product_id / offer_id / URL.
"""

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

from src.fix_paths import fix_path
from src.product_status import DISAPPROVED_STATUSES

CONTEXT = "SHOPPING_ADS"
LOOKBACK_WEEKS = 4
TOP_N = 20


def shopping_status(product: dict) -> str:
    """"disapproved", "approved", "pending" or "no_status" in SHOPPING_ADS (any country)."""
    statuses = {c.get("status") for c in product.get("status_per_reporting_context") or []
                if c.get("reporting_context") == CONTEXT}
    if not statuses:
        return "no_status"
    if statuses & DISAPPROVED_STATUSES:
        return "disapproved"
    if "APPROVED" in statuses:
        return "approved"
    return "pending"


def _disapproving_codes(product: dict) -> list[str]:
    return sorted({i.get("code") or "unknown" for i in product.get("issues") or []
                   if i.get("reporting_context") == CONTEXT and i.get("severity") == "DISAPPROVED"})


def build(
    products: list[dict],
    merchant_id: str,
    snapshot_date: str,
    paid_clicks: Optional[dict[str, int]],
    paid_weeks: list[str],
) -> dict:
    """
    products: current catalog snapshot (normalized rows).
    paid_clicks: offer_id -> ADS clicks over paid_weeks (published weeks, the last complete
    one included; clicks per offer were complete in all weeks measured so far), or None when the
    weekly performance data is not available.
    """
    status = {p["product_id"]: shopping_status(p) for p in products}
    counts = Counter(status.values())

    code_products: dict[str, int] = Counter()
    for p in products:
        if status[p["product_id"]] == "disapproved":
            code_products.update(_disapproving_codes(p))
    top_codes = [
        {"issue_code": code, "products": n, "fix_path": fix_path(code),
         "dedupe_key": f"merchant:{code}:{merchant_id}"}
        for code, n in code_products.most_common(10)
    ]

    result = {
        "snapshot_date": snapshot_date,
        "context": CONTEXT,
        "products": len(products),
        "approved": counts["approved"],
        "disapproved": counts["disapproved"],
        "pending": counts["pending"],
        "no_status": counts["no_status"],
        "top_disapproval_codes": top_codes,
        "lost_paid_visibility": None,
    }
    if paid_clicks is None:
        result["lost_paid_visibility_unavailable"] = "weekly paid traffic per product not loaded"
        return result

    lost = []
    for p in products:
        clicks = paid_clicks.get(p.get("offer_id"), 0)
        if clicks > 0 and status[p["product_id"]] == "disapproved":
            codes = _disapproving_codes(p)
            lost.append({
                "product_id": p["product_id"],
                "offer_id": p.get("offer_id"),
                "current_product_url": p.get("product_url"),
                "paid_clicks": clicks,
                "issue_codes": codes,
                "fix_paths": sorted({fix_path(c) for c in codes}),
                "dedupe_keys": [f"merchant:{c}:{merchant_id}" for c in codes],
            })
    lost.sort(key=lambda x: (-x["paid_clicks"], x["offer_id"] or ""))
    result["lost_paid_visibility"] = {
        "weeks": paid_weeks,
        "products": len(lost),
        "paid_clicks": sum(x["paid_clicks"] for x in lost),
        "top": lost[:TOP_N],
    }
    return result


def paid_weeks(last_week: date) -> list[date]:
    """The last LOOKBACK_WEEKS complete weeks, newest first."""
    return [last_week - timedelta(weeks=i) for i in range(LOOKBACK_WEEKS)]
