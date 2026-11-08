"""Local copy of the CardTrader catalog and printing lookup."""

import re

_NUMBER = re.compile(r"^0*(\d+)(.*)$")


def normalize_number(value):
    """Canonical collector number: no spaces, no leading zeros, upper case ("012a" -> "12A")."""
    value = (value or "").strip().replace(" ", "").upper()
    m = _NUMBER.match(value)
    if m:
        return (m.group(1) or "0") + m.group(2)
    return value
