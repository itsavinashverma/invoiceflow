import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONFIG_KEYS = (
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "DATABASE_URL",
    "MAX_UPLOAD_MB",
    "MAX_PDF_PAGES",
    "TESSERACT_CMD",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No config env vars and a nonexistent .env, so tests never see the developer's real setup."""
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "missing.env"


@pytest.fixture
def settings(clean_env):
    """Default settings (no .env), with Tesseract auto-detected as in a normal install."""
    from invoiceflow.config import load_settings

    return load_settings(clean_env)


@pytest.fixture
def db_engine(tmp_path):
    """A fresh, migrated SQLite file database."""
    from invoiceflow.database.connection import create_db_engine, init_db

    engine = create_db_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    """A session on ``db_engine``; committed at the end of the test."""
    from invoiceflow.database.connection import session_scope

    with session_scope(db_engine) as session:
        yield session


@pytest.fixture
def ui_env(clean_env, monkeypatch, tmp_path):
    """An empty, migrated database that the UI pages (via DATABASE_URL) will use. No developer .env is read."""
    from invoiceflow.database import connection
    from invoiceflow.database.connection import create_db_engine, init_db

    monkeypatch.setattr("invoiceflow.config.PROJECT_ROOT", tmp_path)
    url = f"sqlite:///{(tmp_path / 'ui.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    connection.reset_engine_cache()
    engine = create_db_engine(url)
    init_db(engine)
    yield engine
    connection.reset_engine_cache()
    engine.dispose()


@pytest.fixture
def seeded(ui_env):
    """``ui_env`` with the 12 synthetic demo purchase orders loaded."""
    from invoiceflow.database.connection import session_scope
    from invoiceflow.database.seed import seed_purchase_orders

    with session_scope(ui_env) as s:
        seed_purchase_orders(s)
    return ui_env


@pytest.fixture(autouse=True)
def offline_guard(request, monkeypatch):
    """Tests must never reach the real Gemini API. Only tests marked ``live`` may build a real client."""
    if request.node.get_closest_marker("live"):
        return

    def _forbidden(*args, **kwargs):
        raise AssertionError("A real google.genai.Client was constructed in an offline test")

    monkeypatch.setattr("google.genai.Client", _forbidden)


@pytest.fixture(scope="session")
def gen_module():
    """The real generator script, imported as a module (ground-truth data + PDF rendering)."""
    spec = importlib.util.spec_from_file_location("generate_demo_data", ROOT / "scripts" / "generate_demo_data.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_demo_data"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def demo(gen_module):
    """Synthetic documents generated fresh by the real generator script, keyed by purpose.

    Values are ``GeneratedInvoice`` ground-truth records (path + expected numbers).
    """
    module = gen_module
    import tempfile

    out = Path(tempfile.mkdtemp(prefix="invoiceflow_demo_"))
    generated, _ = module.generate_all(out)
    by_name = {g.path.name: g for g in generated}
    return {
        "standard": by_name["invoice_standard_digital.pdf"],
        "multipage": by_name["invoice_multipage.pdf"],
        "tax": by_name["invoice_tax_separated.pdf"],
        "missing_po": by_name["invoice_missing_po.pdf"],
        "scanned": by_name["invoice_scanned.pdf"],
        "all": generated,
    }


def tesseract_ready() -> bool:
    from invoiceflow.config import load_settings
    from invoiceflow.exceptions import OcrUnavailableError
    from invoiceflow.extraction.ocr_extractor import ensure_tesseract

    try:
        ensure_tesseract(load_settings(Path("nonexistent.env")).tesseract_cmd)
        return True
    except OcrUnavailableError:
        return False


requires_tesseract = pytest.mark.skipif(not tesseract_ready(), reason="Tesseract OCR is not installed")
