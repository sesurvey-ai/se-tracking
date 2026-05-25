"""Rebuild jobs_view by joining all stage_* tables.

Join key: (claim_canonical, invoice_canonical). All four stage_* tables have
both columns now (se-key/se-billing map their `survey_no` into invoice). The
esurvey ID is only present in stage_approved (pw) and is kept as a display
field.

Strategy: full rebuild after each sync run (fast for SQLite at <1M rows).
For very large datasets, pass touched_claims to rebuild_partial.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import db
from normalize import to_iso_date

log = logging.getLogger("se-tracking.jobs")


STATUS_ORDER = ["closed", "keyed", "approved", "debt"]
STATUS_LABELS = {
    "closed": "จบงาน",
    "keyed": "บันทึกงาน",
    "approved": "อนุมัติ",
    "debt": "ตัดหนี้",
}


# SQLite caps host parameters at 999 by default. Anything larger forces a
# full rebuild (which is fast — full rebuild reads each stage_* once).
_PARTIAL_MAX = 200


def rebuild(touched_claims: set | None = None) -> int:
    """Full or partial rebuild of jobs_view. Returns rows-written count."""
    rebuilt_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    if touched_claims and len(touched_claims) > _PARTIAL_MAX:
        log.info("partial rebuild skipped (%d touched > %d); doing full rebuild",
                 len(touched_claims), _PARTIAL_MAX)
        touched_claims = None

    # Step 1: collect all distinct (claim, invoice) pairs across the 4 sources.
    conn = db.open_conn()
    try:
        if touched_claims:
            placeholders = ",".join("?" * len(touched_claims))
            claim_filter = list(touched_claims)
            pairs_sql = f"""
                SELECT DISTINCT claim_canonical, invoice_canonical
                  FROM (
                      SELECT claim_canonical, invoice_canonical FROM stage_keyed    WHERE claim_canonical IN ({placeholders})
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_closed   WHERE claim_canonical IN ({placeholders})
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_approved WHERE claim_canonical IN ({placeholders})
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_debt     WHERE claim_canonical IN ({placeholders})
                  )
            """
            params = claim_filter * 4
        else:
            pairs_sql = """
                SELECT DISTINCT claim_canonical, invoice_canonical
                  FROM (
                      SELECT claim_canonical, invoice_canonical FROM stage_keyed
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_closed
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_approved
                      UNION
                      SELECT claim_canonical, invoice_canonical FROM stage_debt
                  )
            """
            params = []

        pairs = conn.execute(pairs_sql, params).fetchall()
        log.info("jobs rebuild: %d (claim, invoice) pairs", len(pairs))
    finally:
        conn.close()

    if not pairs:
        if touched_claims:
            with db.txn() as conn:
                placeholders = ",".join("?" * len(touched_claims))
                conn.execute(
                    f"DELETE FROM jobs_view WHERE claim_canonical IN ({placeholders})",
                    list(touched_claims),
                )
        return 0

    written = 0
    with db.txn() as conn:
        if touched_claims:
            placeholders = ",".join("?" * len(touched_claims))
            conn.execute(
                f"DELETE FROM jobs_view WHERE claim_canonical IN ({placeholders})",
                list(touched_claims),
            )
        else:
            conn.execute("DELETE FROM jobs_view")

        for pair in pairs:
            claim = pair["claim_canonical"]
            invoice = pair["invoice_canonical"] or ""
            row = _build_row(conn, claim, invoice, rebuilt_at)
            conn.execute(
                """INSERT INTO jobs_view
                      (claim_canonical, invoice_canonical,
                       claim_display, invoice_display, survey_display, survey_canonical,
                       keyed, keyed_at, keyed_keyer, keyed_work_type, keyed_sent,
                       closed, closed_at, closed_amount, closed_surveyor, closed_province,
                       approved, approved_at, approved_amount, approved_deduct,
                       debt, debt_cut_date, debt_amount, debt_invoice,
                       current_status, status_index, skipped_stages,
                       first_seen_at, last_updated_at, rebuilt_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            written += 1
    return written


def _build_row(conn, claim, invoice, rebuilt_at):
    """Compute the materialized row for one (claim, invoice) pair."""
    keyed_row = conn.execute(
        """SELECT * FROM stage_keyed
            WHERE claim_canonical=? AND invoice_canonical=?
            ORDER BY source_id DESC LIMIT 1""",
        (claim, invoice),
    ).fetchone()

    closed_row = conn.execute(
        """SELECT * FROM stage_closed
            WHERE claim_canonical=? AND invoice_canonical=?
            ORDER BY source_id DESC LIMIT 1""",
        (claim, invoice),
    ).fetchone()

    approved_row = conn.execute(
        """SELECT * FROM stage_approved
            WHERE claim_canonical=? AND invoice_canonical=?
            ORDER BY date_approve DESC LIMIT 1""",
        (claim, invoice),
    ).fetchone()

    debt_row = conn.execute(
        """SELECT * FROM stage_debt
            WHERE claim_canonical=? AND invoice_canonical=?
            ORDER BY COALESCE(cut_date, '0') DESC LIMIT 1""",
        (claim, invoice),
    ).fetchone()

    keyed = 1 if keyed_row else 0
    closed = 1 if closed_row else 0
    approved = 1 if approved_row else 0
    debt = 1 if debt_row else 0

    stages = {"keyed": keyed, "closed": closed, "approved": approved, "debt": debt}
    status_index = 0
    current_status = None
    for i, k in enumerate(STATUS_ORDER, start=1):
        if stages[k]:
            status_index = i
            current_status = STATUS_LABELS[k]

    skipped = [
        STATUS_LABELS[k]
        for k in STATUS_ORDER[: max(status_index - 1, 0)]
        if not stages[k]
    ]

    # Display: prefer raw from highest-stage source for claim/invoice;
    # survey_display only comes from pw (stage_approved).
    sources = [r for r in (debt_row, approved_row, closed_row, keyed_row) if r]
    claim_display = None
    invoice_display = None
    for r in sources:
        keys = r.keys()
        if not claim_display and "claim_display" in keys:
            claim_display = r["claim_display"]
        if not invoice_display and "invoice_display" in keys:
            invoice_display = r["invoice_display"]
        if claim_display and invoice_display:
            break
    if not claim_display:
        claim_display = claim
    if not invoice_display:
        invoice_display = invoice or None

    survey_display = approved_row["survey_display"] if approved_row else None
    survey_canon = approved_row["survey_canonical"] if approved_row else None

    candidates = []
    if debt_row and debt_row["cut_date"]:
        candidates.append(debt_row["cut_date"])
    if approved_row and approved_row["date_approve"]:
        candidates.append(to_iso_date(approved_row["date_approve"]))
    if closed_row and closed_row["ts"]:
        candidates.append(to_iso_date(closed_row["ts"]))
    if keyed_row and keyed_row["created_at"]:
        candidates.append(to_iso_date(keyed_row["created_at"]))

    last_updated_at = None
    first_seen_at = None
    for ts in candidates:
        if not ts:
            continue
        if last_updated_at is None or ts > last_updated_at:
            last_updated_at = ts
        if first_seen_at is None or ts < first_seen_at:
            first_seen_at = ts

    return (
        claim, invoice,
        claim_display, invoice_display, survey_display, survey_canon,
        keyed,
        to_iso_date(keyed_row["created_at"]) if keyed_row else None,
        keyed_row["keyer"] if keyed_row else None,
        keyed_row["work_type"] if keyed_row else None,
        keyed_row["isurvey_sent"] if keyed_row else None,
        closed,
        to_iso_date(closed_row["ts"]) if closed_row else None,
        _closed_total(closed_row) if closed_row else None,
        closed_row["surveyor_name"] if closed_row else None,
        closed_row["province_name"] if closed_row else None,
        approved,
        to_iso_date(approved_row["date_approve"]) if approved_row else None,
        approved_row["approve_amount"] if approved_row else None,
        approved_row["deduct_amount"] if approved_row else None,
        debt,
        debt_row["cut_date"] if debt_row else None,
        debt_row["amount"] if debt_row else None,
        debt_row["invoice_display"] if debt_row else None,
        current_status, status_index,
        json.dumps(skipped, ensure_ascii=False),
        first_seen_at, last_updated_at, rebuilt_at,
    )


def _closed_total(row) -> int | None:
    """Sum surveyor+inspector fee fields into a single 'closed_amount'."""
    parts = [
        row["sur_invest"], row["ins_invest"], row["ins_trans"], row["ins_photo"],
        row["out_of_area_amt"], row["out_of_hours_amt"],
    ]
    nums = [int(p) for p in parts if p is not None]
    if not nums:
        return None
    total = sum(nums)
    if row["deduct_amt"] is not None:
        total -= int(row["deduct_amt"])
    return total
