"""Parse uploaded Excel files into debt_records rows.

Columns are located from each sheet's header row ("CLAIM NO." /
"เลขที่ใบแจ้งหนี้" / "AMT.") — some rounds omit the PAYMENT # column, which
shifts everything one column left. Sheets without that header fall back to
the legacy `extract_ตัดหนี้.py` layout:
    Column C (index 2) = CLAIM NO.
    Column D (index 3) = เลขที่ใบแจ้งหนี้
    Column F (index 5) = AMT.
Cut date: scan first 5 rows for "เช็ค DD/M/YYYY".
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import IO

import openpyxl

CLAIM_PATTERN = re.compile(r"^\d{4}/[0-9A-Za-z]+$")
DATE_PATTERN = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")

_LEGACY_COLS = (2, 3, 5)   # claim, invoice, amount
_HEADER_SCAN_ROWS = 10


def _find_columns(rows) -> tuple[int, int, int]:
    """(claim, invoice, amount) indexes from the header row, else legacy layout."""
    for row in rows[:_HEADER_SCAN_ROWS]:
        keys = [re.sub(r"[\s.]", "", str(c)).upper() if c is not None else "" for c in row]
        if "CLAIMNO" in keys and "เลขที่ใบแจ้งหนี้" in keys and "AMT" in keys:
            return keys.index("CLAIMNO"), keys.index("เลขที่ใบแจ้งหนี้"), keys.index("AMT")
    return _LEGACY_COLS


def _clean_claim(v):
    if v is None:
        return None
    s = str(v).strip()
    if not CLAIM_PATTERN.match(s):
        return None
    return s.replace("/", "")


def _clean_invoice(v):
    if v is None:
        return None
    s = str(v).strip()
    if s.startswith("'"):
        s = s[1:]
    return s or None


def _clean_amount(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_excel(source: bytes | IO[bytes], filename: str | None = None) -> tuple[list[dict], int]:
    """Parse one xlsx blob → (records, skipped_count).

    `source` can be bytes or a file-like object. `filename` is used as the
    `source_file` column on each record.
    """
    if isinstance(source, (bytes, bytearray)):
        wb = openpyxl.load_workbook(BytesIO(source), data_only=True, read_only=True)
    else:
        wb = openpyxl.load_workbook(source, data_only=True, read_only=True)

    records: list[dict] = []
    skipped = 0

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows = [list(r) for r in ws.iter_rows(values_only=True)]

        cut_date = None
        for r in all_rows[:5]:
            for cell in r:
                if cell is None:
                    continue
                s = str(cell)
                if "เช็ค" in s:
                    m = DATE_PATTERN.search(s)
                    if m:
                        cut_date = m.group(1)
                        break
            if cut_date:
                break

        claim_i, invoice_i, amount_i = _find_columns(all_rows)
        width = max(claim_i, invoice_i, amount_i) + 1
        for row in all_rows:
            if len(row) < width:
                continue
            claim = _clean_claim(row[claim_i])
            invoice = _clean_invoice(row[invoice_i])
            amount = _clean_amount(row[amount_i])
            if not claim or not invoice:
                skipped += 1
                continue
            records.append({
                "claim": claim,
                "invoice": invoice,
                "amount": amount,
                "cut_date": cut_date,
                "source_file": filename,
                "sheet": sheet_name,
            })

    wb.close()
    return records, skipped
