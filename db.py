"""SQLite cache for tracked jobs.

Schema mirrors the source-of-truth per stage (one stage_* table per workflow
step) plus a materialized `jobs_view` rebuilt after each sync run.

Pragma block matches se-key/se-billing for consistency (WAL + foreign keys).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

log = logging.getLogger("se-tracking.db")

_DB_PATH = os.getenv("TRACKING_DB_PATH", "data/tracking.db")
_LOCK = threading.Lock()


SCHEMA = [
    # --- stage_keyed (from se-key) -------------------------------------------
    # NOTE: se-key's `survey_no` field actually holds the INVOICE number
    # (SEABI-xxx/SESV-xxx). The adapter maps it to invoice_canonical here so
    # joins line up with stage_approved/stage_debt which both index by invoice.
    """
    CREATE TABLE IF NOT EXISTS stage_keyed (
        claim_canonical   TEXT NOT NULL,
        invoice_canonical TEXT NOT NULL DEFAULT '',
        survey_canonical  TEXT NOT NULL DEFAULT '',
        source_id         INTEGER NOT NULL,
        claim_display     TEXT NOT NULL,
        invoice_display   TEXT,
        survey_display    TEXT,
        created_at        TEXT,
        keyer             TEXT,
        work_type         TEXT,
        invoice_mix       TEXT,
        isurvey_sent      INTEGER,
        retry_count       INTEGER,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (claim_canonical, invoice_canonical, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_keyed_claim ON stage_keyed(claim_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_keyed_invoice ON stage_keyed(invoice_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_keyed_created ON stage_keyed(created_at)",

    # --- stage_closed (from se-billing) --------------------------------------
    # Same shape: se-billing's `survey_no` is also an invoice number.
    """
    CREATE TABLE IF NOT EXISTS stage_closed (
        claim_canonical   TEXT NOT NULL,
        invoice_canonical TEXT NOT NULL DEFAULT '',
        survey_canonical  TEXT NOT NULL DEFAULT '',
        source_id         INTEGER NOT NULL,
        claim_display     TEXT NOT NULL,
        invoice_display   TEXT,
        survey_display    TEXT,
        ts                TEXT,
        province_id       TEXT,
        province_name     TEXT,
        amphur_name       TEXT,
        surveyor_name     TEXT,
        inspector_name    TEXT,
        oss_company       TEXT,
        is_se             INTEGER,
        sur_invest        INTEGER,
        ins_invest        INTEGER,
        ins_trans         INTEGER,
        ins_photo         INTEGER,
        out_of_area_amt   INTEGER,
        out_of_hours_amt  INTEGER,
        deduct_amt        INTEGER,
        late_submit       INTEGER,
        incomplete_docs   INTEGER,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (claim_canonical, invoice_canonical, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_closed_claim ON stage_closed(claim_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_closed_invoice ON stage_closed(invoice_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_closed_ts ON stage_closed(ts)",

    # --- stage_approved (from pw — DEFERRED, schema ready) -------------------
    """
    CREATE TABLE IF NOT EXISTS stage_approved (
        claim_canonical    TEXT NOT NULL,
        survey_canonical   TEXT NOT NULL DEFAULT '',
        invoice_canonical  TEXT NOT NULL DEFAULT '',
        source_id          TEXT NOT NULL,
        claim_display      TEXT NOT NULL,
        survey_display     TEXT,
        invoice_display    TEXT,
        date_approve       TEXT,
        approve_amount     REAL,
        offer_amount       REAL,
        deduct_amount      REAL,
        surveyor_name      TEXT,
        claim_type         TEXT,
        deduct_reason      TEXT,
        synced_at          TEXT NOT NULL,
        PRIMARY KEY (claim_canonical, survey_canonical, invoice_canonical, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_approved_claim ON stage_approved(claim_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_approved_date ON stage_approved(date_approve)",

    # --- stage_debt (from ตัดหนี้.json) --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS stage_debt (
        claim_canonical   TEXT NOT NULL,
        invoice_canonical TEXT NOT NULL,
        claim_display     TEXT NOT NULL,
        invoice_display   TEXT,
        cut_date          TEXT,       -- ISO YYYY-MM-DD
        cut_date_be       TEXT,       -- Thai BE label DD/MM/YYYY (kept for export)
        amount            REAL,
        source_file       TEXT,
        sheet             TEXT,
        synced_at         TEXT NOT NULL,
        PRIMARY KEY (claim_canonical, invoice_canonical)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_debt_claim ON stage_debt(claim_canonical)",
    "CREATE INDEX IF NOT EXISTS idx_debt_cut_date ON stage_debt(cut_date)",

    # --- sync_runs (per-source sync log) -------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        status       TEXT NOT NULL,
        rows_seen    INTEGER DEFAULT 0,
        rows_changed INTEGER DEFAULT 0,
        error        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON sync_runs(source, started_at DESC)",

    # --- jobs_view (materialized 4-way join) ---------------------------------
    # Joined on (claim_canonical, invoice_canonical). survey_canonical is kept
    # as a display-only field (only pw populates it with the real esurvey ID).
    """
    CREATE TABLE IF NOT EXISTS jobs_view (
        claim_canonical   TEXT NOT NULL,
        invoice_canonical TEXT NOT NULL DEFAULT '',
        claim_display     TEXT,
        invoice_display   TEXT,
        survey_display    TEXT,
        survey_canonical  TEXT,
        keyed             INTEGER NOT NULL DEFAULT 0,
        keyed_at          TEXT,
        keyed_keyer       TEXT,
        keyed_work_type   TEXT,
        keyed_sent        INTEGER,
        closed            INTEGER NOT NULL DEFAULT 0,
        closed_at         TEXT,
        closed_amount     REAL,
        closed_surveyor   TEXT,
        closed_province   TEXT,
        approved          INTEGER NOT NULL DEFAULT 0,
        approved_at       TEXT,
        approved_amount   REAL,
        approved_deduct   REAL,
        debt              INTEGER NOT NULL DEFAULT 0,
        debt_cut_date     TEXT,
        debt_amount       REAL,
        debt_invoice      TEXT,
        current_status    TEXT,        -- บันทึกงาน / จบงาน / อนุมัติ / ตัดหนี้
        status_index      INTEGER NOT NULL DEFAULT 0,
        skipped_stages    TEXT,        -- JSON array
        first_seen_at     TEXT,        -- anchor date for filtering (ISO)
        last_updated_at   TEXT,
        rebuilt_at        TEXT NOT NULL,
        PRIMARY KEY (claim_canonical, invoice_canonical)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs_view(current_status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs_view(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_last_updated ON jobs_view(last_updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_keyed_at ON jobs_view(keyed_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_closed_at ON jobs_view(closed_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_debt_cut ON jobs_view(debt_cut_date)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_invoice ON jobs_view(invoice_canonical)",
]


def db_path() -> str:
    return _DB_PATH


def _ensure_dir():
    dirpath = os.path.dirname(_DB_PATH) or "."
    os.makedirs(dirpath, exist_ok=True)


def open_conn() -> sqlite3.Connection:
    """Open a SQLite connection with WAL + foreign keys.

    Caller is responsible for closing. Prefer `with txn():` for write paths.
    """
    _ensure_dir()
    conn = sqlite3.connect(
        _DB_PATH,
        timeout=30.0,
        isolation_level=None,         # autocommit off — we use BEGIN/COMMIT manually
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def txn():
    """Write transaction; rollback on exception."""
    conn = open_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_schema():
    """Apply schema DDL idempotently. Safe to call on every boot."""
    with _LOCK:
        conn = open_conn()
        try:
            for stmt in SCHEMA:
                conn.execute(stmt)
        finally:
            conn.close()
    log.info("schema initialized at %s", _DB_PATH)


def record_sync_start(source: str, started_at: str) -> int:
    with txn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs(source, started_at, status) VALUES (?, ?, 'running')",
            (source, started_at),
        )
        return cur.lastrowid


def record_sync_end(run_id: int, finished_at: str, status: str,
                    rows_seen: int = 0, rows_changed: int = 0,
                    error: str | None = None):
    with txn() as conn:
        conn.execute(
            """UPDATE sync_runs
                  SET finished_at=?, status=?, rows_seen=?, rows_changed=?, error=?
                WHERE id=?""",
            (finished_at, status, rows_seen, rows_changed, error, run_id),
        )


def latest_sync_status() -> dict:
    """Return {source: {started_at, finished_at, status, rows_seen, rows_changed, error}}."""
    conn = open_conn()
    try:
        rows = conn.execute(
            """SELECT source, started_at, finished_at, status,
                      rows_seen, rows_changed, error
                 FROM sync_runs
                 WHERE id IN (
                     SELECT MAX(id) FROM sync_runs GROUP BY source
                 )"""
        ).fetchall()
    finally:
        conn.close()
    return {r["source"]: dict(r) for r in rows}
