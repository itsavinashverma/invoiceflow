"""Money display helpers (formatting only; arithmetic stays in Decimal at the call sites)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}
_CENT = Decimal("0.01")


def _group_indian(integer_digits: str) -> str:
    """12345678 -> 1,23,45,678 (lakh/crore grouping)."""
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def format_money(amount: Decimal | None, currency: str | None = None) -> str:
    """``₹1,00,000.00``, ``$1,216.00``, ``CHF 12.50``. ``None`` becomes ``n/a``."""
    if amount is None:
        return "n/a"
    q = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):.2f}".partition(".")
    body = _group_indian(whole) if currency == "INR" else f"{int(whole):,}"
    symbol = _SYMBOLS.get(currency or "")
    prefix = symbol if symbol else (f"{currency} " if currency else "")
    return f"{sign}{prefix}{body}.{frac}"
