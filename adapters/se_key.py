"""se-key adapter: GET /api/records, upsert into stage_keyed.

Incremental sync via `since_id`. Cursor stored in sync_runs (rows_seen acts as
high-water mark; we use the row's max id we've seen via a tiny extra query).
"""
from __future__ import annotations

import logging
import os

import requests

import db
from adapters.base import SyncAdapter, SyncResult, utc_now_iso
from normalize import canonical_claim, canonical_invoice

log = logging.getLogger("se-tracking.adapter.se_key")

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30


class SeKeyAdapter(SyncAdapter):
    name = "se-key"

    def __init__(self):
        self.base_url = os.getenv("SE_KEY_URL", "http://localhost:3000").rstrip("/")
        self.api_key = os.getenv("SE_KEY_API_KEY", "").strip()
        self.interval_minutes = int(os.getenv("SYNC_INTERVAL_MIN", "5"))

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _last_seen_id(self) -> int:
        conn = db.open_conn()
        try:
            row = conn.execute(
                "SELECT MAX(source_id) AS m FROM stage_keyed"
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
            log.info("se-key sync starting (since_id=%d)", since_id)

            offset = 0
            while True:
                params = {
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "since_id": since_id,
                }
                r = requests.get(
                    f"{self.base_url}/api/records",
                    params=params, headers=self._headers(),
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
            log.info("se-key sync ok: seen=%d changed=%d",
                     result.rows_seen, result.rows_changed)
        except Exception as e:
            log.exception("se-key sync failed")
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
                # se-key's `survey_no` actually contains the INVOICE number
                # (SEABI-xxx/SESV-xxx). Map it to invoice_canonical so joins
                # with stage_approved/stage_debt line up.
                invoice_raw = r.get("survey_no") or ""
                claim_canon = canonical_claim(claim_raw)
                if not claim_canon:
                    continue
                invoice_canon = canonical_invoice(invoice_raw) or ""

                cur = conn.execute(
                    """INSERT INTO stage_keyed
                          (claim_canonical, invoice_canonical, survey_canonical, source_id,
                           claim_display, invoice_display, survey_display, created_at,
                           keyer, work_type, invoice_mix,
                           isurvey_sent, retry_count, synced_at)
                       VALUES (?, ?, '', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(claim_canonical, invoice_canonical, source_id)
                       DO UPDATE SET
                           claim_display=excluded.claim_display,
                           invoice_display=excluded.invoice_display,
                           created_at=excluded.created_at,
                           keyer=excluded.keyer,
                           work_type=excluded.work_type,
                           invoice_mix=excluded.invoice_mix,
                           isurvey_sent=excluded.isurvey_sent,
                           retry_count=excluded.retry_count,
                           synced_at=excluded.synced_at""",
                    (
                        claim_canon, invoice_canon, int(r.get("id") or 0),
                        claim_raw, invoice_raw,
                        r.get("created_at"),
                        r.get("keyer") or "",
                        r.get("work_type") or "",
                        r.get("invoice_mix") or "",
                        int(r.get("isurvey_sent") or 0),
                        int(r.get("retry_count") or 0),
                        synced_at,
                    ),
                )
                changed += cur.rowcount
                touched.add(claim_canon)
        return changed
