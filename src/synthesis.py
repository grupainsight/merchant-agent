"""
Merchant Agent - LLM Synthesis.
Takes collected data and generates recommendations via Claude Sonnet.
Called only when snapshot compare detects significant changes.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional

import anthropic

from src import runtime
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
    "issue_code": "one code from the data, see Subject rules"
  }
]

Subject rules (issue_code):
- For a product issue use its exact code from "Issues by Type", e.g. "landing_page_error".
- For an account issue use "account:" + its id from "Account Issues".
- Only for a recommendation about overall performance use "topic:performance",
  about the catalog as a whole "topic:catalog".
- Any other value is rejected. One recommendation per issue_code.

Rules:
- Maximum 5 recommendations per run
- INSIGHT = informational, no action needed
- RECOMMENDATION = suggested fix, client should act
- ACTION = specific executable change (rare, only when very clear)
- Always set expires_at context: INSIGHTs expire in 14 days
- Be specific: "12 products disapproved due to landing_page_error" not "some products have issues"
- "products" in the data are unique products; "occurrences" counts the same product once per
  destination and country - never present occurrences as a number of products
- If a data source is marked unavailable, say so; do not infer that it has no data
- When products are disapproved, calculate approximate revenue impact if possible
- Respond ONLY with the JSON array, no other text
- Language: Polish (this is for Polish e-commerce clients)
"""


@dataclass
class SynthesisResult:
    """
    status: "ok" (parsed recommendations, possibly an empty list), "failed"
    (API error or unparseable answer) or "disabled" (no key / LLM switched off).
    Token usage is kept even when the answer could not be parsed.
    """
    status: str
    recommendations: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    @property
    def llm_called(self) -> bool:
        return self.input_tokens > 0 or self.output_tokens > 0


def synthesize_recommendations(
    client_name: str,
    merchant_id: str,
    health_summary: dict,
    account_issues: dict,
    performance: dict,
    diff: dict,
    cross_channel: Optional[dict] = None,
) -> SynthesisResult:
    """
    Call Claude to generate recommendations from collected data.
    Returns raw recommendation dicts (not yet saved to Supabase) with the call status.
    """
    if not runtime.llm_enabled():
        return SynthesisResult(status="disabled", error="LLM switched off for this run")
    if config.anthropic_api_key == "placeholder":
        logger.warning("Anthropic API key is placeholder - skipping LLM synthesis")
        return SynthesisResult(status="disabled", error="Anthropic API key is a placeholder")

    user_message = _build_user_message(
        client_name, merchant_id, health_summary,
        account_issues, performance, diff, cross_channel,
    )

    result = SynthesisResult(status="failed")
    raw_text = ""
    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        logger.info(
            f"LLM synthesis: {result.input_tokens} input tokens, "
            f"{result.output_tokens} output tokens, stop_reason={response.stop_reason}"
        )
        if response.stop_reason == "max_tokens":
            result.error = "answer cut off at max_tokens"
            return result

        raw_text = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

        recommendations = json.loads(raw_text)
        if not isinstance(recommendations, list):
            result.error = f"LLM returned {type(recommendations).__name__}, expected a JSON array"
            return result

        result.status = "ok"
        result.recommendations = recommendations
        logger.info(f"LLM generated {len(recommendations)} recommendations")

    except json.JSONDecodeError as e:
        result.error = f"answer is not valid JSON: {e}"
        logger.error(f"Failed to parse LLM response as JSON: {e}; raw: {raw_text[:500]}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"[:500]
        logger.error(f"LLM synthesis failed: {e}")
    return result


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
        "- (Disapproved = disapproved in at least one destination other than FREE_LOCAL_LISTINGS)",
    ]

    # Issues breakdown
    issues = health_summary.get("issues_by_code", {})
    if issues:
        parts.append("")
        parts.append("## Issues by Type (code: unique products / occurrences)")
        for code, data in sorted(issues.items(), key=lambda x: x[1]["products"], reverse=True):
            contexts = "; ".join(
                f"{ctx}: " + ", ".join(f"{country} {n}" for country, n in countries.items())
                for ctx, countries in data.get("by_context", {}).items()
            )
            parts.append(
                f"- {code}: {data['products']} products, {data['occurrences']} occurrences, "
                f"{data.get('disapproving_products', 0)} products disapproved by it "
                f"({data.get('description', '')}, severity: {data.get('severity') or 'unknown'}) "
                f"[by destination and country: {contexts}]"
            )

    # Account issues
    if account_issues.get("error"):
        parts.append("")
        parts.append(f"## Account Issues\n- UNAVAILABLE (collection failed): {account_issues['error']}")
    elif account_issues.get("details"):
        parts.append("")
        parts.append("## Account Issues")
        for issue in account_issues["details"]:
            parts.append(
                f"- id {_account_issue_id(issue)} [{issue.get('severity', '?')}] "
                f"{issue.get('title', '?')}: {issue.get('detail', '')}"
            )

    # Performance
    parts.append("")
    parts.append(f"## Performance ({performance.get('date_from', '?')} to {performance.get('date_to', '?')})")
    if performance.get("status") == "error":
        parts.append(f"- UNAVAILABLE (collection failed, not a lack of data): {performance.get('error')}")
    elif performance.get("status") == "no_data":
        parts.append("- The API answered but returned no rows for this period")
    else:
        parts.append(f"- Impressions: {performance.get('impressions', 0)}")
        parts.append(f"- Clicks: {performance.get('clicks', 0)}")
        parts.append(f"- CTR: {performance.get('ctr', 0):.2%}")

    # Diff
    parts.append("")
    parts.append("## Changes vs Previous Snapshot")
    if not diff.get("comparable"):
        parts.append(f"- NOT AVAILABLE: {diff.get('summary', 'no previous data')}")
    else:
        parts.append(f"- Previous snapshot: {diff.get('previous_date')}")
        parts.append(f"- Summary: {diff.get('summary', '')}")
        parts.append(f"- New disapprovals: {diff.get('new_disapprovals_count', 0)}")
        parts.append(f"- Resolved disapprovals: {diff.get('resolved_disapprovals_count', 0)}")
        parts.append(f"- Disapproved delta: {diff.get('disapproved_delta', 0)}")
        if diff.get("new_issue_codes"):
            parts.append(f"- New issue codes: {', '.join(diff['new_issue_codes'])}")
        if diff.get("resolved_issue_codes"):
            parts.append(f"- Issue codes no longer present: {', '.join(diff['resolved_issue_codes'])}")
        for change in diff.get("issue_changes", [])[:10]:
            parts.append(f"- {change['code']}: {change['before']} -> {change['after']} products")

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


def _account_issue_id(issue: dict) -> str:
    """Short id of an account issue: the last segment of its resource name."""
    return (issue.get("issue_id") or "").rstrip("/").rsplit("/", 1)[-1] or "unknown"
