"""Money parsing and formatting.

Rules enforced here (and tested):

* Amounts are **integer minor units** everywhere inside the system. Floating
  point is never used for money: ``0.1 + 0.2`` problems become balance errors.
* Client input is a decimal *string* with at most two fraction digits, no
  sign, no exponent, no whitespace, no thousands separators.
* Only a narrow ASCII grammar is accepted, so locale-specific input such as
  ``"1.234,56"`` is rejected instead of being silently misread.
"""

from __future__ import annotations

import re

from .errors import validation_error

MINOR_UNITS_PER_MAJOR = 100

# 1..15 integer digits, optionally 1..2 fraction digits. No sign, no exponent.
_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]{0,14})(?:\.([0-9]{1,2}))?$")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# ISO 4217 currencies whose minor unit is not 1/100 are deliberately not
# supported: silently multiplying a JPY amount by 100 would be a money bug.
SUPPORTED_CURRENCIES = ("USD", "EUR", "GBP", "INR")


def parse_amount(value: object) -> int:
    """Parse a decimal string such as ``"12.34"`` into integer minor units.

    Raises ``AppError`` (HTTP 422) rather than ``ValueError`` so that every
    caller inherits a consistent API response.
    """
    if not isinstance(value, str):
        raise validation_error(
            "amount must be a string of digits with up to two decimal places, e.g. \"25.00\"."
        )
    text = value.strip()
    if text != value:
        raise validation_error("amount must not contain leading or trailing whitespace.")
    match = _AMOUNT_RE.match(text)
    if not match:
        raise validation_error(
            "amount must look like \"25\", \"25.5\" or \"25.50\" "
            "(no sign, no exponent, at most two decimal places)."
        )
    whole, fraction = match.group(1), match.group(2) or ""
    minor = int(whole) * MINOR_UNITS_PER_MAJOR + int(fraction.ljust(2, "0") or "0")
    return minor


def validate_currency(currency: object) -> str:
    if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
        raise validation_error("currency must be a three-letter ISO code, e.g. \"USD\".")
    if currency not in SUPPORTED_CURRENCIES:
        raise validation_error(
            "unsupported currency; this build handles " + ", ".join(SUPPORTED_CURRENCIES) + "."
        )
    return currency


def format_amount(minor: int) -> str:
    """Format integer minor units as a decimal string (never a float)."""
    if not isinstance(minor, int):
        raise TypeError("format_amount expects integer minor units")
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // MINOR_UNITS_PER_MAJOR}.{minor % MINOR_UNITS_PER_MAJOR:02d}"
