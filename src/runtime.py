"""
Merchant Agent - Run mode (production vs dry run).

Dry run reads production data (Merchant API, previous snapshots, existing
recommendations) but never writes: every database write and every email is
recorded as a JSON line in the output directory instead. The Supabase client is
also wrapped so that a write call that bypasses this routing fails loudly
instead of reaching production tables.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


class DryRunWriteError(RuntimeError):
    """A write reached the Supabase client during a dry run."""


_dry_dir: Optional[Path] = None
_llm_enabled: bool = True


def configure(dry_run_dir: Optional[str] = None, llm_enabled: bool = True) -> None:
    """Switch the process into dry-run mode (when dry_run_dir is set) and/or disable the LLM."""
    global _dry_dir, _llm_enabled
    _dry_dir = Path(dry_run_dir) if dry_run_dir else None
    if _dry_dir:
        _dry_dir.mkdir(parents=True, exist_ok=True)
    _llm_enabled = llm_enabled


def is_dry_run() -> bool:
    return _dry_dir is not None


def llm_enabled() -> bool:
    return _llm_enabled


def record(kind: str, payload: Any) -> None:
    """Append one dry-run record (a would-be write or email) to <dir>/<kind>.jsonl."""
    assert _dry_dir is not None, "record() is only valid in dry-run mode"
    with open(_dry_dir / f"{kind}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


# ============================================================
# Read-only guard around the Supabase client
# ============================================================

_WRITE_METHODS = frozenset({"insert", "upsert", "update", "delete"})


class ReadOnlyClient:
    """Wraps a Supabase client so that table writes raise DryRunWriteError."""

    def __init__(self, client: Any):
        self._client = client

    def table(self, name: str) -> "_ReadOnlyTable":
        return _ReadOnlyTable(self._client.table(name), name)

    def __getattr__(self, attr: str) -> Any:
        if attr in ("rpc", "storage", "functions"):
            raise DryRunWriteError(f"{attr} is not allowed in a dry run")
        return getattr(self._client, attr)


class _ReadOnlyTable:
    def __init__(self, builder: Any, name: str):
        self._builder = builder
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        if attr in _WRITE_METHODS:
            raise DryRunWriteError(f"{attr} on {self._name} blocked in dry run")
        return getattr(self._builder, attr)
