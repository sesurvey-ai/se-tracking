"""SQLite store for debt-api — debt_records table (same shape as D:\\trackingDB\\debt.db)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

log = logging.getLogger("debt-api.db")

DB_PATH = os.getenv("DEBT_DB_PATH", "data/debt.db")
_LOCK = threading.Lock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS debt_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    claim           TEXT NOT NULL,
    invoice         TEXT NOT NULL,
    amount          REAL,
    cut_date        TEXT,           -- "DD/M/YYYY" BE
    source_file     TEXT,
    sheet           TEXT,
    UNIQUE(claim, invoice)
);

CREATE INDEX IF NOT EXISTS idx_debt_claim   ON debt_records(claim);
CREATE INDEX IF NOT EXISTS idx_debt_invoice ON debt_records(invoice);
CREATE INDEX IF NOT EXISTS idx_debt_cut     ON debt_records(cut_date);

CREATE TABLE IF NOT EXISTS upload_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    filename     TEXT NOT NULL,
    size_bytes   INTEGER,
    rows_added   INTEGER,
    rows_updated INTEGER,
    rows_skipped INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_upload_log_at ON upload_log(uploaded_at DESC);
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
    log.info("debt-api schema initialized at %s", DB_PATH)


def count_records() -> int:
    conn = open_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM debt_records").fetchone()[0]
    finally:
        conn.close()
