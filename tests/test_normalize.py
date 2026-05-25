"""Unit tests for normalize.py — covers real cases from upstream sources."""
from datetime import date

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from normalize import (
    canonical_claim,
    canonical_survey,
    canonical_invoice,
    parse_thai_be_date,
    to_iso_date,
    thai_be_label,
)


# -- canonical_claim -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2025/013047387", "2025013047387"),     # pw + ตัดหนี้ format
    ("2025013047387", "2025013047387"),      # already clean
    ("  2025 / 013047387  ", "2025013047387"),
    ("2025ฺ013047387", "2025013047387"),  # Thai vowel ฺ U+0E3A stripped
    ("21BR10AVD-6904-001553", "21BR10AVD-6904-001553"),  # BR/SETP keeps dashes
    ("21BR10AVD-6904-001553 ", "21BR10AVD-6904-001553"),
    ("", None),
    (None, None),
    ("   ", None),
])
def test_canonical_claim(raw, expected):
    assert canonical_claim(raw) == expected


# -- canonical_survey ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("S68425065421", "S68425065421"),
    ("s68425065421", "S68425065421"),
    ("sesv-69050007", "SESV-69050007"),
    ("  SEABI-43426  ", "SEABI-43426"),
    ("", None),
    (None, None),
])
def test_canonical_survey(raw, expected):
    assert canonical_survey(raw) == expected


# -- canonical_invoice ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("'190251000029", "190251000029"),   # Excel leading-quote artifact
    ("190251000029", "190251000029"),
    ("SEABI-434260500019", "SEABI-434260500019"),
    ("  68070002  ", "68070002"),
    ("", None),
    (None, None),
])
def test_canonical_invoice(raw, expected):
    assert canonical_invoice(raw) == expected


# -- parse_thai_be_date --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("22/พ.ค./2569", date(2026, 5, 22)),
    ("26/12/2568", date(2025, 12, 26)),
    ("10/10/2568", date(2025, 10, 10)),
    ("1/ม.ค./2569", date(2026, 1, 1)),
    ("31/ธ.ค./2568", date(2025, 12, 31)),
    ("  22 / พ.ค. / 2569  ", date(2026, 5, 22)),
    # Edge: AD year (not BE) — keep as-is
    ("26/12/2025", date(2025, 12, 26)),
    # Garbage
    ("", None),
    (None, None),
    ("not a date", None),
])
def test_parse_thai_be_date(raw, expected):
    assert parse_thai_be_date(raw) == expected


# -- to_iso_date ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # ISO inputs
    ("2025-04-29", "2025-04-29"),
    ("2025-04-29T13:56:07.123", "2025-04-29"),
    ("2025-04-29 13:56:07.123456", "2025-04-29"),
    # Thai BE
    ("22/พ.ค./2569", "2026-05-22"),
    ("26/12/2568", "2025-12-26"),
    # datetime object
    (date(2025, 4, 29), "2025-04-29"),
    # Junk
    ("", None),
    (None, None),
])
def test_to_iso_date(raw, expected):
    assert to_iso_date(raw) == expected


# -- thai_be_label -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (date(2026, 5, 22), "22/05/2569"),
    ("2026-05-22", "22/05/2569"),
    ("2025-12-26", "26/12/2568"),
    (None, None),
    ("not a date", None),
])
def test_thai_be_label(raw, expected):
    assert thai_be_label(raw) == expected
