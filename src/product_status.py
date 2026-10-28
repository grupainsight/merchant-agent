"""
Merchant Agent - Product status definitions.

Single source of truth for "approved / disapproved / pending" and for issue
counts. Both the weekly report and the snapshot comparison use these functions,
so their numbers cannot drift apart again.

Counting rules:
- a product counts once per issue code, however many contexts and countries the
  issue is reported for ("products"); the raw number of issue entries is kept
  separately as "occurrences";
- the market (country) and destination (reporting context) breakdown is kept in
  "by_context", in products per context and country.
"""

from collections import defaultdict
from typing import Iterable

# FREE_LOCAL_LISTINGS needs a local inventory feed. Without one every product is
# reported as not eligible there, which says nothing about the online offer, so
# it does not make a product "disapproved". It still appears in by_context.
EXCLUDED_CONTEXTS = frozenset({"FREE_LOCAL_LISTINGS"})

DISAPPROVED_STATUSES = frozenset({"NOT_ELIGIBLE_OR_DISAPPROVED", "DISAPPROVED"})

# Merchant API v1 itemLevelIssues severity, most severe first.
_SEVERITY_RANK = {"DISAPPROVED": 3, "DEMOTED": 2, "NOT_IMPACTED": 1}

_BUCKET_RANK = {"disapproved": 3, "approved": 2, "pending": 1}

SAMPLE_SIZE = 10
ALL_COUNTRIES = "ALL"


def classify(product: dict) -> str:
    """Return "approved", "disapproved" or "pending" for one product."""
    has_approval = False
    has_disapproval = False
    for ctx in product.get("status_per_reporting_context") or []:
        if ctx.get("reporting_context") in EXCLUDED_CONTEXTS:
            continue
        status = ctx.get("status")
        if status in DISAPPROVED_STATUSES:
            has_disapproval = True
        elif status == "APPROVED":
            has_approval = True
    if has_disapproval:
        return "disapproved"
    if has_approval:
        return "approved"
    return "pending"


def is_disapproved(product: dict) -> bool:
    return classify(product) == "disapproved"


def count_statuses(products: Iterable[dict]) -> dict:
    counts = {"approved": 0, "disapproved": 0, "pending": 0}
    for p in products:
        counts[classify(p)] += 1
    return counts


def status_by_context(products: Iterable[dict]) -> list[dict]:
    """Products per reporting context and country, by status (all contexts, none excluded)."""
    acc: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"approved": 0, "disapproved": 0, "pending": 0}
    )
    for p in products:
        seen: dict[tuple[str, str], str] = {}
        for ctx in p.get("status_per_reporting_context") or []:
            key = (ctx.get("reporting_context") or "UNKNOWN", ctx.get("country_code") or ALL_COUNTRIES)
            status = ctx.get("status")
            bucket = (
                "disapproved" if status in DISAPPROVED_STATUSES
                else "approved" if status == "APPROVED"
                else "pending"
            )
            # A product listed twice for one context/country counts once, worst status wins.
            if _BUCKET_RANK[bucket] > _BUCKET_RANK.get(seen.get(key), 0):
                seen[key] = bucket
        for key, bucket in seen.items():
            acc[key][bucket] += 1
    return [
        {"reporting_context": ctx, "country_code": country, **counts}
        for (ctx, country), counts in sorted(acc.items())
    ]


def summarize_issues(products: Iterable[dict]) -> dict[str, dict]:
    """
    Issues by code. For each code:
      products              - unique products with the issue (also kept as "count")
      occurrences           - issue entries (one product can report a code per context)
      disapproving_products - products where the code has severity DISAPPROVED in a
                              destination that counts for classify()
      by_context            - {reporting_context: {country: products}}
      severity              - most severe severity seen for the code
      sample_product_ids / sample_offer_ids - up to SAMPLE_SIZE examples
    """
    acc: dict[str, dict] = {}
    for p in products:
        pid = p.get("product_id", "")
        per_code_seen: set[str] = set()
        for issue in p.get("issues") or []:
            code = issue.get("code") or "unknown"
            entry = acc.get(code)
            if entry is None:
                entry = acc[code] = {
                    "code": code,
                    "description": issue.get("description", ""),
                    "severity": "",
                    "products": 0,
                    "occurrences": 0,
                    "disapproving_products": 0,
                    "_disapproving_ids": set(),
                    "_ctx": defaultdict(set),
                    "sample_product_ids": [],
                    "sample_offer_ids": [],
                }
            entry["occurrences"] += 1
            if not entry["description"] and issue.get("description"):
                entry["description"] = issue["description"]
            severity = issue.get("severity", "")
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(entry["severity"], 0):
                entry["severity"] = severity
            ctx = issue.get("reporting_context") or "UNKNOWN"
            # Same rule as classify(): an excluded destination does not disapprove a product.
            if severity == "DISAPPROVED" and ctx not in EXCLUDED_CONTEXTS:
                entry["_disapproving_ids"].add(pid)
            for country in issue.get("applicable_countries") or [ALL_COUNTRIES]:
                entry["_ctx"][(ctx, country)].add(pid)
            if code not in per_code_seen:
                per_code_seen.add(code)
                entry["products"] += 1
                if len(entry["sample_product_ids"]) < SAMPLE_SIZE:
                    entry["sample_product_ids"].append(pid)
                    entry["sample_offer_ids"].append(p.get("offer_id", ""))

    result = {}
    for code, entry in acc.items():
        by_context: dict[str, dict[str, int]] = defaultdict(dict)
        for (ctx, country), ids in sorted(entry.pop("_ctx").items()):
            by_context[ctx][country] = len(ids)
        entry["by_context"] = dict(by_context)
        entry["disapproving_products"] = len(entry.pop("_disapproving_ids"))
        entry["count"] = entry["products"]
        result[code] = entry
    return result


def issue_product_counts(products: Iterable[dict]) -> dict[str, int]:
    """Unique products per issue code (cheap variant of summarize_issues for diffs)."""
    ids: dict[str, set] = defaultdict(set)
    for p in products:
        for issue in p.get("issues") or []:
            ids[issue.get("code") or "unknown"].add(p.get("product_id", ""))
    return {code: len(s) for code, s in ids.items()}
