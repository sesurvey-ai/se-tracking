"""Identifier normalization for cross-source job matching.

Rules harvested from:
  - C:\\Users\\i9\\Desktop\\pw\\Hpw.py  (lines 427-432, 493-499)
  - C:\\Users\\i9\\Desktop\\วางบิลรับเช็ค\\injson\\extract_ตัดหนี้.py (lines 56-71)

Every adapter MUST normalize claim/survey/invoice on ingest. The display field
keeps the original raw value for UI.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# Mirror the union of strip sets used by pw and extract_ตัดหนี้:
#   pw: [/:*?"<>| ฺ ]   (note: includes Thai vowel ฺ U+0E3A)
#   extract_ตัดหนี้: just "/"
_CLAIM_STRIP_RE = re.compile(r'[/:*?"<>|ฺ\s]')
_INVOICE_STRIP_RE = re.compile(r'[/:*?"<>|\s]')

# Thai month abbreviations as they appear in pw filenames + ตัดหนี้.json output.
_THAI_MONTHS = {
    "ม.ค.": 1, "ม.ค": 1,
    "ก.พ.": 2, "ก.พ": 2,
    "มี.ค.": 3, "มี.ค": 3,
    "เม.ย.": 4, "เม.ย": 4,
    "พ.ค.": 5, "พ.ค": 5,
    "มิ.ย.": 6, "มิ.ย": 6,
    "ก.ค.": 7, "ก.ค": 7,
    "ส.ค.": 8, "ส.ค": 8,
    "ก.ย.": 9, "ก.ย": 9,
    "ต.ค.": 10, "ต.ค": 10,
    "พ.ย.": 11, "พ.ย": 11,
    "ธ.ค.": 12, "ธ.ค": 12,
}


def canonical_claim(raw) -> str | None:
    """Strip claim_no into joinable canonical form.

    Examples:
        "2025/013047387"          -> "2025013047387"
        "2025013047387"           -> "2025013047387"
        "  2025 / 013047387  "    -> "2025013047387"
        " 21BR10AVD-6904-001553 " -> "21BR10AVD-6904-001553" (dashes kept)
        ""                        -> None
        None                      -> None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _CLAIM_STRIP_RE.sub("", s)
    return s or None


def canonical_survey(raw) -> str | None:
    """Survey numbers: uppercase, no whitespace, keep dashes.

    Examples:
        "S68425065421"       -> "S68425065421"
        "sesv-69050007"      -> "SESV-69050007"
        "  SEABI-43426  "    -> "SEABI-43426"
    """
    if raw is None:
        return None
    s = re.sub(r"\s+", "", str(raw).strip()).upper()
    return s or None


def canonical_invoice(raw) -> str | None:
    """Invoice numbers: strip Excel leading quote, uppercase, keep dashes.

    Examples:
        "'190251000029"     -> "190251000029"
        "SEABI-434260500019" -> "SEABI-434260500019"
        "  68070002  "      -> "68070002"
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("'"):
        s = s[1:]
    s = _INVOICE_STRIP_RE.sub("", s).upper()
    return s or None


def parse_thai_be_date(s) -> date | None:
    """Parse Thai Buddhist-era date strings to a Gregorian `date`.

    Handles:
        "22/พ.ค./2569"  -> date(2026, 5, 22)
        "26/12/2568"    -> date(2025, 12, 26)
        "10/10/2568"    -> date(2025, 10, 10)
        " 22 / พ.ค. / 2569 " (with spaces) -> date(2026, 5, 22)
    """
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    # Try Thai-month-abbrev form first: DD/<month-abbrev>/YYYY
    m = re.match(r"^(\d{1,2})\s*/\s*([฀-๿.]+?)\s*/\s*(\d{4})$", raw)
    if m:
        month = _THAI_MONTHS.get(m.group(2))
        if not month:
            return None
        try:
            year_ad = int(m.group(3)) - 543
            return date(year_ad, month, int(m.group(1)))
        except ValueError:
            return None
    # Numeric form with BE year: DD/MM/YYYY (YYYY > 2400 = BE)
    m = re.match(r"^(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})$", raw)
    if m:
        try:
            year_raw = int(m.group(3))
            year_ad = year_raw - 543 if year_raw > 2400 else year_raw
            return date(year_ad, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def to_iso_date(s) -> str | None:
    """Best-effort conversion of any source timestamp to ISO date (YYYY-MM-DD).

    Accepts:
        ISO datetime: "2025-04-29T13:56:07.123" -> "2025-04-29"
        ISO datetime with localtime: "2025-04-29 13:56:07.123456" -> "2025-04-29"
        Thai BE: "22/พ.ค./2569" -> "2026-05-22"
        Numeric BE: "26/12/2568" -> "2025-12-26"
        datetime/date objects: returned as ISO
    """
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date().isoformat()
    if isinstance(s, date):
        return s.isoformat()
    raw = str(s).strip()
    if not raw:
        return None
    # ISO prefix check
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Thai BE forms
    d = parse_thai_be_date(raw)
    if d:
        return d.isoformat()
    return None


def thai_be_label(d) -> str | None:
    """Render a Gregorian date as Thai BE label "DD/MM/YYYY" (BE year).

    Used by export.py + dashboard date columns.
    """
    if d is None:
        return None
    if isinstance(d, str):
        iso = to_iso_date(d)
        if not iso:
            return None
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"
