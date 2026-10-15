"""Deterministic parsers for values as printed on Belgian documents.

The model returns both the raw text and its own normalised value. Code parses the
raw text again; when both succeed and disagree, the code wins and the observation
is flagged. This catches the classic decimal-comma and date-order mistakes.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

MONTHS = {
    # Dutch
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7,
    "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
    # French
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7,
    "aout": 8, "septembre": 9, "octobre": 10, "decembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "august": 8, "october": 10,
}

TRUE_WORDS = {"true", "ja", "yes", "oui", "1", "✓", "x"}
FALSE_WORDS = {"false", "nee", "no", "non", "0", "✗"}


class ParseError(ValueError):
    pass


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


# ----------------------------------------------------------------------- numbers

def parse_number(text: str) -> float:
    """'3.190,07' -> 3190.07 · '169.171' -> 169171 · '704.88' -> 704.88 · '- 416,94 EUR' -> -416.94"""
    s = str(text).strip()
    negative = bool(re.match(r"^[-−–]", s))
    s = re.sub(r"[^\d.,]", "", s)
    if not s or not re.search(r"\d", s):
        raise ParseError(f"no number in {text!r}")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) and s.count(",") > 1 else s.replace(",", ".")
    elif "." in s:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
    value = float(s)
    return -value if negative else value


def parse_money(text: str) -> float:
    """Money amounts are stored positive; a deduction sign on a payslip is not a negative amount."""
    return round(abs(parse_number(text)), 2)


def parse_integer(text: str) -> int:
    value = parse_number(text)
    if value != int(value):
        raise ParseError(f"not a whole number: {text!r}")
    return int(value)


def parse_percent(text: str) -> float:
    s = str(text)
    value = parse_number(s)
    return round(value / 100, 6) if "%" in s or value > 1 else value


def parse_area(text: str) -> float:
    return round(parse_number(re.sub(r"m[²2]", "", str(text))), 2)


# -------------------------------------------------------------------------- dates

def _date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError as exc:
        raise ParseError(str(exc)) from exc


def find_dates(text: str) -> list[date]:
    s = _ascii(str(text)).casefold()
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s):
        found.append((m.start(), _date(int(m[1]), int(m[2]), int(m[3]))))
    for m in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", s):
        found.append((m.start(), _date(int(m[3]), int(m[2]), int(m[1]))))
    names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})\s+({names})\s+(\d{{4}})\b", s):
        found.append((m.start(), _date(int(m[3]), MONTHS[m[2]], int(m[1]))))
    return [d for _, d in sorted(found, key=lambda x: x[0])]


def parse_date(text: str) -> date:
    dates = find_dates(text)
    if not dates:
        raise ParseError(f"no date in {text!r}")
    return dates[0]


def parse_month(text: str) -> str:
    """A pay period: '01/05/2026 - 31/05/2026' -> '2026-05'."""
    s = str(text).strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    dates = find_dates(s)
    if not dates:
        raise ParseError(f"no month in {text!r}")
    return f"{dates[0].year:04d}-{dates[0].month:02d}"


# ------------------------------------------------------------------ identifiers

def parse_rrn(text: str) -> str:
    digits = re.sub(r"\D", "", str(text))
    if len(digits) != 11:
        raise ParseError(f"national number needs 11 digits: {text!r}")
    return digits


def parse_iban(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(text)).upper()


def parse_boolean(text: str) -> bool:
    s = str(text).strip().casefold()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise ParseError(f"not a yes/no value: {text!r}")


def parse_year_range(text: str) -> list[int]:
    years = [int(y) for y in re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", str(text))]
    if not years:
        raise ParseError(f"no year in {text!r}")
    return [min(years), max(years)]


# ---------------------------------------------------------------------- addresses

def parse_address(text: str) -> dict[str, str]:
    """'Antoniusring 31, 2000 Antwerpen' / 'Larssingel, 41, 1800, Vilvoorde' -> parts."""
    s = re.sub(r"\s+", " ", str(text)).strip()
    m = re.search(r"\b(\d{4})\b(?!.*\b\d{4}\b)", s)
    if not m:
        raise ParseError(f"no postcode in {text!r}")
    street_part = s[:m.start()].strip(" ,")
    city = s[m.end():].strip(" ,")
    box = ""
    if bm := re.search(r"\b(?:bus|bte|boite|boîte)\s*(\w+)\s*$", street_part, re.I):
        box, street_part = bm[1], street_part[:bm.start()].strip(" ,")
    nm = re.search(r"[, ]\s*(\d+[A-Za-z]?)\s*$", street_part)
    if not nm:
        raise ParseError(f"no house number in {text!r}")
    return {
        "street": street_part[:nm.start()].strip(" ,"),
        "number": nm[1],
        "box": box,
        "postcode": m[1],
        "city": city,
    }


def address_key(parts: dict[str, str]) -> str:
    street = re.sub(r"[^a-z0-9]", "", _ascii(parts["street"]).casefold())
    return f"{street}|{parts['number'].casefold()}|{parts['box'].casefold()}|{parts['postcode']}"


def format_address(parts: dict[str, str]) -> str:
    box = f" bus {parts['box']}" if parts.get("box") else ""
    return f"{parts['street']} {parts['number']}{box}, {parts['postcode']} {parts['city']}".strip()


# ------------------------------------------------------------------------ dispatch

def parse_value(text: str, field_type: str) -> Any:
    """Parse text into the canonical JSON-friendly value for a field type."""
    if text is None or str(text).strip() == "":
        raise ParseError("empty")
    t = field_type
    if t == "money":
        return parse_money(text)
    if t in ("number",):
        return round(parse_number(text), 6)
    if t == "integer":
        return parse_integer(text)
    if t == "percent":
        return parse_percent(text)
    if t == "area":
        return parse_area(text)
    if t == "date":
        return parse_date(text).isoformat()
    if t == "month":
        return parse_month(text)
    if t == "rrn":
        return parse_rrn(text)
    if t == "iban":
        return parse_iban(text)
    if t == "boolean":
        return parse_boolean(text)
    if t == "year_range":
        return parse_year_range(text)
    if t == "address":
        return parse_address(text)
    if t in ("name", "string"):
        return re.sub(r"\s+", " ", str(text)).strip()
    raise ParseError(f"no parser for type {t!r}")


# Types whose printed form a parser can read reliably. For the others (enum,
# name, string) the model's normalised value is used and checked differently.
CODE_PARSEABLE = {"money", "number", "integer", "percent", "area", "date", "month",
                  "rrn", "iban", "boolean", "year_range", "address"}


def comparable(value: Any, field_type: str) -> Any:
    """Canonical form used to decide whether two values are 'the same'."""
    if value is None:
        return None
    if field_type == "address" and isinstance(value, dict):
        return address_key(value)
    if field_type in ("name", "string", "enum"):
        return re.sub(r"[^a-z0-9]", "", _ascii(str(value)).casefold())
    if field_type in ("money", "number", "area", "percent"):
        return round(float(value), 2)
    if field_type == "year_range":
        return tuple(value)
    return value
