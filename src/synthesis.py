"""
Merchant Agent - LLM Synthesis.
Takes collected data and generates recommendations via Claude Sonnet.
Called only when snapshot compare detects significant changes.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Optional

import anthropic

from src.config import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Merchant Agent, part of the Autonoma platform by Grupa Insight.
You analyze Google Merchant Center data for e-commerce clients and generate actionable recommendations.

You receive:
- Product health summary (approved/disapproved/pending counts, issues by code)
- Account-level issues
- Performance data (if available)
- Snapshot diff (what changed since last week)
- Cross-channel data from other agents (Ads, SEO) if available

Your job:
1. Identify the most impactful problems
2. Explain WHY each problem matters (lost revenue, wasted ad spend, missed visibility)
3. Recommend specific, actionable fixes
4. When cross-channel data is available, highlight correlations

Output format - respond with a JSON array of recommendations:
[
  {
    "type": "INSIGHT" | "RECOMMENDATION" | "ACTION",
    "title": "Short title (max 100 chars)",
    "summary": "Detailed explanation with business impact (max 500 chars)",
    "recommended_action": "Specific steps to fix (null for INSIGHT)",
    "impact": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
    "confidence": 50-100,
    "reason_codes": ["CODE1", "CODE2"],
    "dedupe_key_suffix": "unique_key_for_this_issue"
  }
]

Rules:
- Maximum 5 recommendations per run
- INSIGHT = informational, no action needed
- RECOMMENDATION = suggested fix, client should act
- ACTION = specific executable change (rare, only when very clear)
- Always set expires_at context: INSIGHTs expire in 14 days
- Be specific: "12 products disapproved due to landing_page_error" not "some products have issues"
- When products are disapproved, calculate approximate revenue impact if possible
- Respond ONLY with the JSON array, no other text
- Language: Polish (this is for Polish e-commerce clients)
"""


def synthesize_recommendations(
    client_name: str,
    merchant_id: str,
    health_summary: dict,
    account_issues: dict,
    performance: dict,
    diff: dict,
    cross_channel: Optional[dict] = None,
) -> list[dict]:
    """
    Call Claude to generate recommendations from collected data.
    Returns list of raw recommendation dicts (not yet saved to Supabase).
    """
    if config.anthropic_api_key == "placeholder":
        logger.warning("Anthropic API key is placeholder - skipping LLM synthesis")
        return []

    user_message = _build_user_message(
        client_name, merchant_id, health_summary,
        account_issues, performance, diff, cross_channel,
    )

    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

        # Track token usage
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        logger.info(
            f"LLM synthesis: {input_tokens} input tokens, "
            f"{output_tokens} output tokens"
        )

        # Parse JSON response
        recommendations = json.loads(raw_text)
        if not isinstance(recommendations, list):
            logger.error(f"LLM returned non-list: {type(recommendations)}")
            return []

        # Attach token usage to first recommendation for cost tracking
        if recommendations:
            recommendations[0]["_input_tokens"] = input_tokens
            recommendations[0]["_output_tokens"] = output_tokens

        logger.info(f"LLM generated {len(recommendations)} recommendations")
        return recommendations

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}")
        logger.error(f"Raw response: {raw_text[:500]}")
        return []
    except Exception as e:
        logger.error(f"LLM synthesis failed: {e}")
        return []


def _build_user_message(
    client_name: str,
    merchant_id: str,
    health_summary: dict,
    account_issues: dict,
    performance: dict,
    diff: dict,
    cross_channel: Optional[dict],
) -> str:
    """Build the user message with all collected data."""
    parts = [
        f"# Merchant Center Report: {client_name} (ID: {merchant_id})",
        f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}",
        "",
        "## Product Health",
        f"- Total products: {health_summary.get('total', 0)}",
        f"- Approved: {health_summary.get('approved', 0)}",
        f"- Disapproved: {health_summary.get('disapproved', 0)}",
        f"- Pending: {health_summary.get('pending', 0)}",
    ]

    # Issues breakdown
    issues = health_summary.get("issues_by_code", {})
    if issues:
        parts.append("")
        parts.append("## Issues by Type")
        for code, data in sorted(issues.items(), key=lambda x: x[1]["count"], reverse=True):
            parts.append(
                f"- {code}: {data['count']} products "
                f"({data.get('description', '')}, severity: {data.get('severity', 'unknown')})"
            )

    # Account issues
    if account_issues.get("details"):
        parts.append("")
        parts.append("## Account Issues")
        for issue in account_issues["details"]:
            parts.append(f"- [{issue.get('severity', '?')}] {issue.get('title', '?')}: {issue.get('detail', '')}")

    # Performance
    parts.append("")
    parts.append("## Performance (last 7 days)")
    if performance.get("error"):
        parts.append(f"- Performance data unavailable: {performance['error']}")
    else:
        parts.append(f"- Impressions: {performance.get('impressions', 0)}")
        parts.append(f"- Clicks: {performance.get('clicks', 0)}")
        parts.append(f"- CTR: {performance.get('ctr', 0):.2%}")

    # Diff
    parts.append("")
    parts.append("## Changes vs Previous Week")
    parts.append(f"- Significant change: {diff.get('has_significant_change', False)}")
    parts.append(f"- Summary: {diff.get('summary', 'No previous data')}")
    parts.append(f"- New disapprovals: {diff.get('new_disapprovals_count', 0)}")
    parts.append(f"- Resolved disapprovals: {diff.get('resolved_disapprovals_count', 0)}")
    parts.append(f"- Disapproved delta: {diff.get('disapproved_delta', 0)}")

    # Cross-channel
    if cross_channel:
        parts.append("")
        parts.append("## Cross-Channel Data")
        if cross_channel.get("ads"):
            ads = cross_channel["ads"]
            parts.append(f"### Ads Agent (last report)")
            parts.append(f"- Status: {ads.get('status', 'unknown')}")
            parts.append(f"- Summary: {ads.get('summary', 'no data')}")
        if cross_channel.get("seo"):
            seo = cross_channel["seo"]
            parts.append(f"### SEO Agent (last report)")
            parts.append(f"- Status: {seo.get('status', 'unknown')}")
            parts.append(f"- Summary: {seo.get('summary', 'no data')}")

    return "\n".join(parts)
