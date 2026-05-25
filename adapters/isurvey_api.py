"""iSurvey API adapter — pulls enquiry report directly from cloud.isurvey.mobi.

Fetches the same data that users see when they export Excel from iSurvey UI.
Filters to status == "จบงาน" and upserts into stage_closed.

Date strategy:
  - First run (no successful sync yet) → pull from ISURVEY_INITIAL_FROM (default 2023-01-01)
  - Subsequent runs → pull last 7 days (incremental, with overlap for stragglers)

source_id strategy:
  - Deterministic hash of (claim, invoice) offset to 2,000,000,000+
  - Distinct from real se-billing captures (positive small int, 1-1M)
  - Distinct from Excel historical (negative, -1 to -1M)
  - Idempotent: rerun → same source_id → ON CONFLICT UPDATE
  - jobs_view picks ORDER BY source_id DESC → API wins over Excel
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import date, datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import db
from adapters.base import SyncAdapter, SyncResult, utc_now_iso
from normalize import canonical_claim, canonical_invoice, to_iso_date

log = logging.getLogger("se-tracking.adapter.isurvey_api")

BASE_URL = "https://cloud.isurvey.mobi/web/php"
CHUNK_DAYS = 30
PAGE_LIMIT = 5000
REQUEST_TIMEOUT = 120
INITIAL_FROM_DEFAULT = "2023-01-01"
INCREMENTAL_DAYS = 7


def api_source_id(claim: str, invoice: str) -> int:
    """Deterministic positive source_id in range [2e9, ~1e12) for API rows.

    Uses MD5 of 'claim|invoice' truncated to 56 bits → 0..72e15, mod 1e12,
    then offset by 2e9 to guarantee positive AND distinct from se-billing
    real captures (id ~1-1M) and Excel historical (negative)."""
    s = f"{claim}|{invoice or ''}".encode()
    h = int.from_bytes(hashlib.md5(s).digest()[:7], "big")
    return 2_000_000_000 + (h % 1_000_000_000_000)


class IsurveyAPIAdapter(SyncAdapter):
    name = "isurvey-api"

    def __init__(self):
        self.username = os.getenv("ISURVEY_API_USERNAME", "").strip()
        self.password = os.getenv("ISURVEY_API_PASSWORD", "").strip()
        self.initial_from = os.getenv("ISURVEY_INITIAL_FROM", INITIAL_FROM_DEFAULT).strip()
        # Heavier than other adapters — default 60 min, overrideable
        self.interval_minutes = int(os.getenv("ISURVEY_API_INTERVAL_MIN", "60"))
        self._session: requests.Session | None = None
        self._session_lock = threading.Lock()
        self._logged_in = False

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def _get_session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        with self._session_lock:
            if self._session is not None:
                return self._session
            s = requests.Session()
            retry = Retry(
                total=2, backoff_factor=0.5,
                status_forcelist=[502, 503, 504],
                allowed_methods=["GET", "POST"],
            )
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
            self._session = s
            return s

    def _login(self):
        if self._logged_in:
            return
        s = self._get_session()
        r = s.post(
            f"{BASE_URL}/login.php",
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(
                f"iSurvey login failed: {payload.get('message') or 'check credentials'}"
            )
        if payload is None:
            body_lower = r.text.lower()
            if "<form" in body_lower and "password" in body_lower:
                raise RuntimeError("iSurvey login failed: HTML login form returned")
        self._logged_in = True
        log.info("iSurvey API login ok")

    def _get_page(self, params: dict) -> dict:
        self._login()
        s = self._get_session()

        def _do():
            r = s.get(
                f"{BASE_URL}/report/get_data_report.php",
                params=params, timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()

        try:
            return _do()
        except (requests.exceptions.HTTPError, ValueError):
            # session may have expired — re-login once
            self._logged_in = False
            self._login()
            return _do()

    def _fetch_chunk(self, df_str: str, dt_str: str) -> list[dict]:
        """Fetch ALL pages for one date-range chunk."""
        records: list[dict] = []
        page = 1
        start = 0
        while True:
            body = self._get_page({
                "con_date": 2,
                "date_from": df_str,
                "date_to": dt_str,
                "report_type": "enquiry",
                "page": page,
                "start": start,
                "limit": PAGE_LIMIT,
            })
            if isinstance(body, dict):
                batch = body.get("arr_data", body.get("data", []))
                total = body.get("total", body.get("totalCount", 0))
            else:
                batch = body
                total = len(body)
            records.extend(batch)
            if not batch or len(records) >= total:
                break
            page += 1
            start += PAGE_LIMIT
        return records

    def _determine_range(self) -> tuple[date, date]:
        """Return (from_date, to_date) — incremental if prior success exists, else initial."""
        conn = db.open_conn()
        try:
            row = conn.execute(
                "SELECT MAX(finished_at) AS m FROM sync_runs "
                "WHERE source=? AND status='ok' AND rows_seen > 0",
                (self.name,),
            ).fetchone()
        finally:
            conn.close()
        today = date.today()
        if row and row["m"]:
            return today - timedelta(days=INCREMENTAL_DAYS), today
        try:
            return date.fromisoformat(self.initial_from), today
        except ValueError:
            return date.fromisoformat(INITIAL_FROM_DEFAULT), today

    def sync(self) -> SyncResult:
        started = utc_now_iso()
        run_id = db.record_sync_start(self.name, started)
        result = SyncResult(touched_claims=set())

        try:
            if not self.enabled:
                db.record_sync_end(
                    run_id, utc_now_iso(), "ok", 0, 0,
                    error="DEFERRED: ISURVEY_API_USERNAME/PASSWORD not set",
                )
                log.info("isurvey-api deferred (no credentials)")
                return result

            from_date, to_date = self._determine_range()
            log.info("isurvey-api sync range: %s -> %s", from_date, to_date)

            cursor = from_date
            chunk_n = 0
            while cursor <= to_date:
                chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), to_date)
                df_str = cursor.strftime("%d/%m/%Y")
                dt_str = chunk_end.strftime("%d/%m/%Y")
                chunk_n += 1
                log.info("  chunk %d: %s -> %s", chunk_n, df_str, dt_str)
                records = self._fetch_chunk(df_str, dt_str)
                จบ = [r for r in records if str(r.get("stt_desc") or "").strip() == "จบงาน"]
                log.info("    fetched=%d  จบงาน=%d", len(records), len(จบ))
                changed = self._upsert(จบ, result.touched_claims)
                result.rows_seen += len(records)
                result.rows_changed += changed
                cursor = chunk_end + timedelta(days=1)

            db.record_sync_end(run_id, utc_now_iso(), "ok",
                               result.rows_seen, result.rows_changed)
            log.info("isurvey-api sync ok: seen=%d changed=%d",
                     result.rows_seen, result.rows_changed)
        except Exception as e:
            log.exception("isurvey-api sync failed")
            result.error = str(e)
            db.record_sync_end(run_id, utc_now_iso(), "error",
                               result.rows_seen, result.rows_changed, str(e))
        return result

    def _upsert(self, rows: list[dict], touched: set) -> int:
        synced_at = utc_now_iso()
        changed = 0
        with db.txn() as conn:
            for r in rows:
                claim_raw = r.get("claim_no") or ""
                invoice_raw = r.get("survey_no") or ""
                claim_canon = canonical_claim(claim_raw)
                if not claim_canon:
                    continue
                invoice_canon = canonical_invoice(invoice_raw) or ""
                sid = api_source_id(claim_canon, invoice_canon)

                # finish_dt is "YYYY-MM-DD HH:MM" — to_iso_date pulls just the date part
                ts_iso = to_iso_date(r.get("finish_dt")) or to_iso_date(r.get("checker_dt"))

                total = None
                try:
                    if r.get("ctotal") not in (None, ""):
                        total = int(float(r["ctotal"]))
                except (TypeError, ValueError):
                    pass

                cur = conn.execute(
                    """INSERT INTO stage_closed
                          (claim_canonical, invoice_canonical, survey_canonical, source_id,
                           claim_display, invoice_display, survey_display, ts,
                           province_id, province_name, amphur_name,
                           surveyor_name, inspector_name, oss_company, is_se,
                           sur_invest, ins_invest, ins_trans, ins_photo,
                           out_of_area_amt, out_of_hours_amt, deduct_amt,
                           late_submit, incomplete_docs, synced_at)
                       VALUES (?, ?, '', ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, 0,
                               NULL, ?, NULL, NULL, NULL, NULL, NULL, 0, 0, ?)
                       ON CONFLICT(claim_canonical, invoice_canonical, source_id)
                       DO UPDATE SET
                           claim_display=excluded.claim_display,
                           invoice_display=excluded.invoice_display,
                           ts=excluded.ts,
                           province_name=excluded.province_name,
                           amphur_name=excluded.amphur_name,
                           surveyor_name=excluded.surveyor_name,
                           inspector_name=excluded.inspector_name,
                           ins_invest=excluded.ins_invest,
                           synced_at=excluded.synced_at""",
                    (
                        claim_canon, invoice_canon, sid,
                        claim_raw, invoice_raw, ts_iso,
                        r.get("survey_province"), r.get("survey_amphur"),
                        r.get("empcode"), r.get("checkByName"),
                        total, synced_at,
                    ),
                )
                changed += cur.rowcount
                touched.add(claim_canon)
        return changed
