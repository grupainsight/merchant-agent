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

    # Google OAuth
    google_client_id: str = field(default_factory=lambda: os.environ["GOOGLE_CLIENT_ID"])
    google_client_secret: str = field(default_factory=lambda: os.environ["GOOGLE_CLIENT_SECRET"])
    google_refresh_token: str = field(default_factory=lambda: os.environ["GOOGLE_REFRESH_TOKEN"])

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.environ["ANTHROPIC_API_KEY"])

    # Resend
    resend_api_key: str = field(default_factory=lambda: os.environ["RESEND_API_KEY"])
    resend_from: str = field(default_factory=lambda: os.getenv("RESEND_FROM", "autonoma@grupainsight.com"))

    # Agent
    agent_name: str = "merchant"
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))


config = Config()
