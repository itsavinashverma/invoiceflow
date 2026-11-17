"""Custom column types that keep money exact and timestamps unambiguous on every database."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class DecimalType(TypeDecorator):
    """Exact decimal storage.

    PostgreSQL: ``NUMERIC(precision, scale)``. SQLite has no decimal type and SQLAlchemy's
    ``Numeric`` would round-trip through ``float`` there, so SQLite stores the canonical decimal
    *string* instead. Values with more decimal places than ``scale`` are rejected, never rounded
    silently (quantize explicitly before storing). Do not compare or sum these columns in SQL:
    aggregate in Python with ``Decimal`` (text columns do not sort numerically on SQLite).
    """

    impl = String
    cache_ok = True

    def __init__(self, precision: int = 18, scale: int = 2) -> None:
        super().__init__()
        self.precision, self.scale = precision, scale
        self._quantum = Decimal(1).scaleb(-scale)

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self.precision + 4))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        if isinstance(value, (float, bool)):
            raise TypeError("Money columns accept Decimal (or str), never float")
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        quantized = dec.quantize(self._quantum, rounding=ROUND_HALF_UP)
        if quantized != dec:
            raise ValueError(f"{dec} has more than {self.scale} decimal places; quantize it explicitly first")
        if len(quantized.as_tuple().digits) > self.precision:
            raise ValueError(f"{dec} exceeds NUMERIC({self.precision},{self.scale})")
        return str(quantized) if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Dialect):
        return None if value is None else Decimal(str(value)).quantize(self._quantum)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite would otherwise return naive values)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime rejected; use timezone-aware datetimes (UTC)")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)
