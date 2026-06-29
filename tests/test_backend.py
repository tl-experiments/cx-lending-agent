"""
Unit tests for the trickiest pure logic in the mock lending backend:
the Indian digit-grouping formatter (inr), the foreclosure/payoff math, the next-due-date
helper, and the server-side identity gate (_verify). Run:  make test  (or: pytest -q)

These run fully offline — no GCP, no network — by calling the functions directly.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402

KEY = app.API_KEY  # local-dev default ("demo-key-gcex") when no env override


# --------------------------- inr() Indian digit grouping ---------------------------
@pytest.mark.parametrize("n,expected", [
    (0, "₹0"),
    (5, "₹5"),
    (100, "₹100"),
    (999, "₹999"),
    (1000, "₹1,000"),
    (8980, "₹8,980"),
    (99999, "₹99,999"),
    (100000, "₹1,00,000"),          # 1 lakh
    (181240, "₹1,81,240"),
    (10000000, "₹1,00,00,000"),     # 1 crore
    (-181240, "-₹1,81,240"),        # negative sign before symbol
])
def test_inr_grouping(n, expected):
    assert app.inr(n) == expected


# --------------------------- payoff / foreclosure math ---------------------------
def test_payoff_math_tl1001():
    """2% foreclosure charge + ~1 month accrued interest, summed to the total."""
    q = app.get_payoff("TL-1001", phone_last4="4417", x_api_key=KEY)
    op = 181240
    assert q.outstanding_principal == op
    assert q.foreclosure_charge == round(op * 0.02)          # 3625
    assert q.accrued_interest == round(op * 16.5 / 100 / 12)  # 2492
    assert q.total_payoff_amount == op + q.foreclosure_charge + q.accrued_interest  # 187357
    assert q.total_payoff_amount == 187357
    assert q.total_payoff_amount_display == "₹1,87,357"
    # quote is valid for 7 days from today
    assert q.quote_valid_until == (date.today() + timedelta(days=7)).isoformat()


def test_account_summary_tl1001():
    a = app.get_account("TL-1001", phone_last4="4417", x_api_key=KEY)
    assert a.emi_amount == 8980 and a.emi_amount_display == "₹8,980"
    assert a.outstanding_principal == 181240 and a.outstanding_principal_display == "₹1,81,240"
    assert a.borrower_name == "Asha R."


# --------------------------- identity gate (_verify) ---------------------------
@pytest.mark.parametrize("loan,phone", [("TL-1001", "9999"), ("TL-1001", ""), ("TL-1002", "4417")])
def test_verification_blocks_wrong_or_missing_phone(loan, phone):
    with pytest.raises(HTTPException) as e:
        app.get_account(loan, phone_last4=phone, x_api_key=KEY)
    assert e.value.status_code == 403


def test_unknown_loan_is_404():
    with pytest.raises(HTTPException) as e:
        app.get_account("TL-9999", phone_last4="0000", x_api_key=KEY)
    assert e.value.status_code == 404  # 404 (not found) is checked before the 403 gate


def test_bad_api_key_is_401():
    with pytest.raises(HTTPException) as e:
        app.get_account("TL-1001", phone_last4="4417", x_api_key="wrong")
    assert e.value.status_code == 401


# --------------------------- next due date ---------------------------
def test_next_due_date_is_future_with_correct_day():
    d = app._next_due_date(5)
    assert d > date.today()
    assert d.day == 5


def test_next_due_date_clamps_to_real_month_end_not_flat_28():
    # A 31st-due loan in January keeps the 31st (the old flat-28 clamp silently lost 3 days)…
    assert app._next_due_date(31, today=date(2026, 1, 10)) == date(2026, 1, 31)
    # …and clamps to Feb's actual last day, not a hardcoded 28 (would be 29 in a leap year)
    assert app._next_due_date(31, today=date(2026, 2, 10)) == date(2026, 2, 28)
