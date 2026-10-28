import os

# src.config reads these at import time; tests never reach real services.
for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN", "ANTHROPIC_API_KEY", "RESEND_API_KEY"):
    os.environ[key] = "test"

import pytest

from src import db, runtime
from tests.fake_supabase import FakeSupabase


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(db, "_client", fake)
    runtime.configure(dry_run_dir=None, llm_enabled=True)
    yield fake
    runtime.configure(dry_run_dir=None, llm_enabled=True)
