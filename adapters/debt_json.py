"""ตัดหนี้ adapter — reads from debt-api (HTTP).

Migrated from local SQLite file → HTTP API. The central debt-api stores
the canonical debt_records table; uploads happen through its /api/upload
endpoint (or POST /api/records/bulk).
"""
from __future__ import annotations

import logging
import os

import requests

import db
from adapters.base import SyncAdapter, SyncResult, utc_now_iso
from normalize import canonical_claim, canonical_invoice, parse_thai_be_date

log = logging.getLogger("se-tracking.adapter.debt")

PAGE_SIZE = 5000
REQUEST_TIMEOUT = 60


class DebtJsonAdapter(SyncAdapter):
    name = "debt"

    def __init__(self):
        self.base_url = os.getenv("DEBT_API_URL", "http://localhost:5600").rstrip("/")
        self.api_key = os.getenv("DEBT_API_KEY", "").strip()
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
        """We don't persist debt's source row id, so pull everything each time.
        debt-api is small (~100k rows); a full pull takes ~3 seconds."""
        return 0

    def sync(self) -> SyncResult:
        started = utc_now_iso()
        run_id = db.record_sync_start(self.name, started)
        result = SyncResult(touched_claims=set())

        try:
            log.info("debt sync starting (base=%s)", self.base_url)

            offset = 0
            while True:
                r = requests.get(
                    f"{self.base_url}/api/records",
                    params={"limit": PAGE_SIZE, "offset": offset},
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
            log.info("debt sync ok: seen=%d changed=%d",
                     result.rows_seen, result.rows_changed)
        except Exception as e:
            log.exception("debt sync failed")
            result.error = str(e)
            db.record_sync_end(run_id, utc_now_iso(), "error",
                               result.rows_seen, result.rows_changed, str(e))
        return result

    def _upsert(self, rows, touched: set) -> int:
        synced_at = utc_now_iso()
        changed = 0
        with db.txn() as conn:
            for r in rows:
                claim_raw = r.get("claim") or ""
                invoice_raw = r.get("invoice") or ""
                claim_canon = canonical_claim(claim_raw)
                invoice_canon = canonical_invoice(invoice_raw)
                if not claim_canon or not invoice_canon:
                    continue

                cut_date_be = r.get("cut_date")
                cut_iso = None
                if cut_date_be:
                    d = parse_thai_be_date(cut_date_be)
                    if d:
                        cut_iso = d.isoformat()

                amount = r.get("amount")
                if amount is not None:
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        amount = None

                cur = conn.execute(
                    """INSERT INTO stage_debt
                          (claim_canonical, invoice_canonical,
                           claim_display, invoice_display,
                           cut_date, cut_date_be, amount,
                           source_file, sheet, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(claim_canonical, invoice_canonical)
                       DO UPDATE SET
                           claim_display=excluded.claim_display,
                           invoice_display=excluded.invoice_display,
                           cut_date=excluded.cut_date,
                           cut_date_be=excluded.cut_date_be,
                           amount=excluded.amount,
                           source_file=excluded.source_file,
                           sheet=excluded.sheet,
                           synced_at=excluded.synced_at""",
                    (
                        claim_canon, invoice_canon,
                        claim_raw, invoice_raw,
                        cut_iso, cut_date_be, amount,
                        r.get("source_file"), r.get("sheet"),
                        synced_at,
                    ),
                )
                changed += cur.rowcount
                touched.add(claim_canon)
        return changed
