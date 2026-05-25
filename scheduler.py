"""APScheduler glue: per-source sync jobs + jobs_view rebuild trigger."""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

import jobs as jobs_view
from adapters import (
    SeKeyAdapter, SeBillingAdapter, DebtJsonAdapter, PwDbAdapter,
    IsurveyAPIAdapter, SyncAdapter, SyncResult,
)
from adapters.base import utc_now_iso

log = logging.getLogger("se-tracking.scheduler")

_ADAPTERS: list[SyncAdapter] = []
_LOCKS: dict[str, threading.Lock] = {}
_SCHEDULER: BackgroundScheduler | None = None
_LAST_REBUILD_AT: str | None = None


def _make_adapters() -> list[SyncAdapter]:
    return [
        SeKeyAdapter(),
        SeBillingAdapter(),
        DebtJsonAdapter(),
        PwDbAdapter(),
        IsurveyAPIAdapter(),   # pulls "จบงาน" from cloud.isurvey.mobi
    ]


def get_adapters() -> list[SyncAdapter]:
    if not _ADAPTERS:
        _ADAPTERS.extend(_make_adapters())
        for a in _ADAPTERS:
            _LOCKS[a.name] = threading.Lock()
    return _ADAPTERS


def get_adapter(name: str) -> SyncAdapter | None:
    for a in get_adapters():
        if a.name == name:
            return a
    return None


def run_sync(name: str, *, rebuild_view: bool = True,
             on_event: Callable[[dict], None] | None = None) -> SyncResult:
    """Run one adapter under its lock, then rebuild jobs_view.

    `on_event(dict)` is invoked at progress points for SSE consumers.
    """
    a = get_adapter(name)
    if not a:
        raise ValueError(f"unknown adapter: {name}")

    lock = _LOCKS[name]
    if not lock.acquire(blocking=False):
        if on_event:
            on_event({"type": "skipped", "source": name, "reason": "locked"})
        return SyncResult(error="already running")
    try:
        if on_event:
            on_event({"type": "start", "source": name, "at": utc_now_iso()})
        result = a.sync()
        if on_event:
            on_event({
                "type": "done", "source": name, "at": utc_now_iso(),
                "rows_seen": result.rows_seen,
                "rows_changed": result.rows_changed,
                "error": result.error,
            })
        if rebuild_view and result.rows_changed > 0:
            touched = result.touched_claims or set()
            if on_event:
                on_event({"type": "rebuild_start", "touched": len(touched)})
            written = jobs_view.rebuild(touched or None)
            global _LAST_REBUILD_AT
            _LAST_REBUILD_AT = utc_now_iso()
            if on_event:
                on_event({"type": "rebuild_done", "rows": written, "at": _LAST_REBUILD_AT})
        return result
    finally:
        lock.release()


def run_sync_all(on_event: Callable[[dict], None] | None = None) -> dict:
    """Run all adapters serially, then a single jobs_view rebuild at the end.

    Serial keeps SQLite writes from contending; the long pole is debt_json's
    one-time parse, which is still under a few seconds.
    """
    summary: dict[str, dict] = {}
    all_touched: set[str] = set()
    for a in get_adapters():
        if on_event:
            on_event({"type": "start", "source": a.name})
        # Use the per-source lock without rebuild_view; we'll rebuild once at the end.
        lock = _LOCKS[a.name]
        if not lock.acquire(blocking=False):
            summary[a.name] = {"error": "locked"}
            if on_event:
                on_event({"type": "skipped", "source": a.name, "reason": "locked"})
            continue
        try:
            r = a.sync()
            summary[a.name] = {
                "rows_seen": r.rows_seen,
                "rows_changed": r.rows_changed,
                "error": r.error,
            }
            if r.touched_claims:
                all_touched |= r.touched_claims
            if on_event:
                on_event({
                    "type": "done", "source": a.name,
                    "rows_seen": r.rows_seen, "rows_changed": r.rows_changed,
                    "error": r.error,
                })
        finally:
            lock.release()

    if on_event:
        on_event({"type": "rebuild_start", "touched": len(all_touched)})
    written = jobs_view.rebuild(all_touched or None)
    global _LAST_REBUILD_AT
    _LAST_REBUILD_AT = utc_now_iso()
    if on_event:
        on_event({"type": "rebuild_done", "rows": written, "at": _LAST_REBUILD_AT})
    summary["_rebuild"] = {"rows": written, "at": _LAST_REBUILD_AT}
    return summary


def last_rebuild_at() -> str | None:
    return _LAST_REBUILD_AT


def start():
    """Boot the background scheduler. Idempotent."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER

    default_interval = int(os.getenv("SYNC_INTERVAL_MIN", "5"))
    sched = BackgroundScheduler(daemon=True)
    for a in get_adapters():
        # adapters can override interval (e.g. isurvey-api defaults to 60 min)
        minutes = getattr(a, "interval_minutes", default_interval) or default_interval
        sched.add_job(
            func=run_sync, args=(a.name,),
            trigger="interval", minutes=minutes,
            id=a.name, max_instances=1, coalesce=True,
            replace_existing=True,
        )
    sched.start()
    log.info("scheduler started (default=%d min, adapters=%s)",
             default_interval, [a.name for a in get_adapters()])
    _SCHEDULER = sched
    return sched


def shutdown():
    global _SCHEDULER
    if _SCHEDULER:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
