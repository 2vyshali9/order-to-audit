from decimal import Decimal

import pytest

from app.money import (D, fy_of, inr, inr_plain, line_tax, rnd, rupees_to_paise, to_base, words_inr, fmt_qty)

FLOUR = {"name": "Flour", "base_unit": "g", "disp_unit": "kg", "factor": 1000, "carton_size": None}
FG = {"name": "Murukku", "base_unit": "packet", "disp_unit": "packet", "factor": 1, "carton_size": 20}
OIL = {"name": "Oil", "base_unit": "ml", "disp_unit": "L", "factor": 1000, "carton_size": None}


def test_no_float_drift_in_money():
    assert rupees_to_paise("0.1") + rupees_to_paise("0.2") == rupees_to_paise("0.3") == 30
    assert rupees_to_paise(2.5) == 250
    assert sum(rupees_to_paise("0.10") for _ in range(1000)) == 10000


def test_round_half_up_not_bankers():
    assert rnd(Decimal("0.5")) == 1 and rnd(Decimal("1.5")) == 2 and rnd(Decimal("2.5")) == 3


def test_unit_conversion():
    assert to_base("40", "kg", FLOUR) == 40000
    assert to_base("10.5", "L", OIL) == 10500
    assert to_base("1.2", "kg", FLOUR) == 1200
    assert to_base("250", "g", FLOUR) == 250
    assert to_base(3, "carton", FG) == 60            # carton of 20 packets
    assert fmt_qty(19500, OIL) == "19.5 L"
    assert fmt_qty(120000, FLOUR) == "120 kg"


def test_unit_conversion_rejects_bad_input():
    with pytest.raises(ValueError):
        to_base("0.0005", "kg", FLOUR)               # half a gram
    with pytest.raises(ValueError):
        to_base(1, "L", FLOUR)                       # wrong dimension


def test_gst_intra_state_splits_in_half():
    assert line_tax(750000, 500, True) == (18750, 18750, 0)          # benchmark invoice
    assert 750000 + 18750 * 2 == 787500


def test_gst_inter_state_is_igst():
    assert line_tax(600000, 500, False) == (0, 0, 30000)


def test_gst_odd_paise_rounds_half_up_each_leg():
    # 5% of Rs 0.30 = 1.5 paise -> halves are 0.75 paise -> 1 paisa each
    assert line_tax(30, 500, True) == (1, 1, 0)
    assert line_tax(10, 500, True) == (0, 0, 0)       # 0.25 paise each rounds to 0
    assert line_tax(10, 500, False) == (0, 0, 1)      # 0.5 paise rounds up


def test_financial_year_numbering_boundary():
    assert fy_of("2026-03-31") == "25-26" and fy_of("2026-04-01") == "26-27" and fy_of("2026-11-21") == "26-27"


def test_indian_formatting_and_words():
    assert inr_plain(787500) == "7,875.00" and inr(12345678) == "₹1,23,456.78"
    assert words_inr(787500) == "Seven Thousand Eight Hundred Seventy Five Rupees Only"
    assert words_inr(10050) == "One Hundred Rupees and Fifty Paise Only"
