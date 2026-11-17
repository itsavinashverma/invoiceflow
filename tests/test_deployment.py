"""Deployment readiness: configuration sources, the access gate, hosted-storage warnings, safe error output,
and repository hygiene (no secrets, no oversized files, correct Streamlit Cloud files)."""

import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from streamlit.proto.TextInput_pb2 import TextInput as TextInputProto
from streamlit.testing.v1 import AppTest
from ui_helpers import alltext, no_exceptions, page

from invoiceflow import config
from invoiceflow.config import is_hosted_environment, load_settings, storage_warning
from invoiceflow.database import connection
from invoiceflow.database.connection import session_scope
from invoiceflow.database.models import ProcessingRun
from invoiceflow.ui import access

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app.py")
CODE = "open-sesame-4821"


@pytest.fixture(autouse=True)
def _fresh_lockout():
    access.reset_lockout()
    yield
    access.reset_lockout()


def everything_shown(at: AppTest) -> str:
    """Every piece of text a visitor could see, including widget values and labels."""
    parts = [alltext(at)]
    parts += [str(e.value) for e in at.text_input] + [e.label for e in at.text_input]
    parts += [t.value for t in at.title] + [s.value for s in at.subheader] + [b.label for b in at.button]
    return "\n".join(parts)


# ============================================================================ configuration sources


def test_access_code_setting_is_read_but_never_summarised(clean_env, monkeypatch):
    assert load_settings(clean_env).access_code is None and not load_settings(clean_env).has_access_gate
    monkeypatch.setenv("ACCESS_CODE", "   ")  # blank is "not set"
    assert load_settings(clean_env).access_code is None
    monkeypatch.setenv("ACCESS_CODE", CODE)
    s = load_settings(clean_env)
    assert s.access_code == CODE and s.has_access_gate
    assert s.safe_summary()["access_gate"] == "enabled" and CODE not in " ".join(s.safe_summary().values())


def test_hosted_environment_detection(monkeypatch):
    monkeypatch.delenv("INVOICEFLOW_HOSTED", raising=False)
    assert is_hosted_environment() is False  # this checkout is not under /mount/src
    monkeypatch.setattr(config, "PROJECT_ROOT", Path("/mount/src/invoiceflow"))  # where Streamlit Community Cloud mounts the repo
    assert is_hosted_environment() is True
    monkeypatch.setattr(config, "PROJECT_ROOT", ROOT)
    monkeypatch.setenv("INVOICEFLOW_HOSTED", "1")
    assert is_hosted_environment() is True


def test_sqlite_on_a_hosted_platform_is_flagged_as_not_durable(clean_env, monkeypatch):
    sqlite = load_settings(clean_env)
    monkeypatch.setattr(config, "is_hosted_environment", lambda: True)
    assert "erased" in storage_warning(sqlite) and "DATABASE_URL" in storage_warning(sqlite)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:6543/db")
    assert storage_warning(load_settings(clean_env)) is None  # PostgreSQL is durable
    monkeypatch.setattr(config, "is_hosted_environment", lambda: False)
    monkeypatch.delenv("DATABASE_URL")
    assert storage_warning(load_settings(clean_env)) is None  # local SQLite is fine


def test_streamlit_secrets_configure_the_real_app_and_environment_wins(ui_env, monkeypatch):
    """What Streamlit Community Cloud does: values come from st.secrets (top-level keys)."""
    monkeypatch.delenv("DATABASE_URL")  # not set in the environment: secrets must supply it
    url = ui_env.url.render_as_string(hide_password=False)
    at = AppTest.from_string("from invoiceflow.ui import health_page\nhealth_page.render()", default_timeout=60)
    at.secrets["GEMINI_API_KEY"] = "AIzaSyFROMSECRETSxxxxxxxxxxxxxxxxxxxxxxx"
    at.secrets["GEMINI_MODEL"] = "model-from-secrets"
    at.secrets["DATABASE_URL"] = url
    at.run()
    no_exceptions(at)
    text = everything_shown(at) + "\n".join(str(j.value) for j in at.json)
    assert "API key is configured" in text and "model-from-secrets" in text and "Connected and up to date" in text
    assert "AIzaSyFROMSECRETS" not in text  # configured from secrets, still never displayed

    monkeypatch.setenv("GEMINI_MODEL", "model-from-environment")  # an environment variable beats a secret
    at2 = AppTest.from_string("from invoiceflow.ui import health_page\nhealth_page.render()", default_timeout=60)
    at2.secrets["GEMINI_MODEL"] = "model-from-secrets"
    at2.secrets["DATABASE_URL"] = url
    at2.run()
    assert "model-from-environment" in everything_shown(at2)


def test_a_fresh_deployment_initialises_with_no_configuration_at_all(clean_env, monkeypatch, tmp_path):
    """No key, no .env, a database location whose folders do not exist yet: the app must still come up cleanly."""
    monkeypatch.setattr("invoiceflow.config.PROJECT_ROOT", tmp_path)
    target = tmp_path / "does" / "not" / "exist" / "yet" / "invoiceflow.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target.as_posix()}")
    connection.reset_engine_cache()
    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        no_exceptions(at)
        assert at.title[0].value == "Dashboard" and target.exists()  # folders and schema were created
        health = page("health_page")
        text = everything_shown(health)
        assert "Connected and up to date" in text and "GEMINI_API_KEY is not set" in text  # degraded honestly, not broken
    finally:
        connection.reset_engine_cache()


# ============================================================================ the access gate


@pytest.fixture
def gated(ui_env, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", CODE)
    return ui_env


def submit(at: AppTest, code: str) -> AppTest:
    at.text_input[0].set_value(code)
    at.button[0].click().run()
    return at


def test_the_gate_blocks_everything_until_the_code_is_entered(gated):
    at = AppTest.from_file(APP, default_timeout=60).run()
    no_exceptions(at)
    assert [t.label for t in at.text_input] == ["Access code"] and at.text_input[0].proto.type == TextInputProto.PASSWORD  # masked input
    assert not at.file_uploader and not at.sidebar.markdown and "Dashboard" not in [t.value for t in at.title][1:]
    assert CODE not in everything_shown(at)  # the secret is never rendered

    wrong = submit(at, "not-the-code")
    assert any("Incorrect access code" in e.value for e in wrong.error) and wrong.text_input[0].label == "Access code"

    granted = submit(wrong, CODE)
    no_exceptions(granted)
    assert granted.title[0].value == "Dashboard" and not granted.text_input  # the gate is gone
    granted.run()  # reruns keep the session authorised
    assert granted.title[0].value == "Dashboard"
    assert CODE not in everything_shown(granted)


def test_the_gate_protects_every_page_of_the_real_app(gated):
    """Pages are only reachable through the shell, so the shell's gate covers uploads and history alike."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.file_uploader and not at.dataframe and not at.get("download_button")


def test_repeated_wrong_codes_lock_the_session(gated):
    at = AppTest.from_file(APP, default_timeout=60).run()
    for _ in range(access.MAX_ATTEMPTS_PER_SESSION):
        submit(at, "wrong")
    locked = submit(at, CODE)  # even the right code is refused once the session is locked
    assert any("Too many incorrect attempts" in e.value for e in locked.error) and not locked.text_input


def test_many_failures_across_visitors_lock_the_gate_for_everyone(gated):
    access._failures.extend([time.monotonic()] * access.LOCKOUT_FAILURES)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert any("Too many incorrect access codes" in e.value for e in at.error) and not at.text_input
    access._failures[:] = [time.monotonic() - access.LOCKOUT_WINDOW_SECONDS - 1] * access.LOCKOUT_FAILURES  # the window passes
    assert at.run().text_input  # the form is offered again


def test_without_an_access_code_there_is_no_gate(ui_env):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert at.title[0].value == "Dashboard" and not at.text_input


# ============================================================================ hosted-storage warning and safe errors in the UI


def test_the_shell_warns_when_a_hosted_deployment_would_lose_its_data(ui_env, monkeypatch):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not any("erased" in w.value for w in at.warning)  # not hosted: no warning
    monkeypatch.setattr(config, "is_hosted_environment", lambda: True)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert any("erased whenever the app restarts" in w.value for w in at.warning)


def test_an_unexpected_crash_never_shows_internal_details(seeded, demo, monkeypatch):
    from invoiceflow.services.pipeline import InvoicePipeline
    from invoiceflow.ui import context

    def explode(self, *a, **k):
        raise ValueError("password=hunter2 internal detail /srv/app/secret.py")

    monkeypatch.setattr(InvoicePipeline, "process", explode)
    monkeypatch.setattr(context, "make_pipeline", lambda s: InvoicePipeline(s))
    at = AppTest.from_string("from invoiceflow.ui import process_page\nprocess_page.render()", default_timeout=60).run()
    at.file_uploader(key="uploader_0").upload("a.pdf", demo["standard"].path.read_bytes(), "application/pdf").run()
    at.button(key="process_btn").click().run()
    no_exceptions(at)
    shown = everything_shown(at)
    assert "could not be completed or recorded" in shown and "unexpected error" in shown
    assert "hunter2" not in shown and "secret.py" not in shown and "ValueError" not in shown
    with session_scope(seeded) as s:
        assert s.query(ProcessingRun).count() == 0  # nothing half-written


def test_the_server_limits_uploads_and_hides_error_details():
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["server"]["maxUploadSize"] == 10 and cfg["server"]["enableXsrfProtection"] is True and cfg["server"]["headless"] is True
    assert cfg["client"]["showErrorDetails"] == "none"  # visitors never see stack traces
    assert cfg["browser"]["gatherUsageStats"] is False
    assert load_settings(Path("missing.env")).max_upload_mb <= cfg["server"]["maxUploadSize"]
    assert not (set(cfg) & {"secrets"})  # no secrets section in a committed file


def test_health_page_warns_when_the_app_limit_exceeds_the_server_cap(ui_env, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "50")
    at = page("health_page")
    assert any("rejects uploads above 10 MB" in w.value for w in at.warning)


# ============================================================================ repository hygiene


def tracked_or_candidate_files() -> list[Path]:
    """Files that would be committed: tracked + untracked-not-ignored (via git), else everything outside ignored folders."""
    if shutil.which("git") and (ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return [ROOT / p for p in out.stdout.splitlines() if p and (ROOT / p).is_file()]
    skip = {".venv", ".git", "data", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and p.name != ".env"]


KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
DB_URL_PATTERN = re.compile(r"postgres(?:ql)?(?:\+\w+)?://([^:\s/]+):([^@\s]+)@")
PLACEHOLDER_WORDS = {"USER", "PASSWORD", "YOUR_PASSWORD", "YOUR_PROJECT_REF", "postgres.YOUR_PROJECT_REF", "postgres.PROJECT_REF"}


def is_placeholder(value: str) -> bool:
    """Documentation placeholders: <angle-bracketed>, YOUR_..., or a known template word. Real credentials never look like this."""
    return value in PLACEHOLDER_WORDS or "<" in value or "YOUR_" in value


def find_secrets(text: str, name: str = "file") -> list[str]:
    found = [f"{name}: Gemini-style key" for _ in KEY_PATTERN.findall(text)]
    for user, password in DB_URL_PATTERN.findall(text):
        if not (is_placeholder(user) and is_placeholder(password)):
            found.append(f"{name}: database URL with credentials")
    return found


def test_the_secret_scanner_itself_catches_real_looking_secrets_and_accepts_placeholders():
    realistic_key = "AIza" + "Sy" + "B" * 33
    assert find_secrets(f"GEMINI_API_KEY={realistic_key}")  # a real-looking key is flagged
    assert find_secrets(
        "DATABASE_URL=postgresql://postgres.abcdefgh:Sup3rS3cretPw@aws-0-eu.pooler.supabase.com:6543/postgres"
    )  # so is a real URL
    assert find_secrets("postgresql+psycopg://postgres.abcdefgh:<password>@host:6543/postgres")  # a real user is still flagged
    assert not find_secrets("postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres")
    assert not find_secrets(
        'DATABASE_URL = "postgresql+psycopg://postgres.YOUR_PROJECT_REF:YOUR_PASSWORD@aws-0-YOUR_REGION.pooler.supabase.com"'
    )


def test_no_real_looking_secrets_in_committable_files():
    offenders = []
    for path in tracked_or_candidate_files():
        if path.suffix.lower() in {".pdf", ".png", ".svg", ".ico"} or "tests" in path.relative_to(ROOT).parts:
            continue  # binaries; tests contain deliberately FAKE keys and passwords
        offenders += find_secrets(path.read_text(encoding="utf-8", errors="ignore"), path.name)
    assert offenders == []


def test_env_files_and_databases_are_ignored_and_never_committable():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for needed in (".env", ".streamlit/secrets.toml", "*.db", "*.sqlite3", ".venv/", "data/"):
        assert needed in ignore, f"{needed} missing from .gitignore"
    names = {p.name for p in tracked_or_candidate_files()}
    assert ".env" not in names and "secrets.toml" not in names and not any(n.endswith((".db", ".sqlite", ".sqlite3")) for n in names)
    assert ".env.example" in names and "secrets.toml.example" in names  # templates ship, real files do not


def test_no_oversized_files_are_committed():
    big = [(p.name, p.stat().st_size) for p in tracked_or_candidate_files() if p.stat().st_size > 1_000_000]
    assert big == []  # the largest sample PDF (a simulated scan) is well under 1 MB


def test_streamlit_cloud_files_are_in_place():
    assert (ROOT / "app.py").exists() and "app_shell.run()" in (ROOT / "app.py").read_text(encoding="utf-8")  # entry point
    packages = (ROOT / "packages.txt").read_text(encoding="utf-8").split()
    assert "tesseract-ocr" in packages and "tesseract-ocr-eng" in packages  # system dependency for OCR
    lines = [
        ln.strip() for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")
    ]
    unpinned = [ln for ln in lines if "==" not in ln]
    assert unpinned == [] and any(ln.startswith("psycopg") for ln in lines) and any(ln.startswith("streamlit==") for ln in lines)
    assert not any("pytest" in ln or "ruff" in ln or "playwright" in ln for ln in lines)  # dev tools stay out of the deployed app
    assert (ROOT / "sample_data" / "purchase_orders" / "purchase_orders.json").exists()  # the demo-PO loader needs it
    assert any((ROOT / "sample_data" / "invoices").glob("*.pdf"))  # and there are sample invoices to try


def test_templates_contain_only_placeholders():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("GEMINI_API_KEY", "DATABASE_URL", "ACCESS_CODE", "TESSERACT_CMD"):
        assert re.search(rf"(?m)^{key}=\s*$", env), f"{key} must be empty in .env.example"
    secrets = tomllib.loads((ROOT / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8"))  # also proves it is valid TOML
    assert secrets["GEMINI_API_KEY"] == "YOUR_KEY" and "YOUR_PASSWORD" in secrets["DATABASE_URL"]
    assert "ACCESS_CODE" not in secrets  # optional settings stay commented out in the template


def test_text_files_have_no_byte_order_mark():
    """A BOM breaks strict TOML parsing and can confuse tools that read requirements.txt or .gitignore line by line."""
    bad = []
    for path in tracked_or_candidate_files():
        if path.suffix.lower() not in {".pdf", ".png", ".ico", ".svg"} and path.read_bytes().startswith(b"\xef\xbb\xbf"):
            bad.append(path.relative_to(ROOT).as_posix())
    assert bad == []
