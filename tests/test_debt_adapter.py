"""DebtJsonAdapter — prefix-less ตัดหนี้ invoices must join the other stages' row."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import jobs
from adapters import debt_json

CLAIM = "2026013068868"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "tracking.db"))
    db.init_schema()


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"rows": self._rows}


def _serve(monkeypatch, rows):
    """Fake debt-api: one page of `rows`, then empty."""
    monkeypatch.setattr(
        debt_json.requests, "get",
        lambda url, params, **kw: _Resp(rows if params["offset"] == 0 else []),
    )


def _debt(invoice, claim=CLAIM):
    return {"claim": claim, "invoice": invoice, "amount": 1050.0,
            "cut_date": "2/10/2569", "source_file": "x.xlsx", "sheet": "26100020"}


def _seed_keyed(invoice, claim=CLAIM):
    with db.txn() as conn:
        conn.execute(
            """INSERT INTO stage_keyed (claim_canonical, invoice_canonical, source_id,
                                        claim_display, invoice_display, synced_at)
               VALUES (?, ?, 1, ?, ?, 'seed')""",
            (claim, invoice, claim, invoice),
        )


def _seed_debt(invoice, claim=CLAIM):
    with db.txn() as conn:
        conn.execute(
            """INSERT INTO stage_debt (claim_canonical, invoice_canonical, claim_display,
                                       invoice_display, synced_at)
               VALUES (?, ?, ?, ?, 'old-run')""",
            (claim, invoice, claim, invoice),
        )


def _debt_keys():
    conn = db.open_conn()
    try:
        return sorted(tuple(r) for r in conn.execute(
            "SELECT claim_canonical, invoice_canonical FROM stage_debt"))
    finally:
        conn.close()


def test_bare_invoice_resolves_to_prefixed_and_joins_one_row(monkeypatch):
    _seed_keyed("SEABI-120260800591")
    _serve(monkeypatch, [_debt("120260800591")])

    result = debt_json.DebtJsonAdapter().sync()
    assert result.error is None
    assert _debt_keys() == [(CLAIM, "SEABI-120260800591")]

    jobs.rebuild()
    conn = db.open_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs_view WHERE claim_canonical=?", (CLAIM,)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert (row["keyed"], row["debt"], row["current_status"]) == (1, 1, "ตัดหนี้")
    assert row["invoice_display"] == "SEABI-120260800591"
    assert row["debt_invoice"] == "120260800591"


def test_unmatched_invoice_kept_as_is(monkeypatch):
    _seed_keyed("SEABI-999999999999")
    _serve(monkeypatch, [_debt("120260800591")])

    debt_json.DebtJsonAdapter().sync()
    assert _debt_keys() == [(CLAIM, "120260800591")]


def test_stale_bare_row_from_old_sync_is_pruned(monkeypatch):
    _seed_keyed("SEABI-120260800591")
    _seed_debt("120260800591")          # left behind by the pre-fix adapter
    _serve(monkeypatch, [_debt("120260800591")])

    result = debt_json.DebtJsonAdapter().sync()
    assert _debt_keys() == [(CLAIM, "SEABI-120260800591")]
    assert CLAIM in result.touched_claims


def test_empty_pull_does_not_wipe_stage_debt(monkeypatch):
    _seed_debt("120260800591")
    _serve(monkeypatch, [])

    debt_json.DebtJsonAdapter().sync()
    assert _debt_keys() == [(CLAIM, "120260800591")]
