def product(pid, *, contexts=None, issues=None, offer=None, snapshot_date="2026-10-27", created_at=None, client_id="c1"):
    """A merchant_product_status row. contexts: list of (context, country, status)."""
    row = {
        "client_id": client_id,
        "merchant_id": "m1",
        "product_id": f"accounts/m1/products/{pid}",
        "offer_id": offer or str(pid),
        "status_per_reporting_context": [
            {"reporting_context": c, "country_code": cc, "status": s}
            for c, cc, s in (contexts if contexts is not None else [("SHOPPING_ADS", "PL", "APPROVED")])
        ],
        "issues": issues or [],
        "snapshot_date": snapshot_date,
    }
    if created_at:
        row["created_at"] = created_at
    return row


def issue(code, *, context="SHOPPING_ADS", countries=("PL",), severity="DISAPPROVED", description=""):
    return {
        "code": code,
        "severity": severity,
        "reporting_context": context,
        "applicable_countries": list(countries),
        "description": description or code,
    }


DISAPPROVED = [("SHOPPING_ADS", "PL", "NOT_ELIGIBLE_OR_DISAPPROVED")]
