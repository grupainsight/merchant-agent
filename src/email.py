"""
Merchant Agent - Email notifications via Resend.
"""

import logging
from typing import Optional

import resend

from src.config import config

logger = logging.getLogger(__name__)


def send_report(
    to: str,
    subject: str,
    html_body: str,
    reply_to: Optional[str] = None,
) -> bool:
    """Send a report email. Returns True on success."""
    resend.api_key = config.resend_api_key

    try:
        params = {
            "from": config.resend_from,
            "to": [to],
            "subject": subject,
            "html": html_body,
        }
        if reply_to:
            params["reply_to"] = reply_to

        resend.Emails.send(params)
        logger.info(f"Email sent to {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return False
