"""pw adapter — reads from emcs-api (HTTP) instead of local SQLite.

Migrated from `D:\\trackingDB\\emcs.db` file access to HTTP API. The
central service stores the same `pw_records` table; this adapter just
pulls incrementally via `since_id`.
"""
from __future__ import annotations

import logging
import os

import requests

import db
from adapters.base import SyncAdapter, SyncResult, utc_now_iso
from normalize import (
    canonical_claim, canonical_survey, canonical_invoice, to_iso_date,
)

log = logging.getLogger("se-tracking.adapter.pw_db")

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30


class PwDbAdapter(SyncAdapter):
    name = "pw"

    def __init__(self):
        self.base_url = os.getenv("EMCS_API_URL", "http://localhost:5500").rstrip("/")
        self.api_key = os.getenv("EMCS_API_KEY", "").strip()
        self.interval_minutes = int(os.getenv("SYNC_INTERVAL_MIN", "5"))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _last_seen_id(self) -> int:
        """Find the highest source_id we've already imported from emcs-api.

        We stored emcs-api row ids as TEXT in source_id. Take the max
        numeric value to use as `since_id` for the next pull.
        """
        conn = db.open_conn()
        try:
            row = conn.execute(
                "SELECT MAX(CAST(source_id AS INTEGER)) AS m "
                "FROM stage_approved "
                "WHERE source_id GLOB '[0-9]*'"
            ).fetchone()
            return int(row["m"]) if row and row["m"] is not None else 0
        finally:
            conn.close()

    def sync(self) -> SyncResult:
        started = utc_now_iso()
        run_id = db.record_sync_start(self.name, started)
        result = SyncResult(touched_claims=set())

        try:
            since_id = self._last_seen_id()
            log.info("pw sync starting (base=%s, since_id=%d)", self.base_url, since_id)

            offset = 0
            while True:
                r = requests.get(
                    f"{self.base_url}/api/records",
                    params={"limit": PAGE_SIZE, "offset": offset, "since_id": since_id},
                    headers=self._headers(),
                    timeout=REQUEST_TIMEOUT,
                )
                r.raise_for_status()
                data = r.json()
                rows = data.get("rows", []) or []
                if not rows:
                    break

                changed = self._upsert(rows, result.touched_claims)
                result.rows_seen += len(rows)
                result.rows_changed += changed

                if len(rows) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            db.record_sync_end(run_id, utc_now_iso(), "ok",
                               result.rows_seen, result.rows_changed)
            log.info("pw sync ok: seen=%d changed=%d",
                     result.rows_seen, result.rows_changed)
        except Exception as e:
            log.exception("pw sync failed")
            result.error = str(e)
            db.record_sync_end(run_id, utc_now_iso(), "error",
                               result.rows_seen, result.rows_changed, str(e))
        return result

    def _upsert(self, rows, touched: set) -> int:
        synced_at = utc_now_iso()
        changed = 0
        with db.txn() as conn:
            for r in rows:
                claim_raw = r.get("claim_no") or ""
                survey_raw = r.get("survey_no") or ""
                invoice_raw = r.get("invoice_no") or ""

                claim_canon = canonical_claim(claim_raw)
                if not claim_canon:
                    continue
                survey_canon = canonical_survey(survey_raw) or ""
                invoice_canon = canonical_invoice(invoice_raw) or ""

                date_iso = to_iso_date(r.get("date_approve"))

                cur = conn.execute(
                    """INSERT INTO stage_approved
                          (claim_canonical, survey_canonical, invoice_canonical, source_id,
                           claim_display, survey_display, invoice_display,
                           date_approve, approve_amount, offer_amount, deduct_amount,
                           surveyor_name, claim_type, deduct_reason, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(claim_canonical, survey_canonical, invoice_canonical, source_id)
                       DO UPDATE SET
                           claim_display=excluded.claim_display,
                           survey_display=excluded.survey_display,
                           invoice_display=excluded.invoice_display,
                           date_approve=excluded.date_approve,
                           approve_amount=excluded.approve_amount,
                           offer_amount=excluded.offer_amount,
                           deduct_amount=excluded.deduct_amount,
                           surveyor_name=excluded.surveyor_name,
                           claim_type=excluded.claim_type,
                           deduct_reason=excluded.deduct_reason,
                           synced_at=excluded.synced_at""",
                    (
                        claim_canon, survey_canon, invoice_canon, str(r.get("id") or 0),
                        claim_raw, survey_raw, invoice_raw,
                        date_iso,
                        _f(r.get("approve_amount")),
                        _f(r.get("offer_amount")),
                        _f(r.get("deduct_amount")),
                        r.get("surveyer"), r.get("claim_type"), r.get("deduct_reason"),
                        synced_at,
                    ),
                )
                changed += cur.rowcount
                touched.add(claim_canon)
        return changed


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
