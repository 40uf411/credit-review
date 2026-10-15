import pytest

from credit_review.parsing import ParseError, comparable, parse_address, parse_value


@pytest.mark.parametrize("text, expected", [
    ("3.190,07", 3190.07),        # payslip: Belgian thousands + decimal comma
    ("169.171", 169171.0),        # BECRIS: thousands only
    ("704.88", 704.88),           # application form: decimal point
    ("45788.05", 45788.05),
    ("1.851,73", 1851.73),
    ("324.548,45 EUR", 324548.45),
    ("- 416,94", 416.94),         # deduction printed with a sign: the amount is positive
    ("52.600", 52600.0),
])
def test_money(text, expected):
    assert parse_value(text, "money") == expected


@pytest.mark.parametrize("text, expected", [
    ("01/03/1975", "1975-03-01"),
    ("1/3/1975", "1975-03-01"),
    ("1975-03-01", "1975-03-01"),
    ("30/8/2026", "2026-08-30"),
    ("13 mei 2034", "2034-05-13"),        # Dutch month name on the EPC
])
def test_dates(text, expected):
    assert parse_value(text, "date") == expected


def test_pay_period_is_a_month():
    assert parse_value("01/05/2026 - 31/05/2026", "month") == "2026-05"
    assert parse_value("2026-05", "month") == "2026-05"


def test_national_number_formats_compare_equal():
    assert parse_value("75.03.01-632.61", "rrn") == parse_value("75030163261", "rrn") == "75030163261"
    with pytest.raises(ParseError):
        parse_value("7503016326", "rrn")


def test_addresses_in_both_layouts_compare_equal():
    a = parse_address("Antoniusring 31, 2000 Antwerpen")
    b = parse_address("Antoniusring 31 2000 Antwerpen")
    c = parse_address("Larssingel, 41, 1800, Vilvoorde")
    assert comparable(a, "address") == comparable(b, "address")
    assert comparable(a, "address") != comparable(c, "address")
    assert c == {"street": "Larssingel", "number": "41", "box": "", "postcode": "1800", "city": "Vilvoorde"}


def test_other_types():
    assert parse_value("van 1919 tot 1945", "year_range") == [1919, 1945]
    assert parse_value("1922", "year_range") == [1922, 1922]
    assert parse_value("198,30 m²", "area") == 198.3
    assert parse_value("0", "boolean") is False
    assert parse_value("true", "boolean") is True
    assert parse_value("4,0%", "percent") == 0.04
    assert parse_value("BE86 1296 0178 8955", "iban") == "BE86129601788955"
