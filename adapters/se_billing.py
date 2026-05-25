"""se-billing adapter: GET /api/captures, upsert into stage_closed.

se-billing's API doesn't expose since_id, so we paginate by offset and let the
upsert's ON CONFLICT keep things idempotent. For large datasets this is fine —
captures grow slowly (1-2 per surveyed job).
"""
from __future__ import annotations

import logging
import os

import requests

import db
from adapters.base import SyncAdapter, SyncResult, utc_now_iso
from normalize import canonical_claim, canonical_invoice

log = logging.getLogger("se-tracking.adapter.se_billing")

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30


class SeBillingAdapter(SyncAdapter):
    name = "se-billing"

    def __init__(self):
        self.base_url = os.getenv("SE_BILLING_URL", "http://localhost:3200").rstrip("/")
        self.token = os.getenv("SE_BILLING_TOKEN", "").strip()
        self.interval_minutes = int(os.getenv("SYNC_INTERVAL_MIN", "5"))

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def sync(self) -> SyncResult:
        started = utc_now_iso()
        run_id = db.record_sync_start(self.name, started)
        result = SyncResult(touched_claims=set())

        try:
            log.info("se-billing sync starting")
            offset = 0
            while True:
                r = requests.get(
                    f"{self.base_url}/api/captures",
                    params={"limit": PAGE_SIZE, "offset": offset},
                    headers=self._headers(), timeout=REQUEST_TIMEOUT,
                )
                r.raise_for_status()
                data = r.json()
                rows = data.get("rows", []) or []
                if not rows:
                    break

                changed = self._upsert(rows, result.touched_claims)
                result.rows_seen += len(rows)
                result.rows_changed += changed

                total = int(data.get("total") or 0)
                offset += len(rows)
                if len(rows) < PAGE_SIZE or offset >= total:
                    break

            db.record_sync_end(run_id, utc_now_iso(), "ok",
                               result.rows_seen, result.rows_changed)
            log.info("se-billing sync ok: seen=%d changed=%d",
                     result.rows_seen, result.rows_changed)
        except Exception as e:
            log.exception("se-billing sync failed")
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
                # se-billing's `survey_no` is the invoice number too (matches se-key).
                invoice_raw = r.get("survey_no") or ""
                claim_canon = canonical_claim(claim_raw)
                if not claim_canon:
                    continue
                invoice_canon = canonical_invoice(invoice_raw) or ""

                cur = conn.execute(
                    """INSERT INTO stage_closed
                          (claim_canonical, invoice_canonical, survey_canonical, source_id,
                           claim_display, invoice_display, survey_display, ts,
                           province_id, province_name, amphur_name,
                           surveyor_name, inspector_name, oss_company, is_se,
                           sur_invest, ins_invest, ins_trans, ins_photo,
                           out_of_area_amt, out_of_hours_amt, deduct_amt,
                           late_submit, incomplete_docs, synced_at)
                       VALUES (?, ?, '', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(claim_canonical, invoice_canonical, source_id)
                       DO UPDATE SET
                           claim_display=excluded.claim_display,
                           invoice_display=excluded.invoice_display,
                           ts=excluded.ts,
                           province_id=excluded.province_id,
                           province_name=excluded.province_name,
                           amphur_name=excluded.amphur_name,
                           surveyor_name=excluded.surveyor_name,
                           inspector_name=excluded.inspector_name,
                           oss_company=excluded.oss_company,
                           is_se=excluded.is_se,
                           sur_invest=excluded.sur_invest,
                           ins_invest=excluded.ins_invest,
                           ins_trans=excluded.ins_trans,
                           ins_photo=excluded.ins_photo,
                           out_of_area_amt=excluded.out_of_area_amt,
                           out_of_hours_amt=excluded.out_of_hours_amt,
                           deduct_amt=excluded.deduct_amt,
                           late_submit=excluded.late_submit,
                           incomplete_docs=excluded.incomplete_docs,
                           synced_at=excluded.synced_at""",
                    (
                        claim_canon, invoice_canon, int(r.get("id") or 0),
                        claim_raw, invoice_raw,
                        r.get("ts"),
                        r.get("province_id"), r.get("province_name"), r.get("amphur_name"),
                        r.get("surveyor_name"), r.get("inspector_name"),
                        r.get("oss_company"), int(r.get("is_se") or 0),
                        _i(r.get("sur_invest")), _i(r.get("ins_invest")),
                        _i(r.get("ins_trans")), _i(r.get("ins_photo")),
                        _i(r.get("out_of_area_amt")), _i(r.get("out_of_hours_amt")),
                        _i(r.get("deduct_amt")),
                        int(r.get("late_submit") or 0),
                        int(r.get("incomplete_docs") or 0),
                        synced_at,
                    ),
                )
                changed += cur.rowcount
                touched.add(claim_canon)
        return changed


def _i(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
