from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_shell_renders_without_api_key(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr("invoiceflow.config.PROJECT_ROOT", tmp_path)  # ignore the developer's real .env
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value.endswith("InvoiceFlow")
    assert any("not set" in w.value for w in at.warning)
