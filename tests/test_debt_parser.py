"""debt-api/parser.py — columns come from the header row, legacy layout as fallback."""
import importlib.util
import os
from io import BytesIO

import openpyxl

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debt-api", "parser.py")
_spec = importlib.util.spec_from_file_location("debt_api_parser", _PATH)
xparser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xparser)

TITLE = [None, None, "บริษัท เอสอี เซอร์เวย์ แอนด์ คอนซัลแตนท์ จำกัด"]
CHEQUE = [None, None, "จ่ายค่าจัดการสินไหมรถยนต์  บจก.เอสอีเซอร์เวย์ เช็ค 2/10/2569"]
EXPECTED = [{
    "claim": "2026013068868", "invoice": "120260800591", "amount": 1050.0,
    "cut_date": "2/10/2569", "source_file": "f.xlsx", "sheet": "Sheet",
}]


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse(rows):
    records, _ = xparser.parse_excel(_xlsx(rows), filename="f.xlsx")
    return records


def test_standard_layout_with_payment_column():
    assert _parse([
        TITLE, CHEQUE,
        [None, "PAYMENT #", "CLAIM NO.", "เลขที่ใบแจ้งหนี้", "ทะเบียนรถ", "AMT."],
        [2000, 2026279278, "2026/013068868", "'120260800591", " 5ขฎ-5068", 1050, 1123.5],
    ]) == EXPECTED


def test_layout_without_payment_column_is_shifted_left():
    # e.g. "excel ตัดหนี้รอบรับเช็ค 10-7-69" — used to parse to 0 rows
    assert _parse([
        TITLE, CHEQUE,
        [None, "CLAIM NO.", "เลขที่ใบแจ้งหนี้", "ทะเบียนรถ", "AMT."],
        [101, "2026/013068868", "'120260800591", " 5ขฎ-5068", 1050, 1123.5],
    ]) == EXPECTED


def test_sheet_without_header_uses_legacy_columns():
    assert _parse([
        TITLE, CHEQUE,
        [2000, 2026279278, "2026/013068868", "'120260800591", " 5ขฎ-5068", 1050, 1123.5],
    ]) == EXPECTED
