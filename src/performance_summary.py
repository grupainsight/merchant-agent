"""
Merchant Agent - Weekly paid / free summary for the dashboard, report and prompt.

ADS and ORGANIC are summarized separately and never added up. Totals and trends
come from the complete channel totals of published weeks; per-offer rows only
feed the completeness ratios (contract autonoma_contracts.merchant_performance).
"""

from datetime import date, timedelta
from typing import Optional

from autonoma_contracts.merchant_performance import completeness

from src.collectors.product_performance import METHODS

TREND_WEEKS = 4
YEAR = timedelta(weeks=52)


def _pct(current: int, base: float) -> Optional[float]:
    if not base:
        return None
    return round((current - base) / base * 100, 1)


def summarize(periods: dict[str, dict], last_week: date) -> dict:
    """
    periods: published weeks keyed by period_start (ISO date).
    data_status: "complete" (last week and the TREND_WEEKS before it are published),
    "partial" (last week published, some trend weeks missing), "missing" (no last week).
    """
    key = last_week.isoformat()
    current = periods.get(key)
    previous = [periods.get((last_week - timedelta(weeks=i)).isoformat()) for i in range(1, TREND_WEEKS + 1)]
    year_ago = periods.get((last_week - YEAR).isoformat())
    summary = {
        "period": {"start": key, "end": (last_week + timedelta(days=6)).isoformat()},
        "unit": "Monday-Sunday week, Merchant Reports API product_performance_view",
        "weeks_available": len(periods),
        "missing_trend_weeks": [(last_week - timedelta(weeks=i)).isoformat()
                                for i, p in enumerate(previous, start=1) if p is None],
    }
    if current is None:
        summary["data_status"] = "missing"
        return summary
    summary["data_status"] = "complete" if all(previous) else "partial"
    summary["fetched_at"] = current.get("fetched_at")

    for method in METHODS:
        prefix = method.lower()
        impressions, clicks = current[f"{prefix}_impressions"], current[f"{prefix}_clicks"]
        channel = {
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else None,
            "completeness": {
                "impressions_by_offer_share": completeness(current[f"{prefix}_impressions_by_offer"], impressions),
                "clicks_by_offer_share": completeness(current[f"{prefix}_clicks_by_offer"], clicks),
            },
            "trend_vs_prev_4w": None,
            "trend_yoy": None,
        }
        if all(previous):
            avg_impressions = sum(p[f"{prefix}_impressions"] for p in previous) / TREND_WEEKS
            avg_clicks = sum(p[f"{prefix}_clicks"] for p in previous) / TREND_WEEKS
            channel["trend_vs_prev_4w"] = {
                "impressions_pct": _pct(impressions, avg_impressions),
                "clicks_pct": _pct(clicks, avg_clicks),
                "weeks_used": TREND_WEEKS,
            }
        if year_ago:
            channel["trend_yoy"] = {
                "impressions_pct": _pct(impressions, year_ago[f"{prefix}_impressions"]),
                "clicks_pct": _pct(clicks, year_ago[f"{prefix}_clicks"]),
                "period_start": year_ago["period_start"],
            }
        summary[method] = channel
    return summary


def prompt_lines(summary: Optional[dict]) -> list[str]:
    """Performance section of the LLM input: channels in separate blocks, with completeness."""
    if not summary or summary.get("data_status") == "missing":
        return []
    lines = [
        f"## Product traffic by channel, week {summary['period']['start']} to {summary['period']['end']}",
        "- ADS (paid) and ORGANIC (free listings) are separate channels: never add them up,",
        "  and do not explain why traffic changed (Ads and SEO agents own those decisions).",
        "- Per-product numbers cover only part of the traffic (see completeness); a product",
        "  without a row does not have zero traffic.",
    ]
    for method in METHODS:
        c = summary[method]
        trend = c["trend_vs_prev_4w"]
        lines.append(
            f"- {method}: {c['impressions']} impressions, {c['clicks']} clicks"
            + (f", CTR {c['ctr']:.2%}" if c["ctr"] is not None else "")
            + (f"; vs previous 4 weeks: impressions {trend['impressions_pct']:+}%, clicks {trend['clicks_pct']:+}%"
               if trend and trend["impressions_pct"] is not None and trend["clicks_pct"] is not None
               else "; trend not available")
            + f"; per-product completeness: impressions {c['completeness']['impressions_by_offer_share']},"
              f" clicks {c['completeness']['clicks_by_offer_share']}"
        )
    return lines
