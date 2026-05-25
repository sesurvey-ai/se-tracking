"""SQLite store for emcs-api — pw_records table (same shape as old emcs.db)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

log = logging.getLogger("emcs-api.db")

DB_PATH = os.getenv("EMCS_DB_PATH", "data/emcs.db")
_LOCK = threading.Lock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pw_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),

    claim_no        TEXT NOT NULL,
    survey_no       TEXT,
    invoice_no      TEXT,
    invoice_seq     TEXT,

    date_approve    TEXT,
    offer_amount    REAL,
    approve_amount  REAL,
    deduct_amount   REAL,
    deduct_reason   TEXT,

    claim_type      TEXT,
    surveyer        TEXT,
    acc_province    TEXT,

    source_file     TEXT,
    source_sheet    TEXT,

    UNIQUE(claim_no, invoice_no, invoice_seq)
);

CREATE INDEX IF NOT EXISTS idx_pw_claim   ON pw_records(claim_no);
CREATE INDEX IF NOT EXISTS idx_pw_survey  ON pw_records(survey_no);
CREATE INDEX IF NOT EXISTS idx_pw_invoice ON pw_records(invoice_no);
CREATE INDEX IF NOT EXISTS idx_pw_date    ON pw_records(date_approve);
"""


def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)


def open_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def txn():
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
    with _LOCK:
        conn = open_conn()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
    log.info("emcs-api schema initialized at %s", DB_PATH)


def count_records() -> int:
    conn = open_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM pw_records").fetchone()[0]
    finally:
        conn.close()


def db_path() -> str:
    return DB_PATH
