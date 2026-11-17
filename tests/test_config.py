import pytest

from invoiceflow import constants
from invoiceflow.config import ConfigError, load_settings, normalize_database_url


def test_loads_without_api_key(clean_env):
    s = load_settings(clean_env)
    assert not s.has_gemini_key
    assert s.gemini_model == constants.DEFAULT_GEMINI_MODEL
    assert s.is_sqlite
    assert s.max_upload_mb == constants.DEFAULT_MAX_UPLOAD_MB
    assert s.max_pdf_pages == constants.DEFAULT_MAX_PDF_PAGES


def test_env_file_is_read(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=from-dotenv\nMAX_PDF_PAGES=5\n")
    s = load_settings(env)
    assert s.gemini_api_key == "from-dotenv"
    assert s.max_pdf_pages == 5


def test_real_env_beats_dotenv(clean_env, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEMINI_MODEL=from-file\n")
    monkeypatch.setenv("GEMINI_MODEL", "from-env")
    assert load_settings(env).gemini_model == "from-env"


def test_streamlit_secrets_used_when_env_missing(clean_env, monkeypatch):
    monkeypatch.setattr("invoiceflow.config._streamlit_secrets", lambda: {"GEMINI_API_KEY": "from-secrets"})
    assert load_settings(clean_env).gemini_api_key == "from-secrets"


def test_invalid_int_raises(clean_env, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "abc")
    with pytest.raises(ConfigError):
        load_settings(clean_env)
    monkeypatch.setenv("MAX_UPLOAD_MB", "0")
    with pytest.raises(ConfigError):
        load_settings(clean_env)


@pytest.mark.parametrize(
    "raw",
    [
        "postgres://u:p@h:6543/db",
        "postgresql://u:p@h:6543/db",
        "postgresql+psycopg2://u:p@h:6543/db",
        "postgresql+psycopg://u:p@h:6543/db",
    ],
)
def test_postgres_urls_use_psycopg(raw):
    assert normalize_database_url(raw) == "postgresql+psycopg://u:p@h:6543/db"


def test_sqlite_url_untouched():
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_safe_summary_hides_secrets(clean_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@host:6543/db")
    s = load_settings(clean_env)
    text = " ".join(s.safe_summary().values())
    assert "super-secret-key" not in text
    assert "hunter2" not in text and "user:" not in text
    assert "host:6543/db" in text
