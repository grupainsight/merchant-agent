"""
Merchant Agent - Google OAuth helper.
Handles token refresh for Merchant Center API access.
"""

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.config import config

SCOPES = ["https://www.googleapis.com/auth/content"]


def get_credentials() -> Credentials:
    """Get refreshed Google OAuth credentials for Merchant API."""
    creds = Credentials(
        token=None,
        refresh_token=config.google_refresh_token,
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds
