"""
Merchant Agent - Configuration.
Loads from environment variables (Railway injects these).
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Supabase
    supabase_url: str = field(default_factory=lambda: os.environ["SUPABASE_URL"])
    supabase_key: str = field(default_factory=lambda: os.environ["SUPABASE_SERVICE_KEY"])

    # Google OAuth (strip: a pasted token with a trailing newline/space makes Google answer invalid_grant)
    google_client_id: str = field(default_factory=lambda: os.environ["GOOGLE_CLIENT_ID"].strip())
    google_client_secret: str = field(default_factory=lambda: os.environ["GOOGLE_CLIENT_SECRET"].strip())
    google_refresh_token: str = field(default_factory=lambda: os.environ["GOOGLE_REFRESH_TOKEN"].strip())

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.environ["ANTHROPIC_API_KEY"])

    # Resend
    resend_api_key: str = field(default_factory=lambda: os.environ["RESEND_API_KEY"])
    resend_from: str = field(default_factory=lambda: os.getenv("RESEND_FROM", "autonoma@grupainsight.com"))

    # Weekly product performance (paid / free per offer, sql/005 in autonoma-contracts).
    # Off until the migration is applied.
    product_performance: bool = field(default_factory=lambda: os.getenv("MERCHANT_PRODUCT_PERFORMANCE", "off") == "on")
    perf_revision_weeks: int = field(default_factory=lambda: int(os.getenv("MERCHANT_PERF_REVISION_WEEKS", "2")))
    perf_backfill_weeks: int = field(default_factory=lambda: int(os.getenv("MERCHANT_PERF_BACKFILL_WEEKS", "56")))
    perf_retention_weeks: int = field(default_factory=lambda: int(os.getenv("MERCHANT_PERF_RETENTION_WEEKS", "60")))

    # Agent
    agent_name: str = "merchant"
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))


config = Config()
