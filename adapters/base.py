"""Sync adapter contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SyncResult:
    rows_seen: int = 0
    rows_changed: int = 0
    error: str | None = None
    touched_claims: set[str] | None = None   # for partial jobs_view rebuild

    def merge(self, other: "SyncResult"):
        self.rows_seen += other.rows_seen
        self.rows_changed += other.rows_changed
        if self.touched_claims is not None and other.touched_claims is not None:
            self.touched_claims |= other.touched_claims


class SyncAdapter(ABC):
    """Source-specific data ingestion.

    Adapters MUST:
      - normalize identifiers via normalize.canonical_*
      - record sync_runs entries (use db.record_sync_start/end)
      - upsert into their stage_* table
      - return a SyncResult including touched_claims for partial rebuild
    """

    name: str = "unknown"
    interval_minutes: int = 5

    @abstractmethod
    def sync(self) -> SyncResult:
        ...


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
