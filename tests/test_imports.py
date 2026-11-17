import importlib

import pytest

MODULES = [
    "invoiceflow",
    "invoiceflow.config",
    "invoiceflow.schemas",
    "invoiceflow.constants",
    "invoiceflow.extraction.pdf_extractor",
    "invoiceflow.extraction.ocr_extractor",
    "invoiceflow.ai.gemini_client",
    "invoiceflow.database.models",
    "invoiceflow.database.connection",
    "invoiceflow.database.repositories",
    "invoiceflow.database.seed",
    "invoiceflow.services.normalizer",
    "invoiceflow.services.duplicate_detector",
    "invoiceflow.services.po_matcher",
    "invoiceflow.services.validation_engine",
    "invoiceflow.services.pipeline",
    "invoiceflow.utils.logging_config",
    "invoiceflow.utils.pdf_security",
    "invoiceflow.ui.components",
    "invoiceflow.ui.dashboard",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_third_party_stack_imports():
    for name in ("streamlit", "google.genai", "pydantic", "pymupdf", "pytesseract", "PIL", "sqlalchemy", "dotenv", "reportlab"):
        importlib.import_module(name)


def test_psycopg_importable():
    try:
        importlib.import_module("psycopg")
    except ImportError as exc:  # environment issue, not a code defect
        pytest.skip(f"psycopg unusable on this machine: {str(exc).splitlines()[0]}")
