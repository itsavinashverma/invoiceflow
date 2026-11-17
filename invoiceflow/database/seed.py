"""Seed data loader for sample purchase orders.

Idempotent by design: a PO is inserted only if its ``po_number`` is not already present. Existing rows
are never overwritten (they may have been edited since), so running the seed any number of times
creates no duplicates and destroys nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from invoiceflow.database.repositories import create_purchase_order_if_missing
from invoiceflow.schemas import PurchaseOrderData

logger = logging.getLogger(__name__)

# Located from this file, not from configuration, so it cannot move depending on import order or test patches.
DEFAULT_PO_FILE = Path(__file__).resolve().parents[2] / "sample_data" / "purchase_orders" / "purchase_orders.json"


class SeedDataError(ValueError):
    """The seed file is missing or malformed. ``str(exc)`` says which entry is wrong."""


@dataclass
class SeedResult:
    created: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{len(self.created)} purchase order(s) created, {len(self.already_present)} already present"


def load_purchase_orders(path: Path = DEFAULT_PO_FILE) -> list[PurchaseOrderData]:
    """Parse and validate the seed file. Money is read from strings, never through float."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), parse_float=str)
    except FileNotFoundError as exc:
        raise SeedDataError(f"Seed file not found: {path}. Generate it with: python scripts/generate_demo_data.py") from exc
    except json.JSONDecodeError as exc:
        raise SeedDataError(f"Seed file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise SeedDataError(f"Seed file {path} must contain a non-empty JSON list of purchase orders")

    items: list[PurchaseOrderData] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw, 1):
        try:
            data = PurchaseOrderData(
                po_number=entry["po_number"],
                vendor_name=entry["vendor"]["name"],
                currency=entry["currency"],
                total_amount=entry["total"],
                tolerance_percent=entry.get("tolerance_percent", "0.00"),
                po_date=entry["issue_date"],
                status=entry.get("status", "OPEN"),
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise SeedDataError(f"Purchase order #{i} in {path} is invalid: {exc}") from exc
        if data.po_number.casefold() in seen:
            raise SeedDataError(f"Duplicate po_number {data.po_number!r} in {path}")
        seen.add(data.po_number.casefold())
        items.append(data)
    return items


def seed_purchase_orders(session: Session, items: list[PurchaseOrderData] | None = None) -> SeedResult:
    """Insert any missing sample POs. The caller commits (``session_scope``)."""
    result = SeedResult()
    for data in items if items is not None else load_purchase_orders():
        _, created = create_purchase_order_if_missing(session, data)
        (result.created if created else result.already_present).append(data.po_number)
    logger.info("Seed: %s", result.summary())
    return result
