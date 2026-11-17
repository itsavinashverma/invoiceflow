"""Initialise / migrate the database and load the sample purchase orders (safe to re-run).

Usage:
    python scripts/seed_database.py            # migrate + seed sample POs
    python scripts/seed_database.py --no-seed  # migrate only

Uses DATABASE_URL (SQLite file under data/ when unset). Existing rows are never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import SQLAlchemyError

from invoiceflow.config import load_settings
from invoiceflow.database.connection import (
    DatabaseUnavailableError,
    describe_target,
    get_engine,
    init_db,
    safe_error_reason,
    session_scope,
)
from invoiceflow.database.repositories import count_purchase_orders
from invoiceflow.database.seed import SeedDataError, seed_purchase_orders
from invoiceflow.utils.logging_config import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-seed", action="store_true", help="only create/migrate the schema")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    try:
        engine = init_db()
        print(f"Database ready: {describe_target(settings.database_url)}")
        if not args.no_seed:
            with session_scope(engine) as session:
                print(seed_purchase_orders(session).summary())
            with session_scope(engine) as session:
                print(f"Purchase orders in database: {count_purchase_orders(session)}")
    except (DatabaseUnavailableError, SeedDataError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"ERROR: database operation failed: {safe_error_reason(exc, get_engine())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
