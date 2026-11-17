"""Initialise the database schema. Seed data is added in the data-model stage.

Usage: python scripts/seed_database.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invoiceflow.database.connection import init_db  # noqa: E402
from invoiceflow.utils.logging_config import setup_logging  # noqa: E402


def main() -> None:
    setup_logging()
    engine = init_db()
    print(f"Initialised database: {engine.url.render_as_string(hide_password=True)}")


if __name__ == "__main__":
    main()
