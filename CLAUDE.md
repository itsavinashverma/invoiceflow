# CLAUDE.md — InvoiceFlow persistent instructions

InvoiceFlow is an AI-powered invoice-processing app (AI Solutions Analyst case study). It must **actually work**: no mockups, no simulated success.

## Stack
Python 3.12 · Streamlit · `google-genai` (Gemini Developer API) · Pydantic v2 · PyMuPDF · pytesseract + Tesseract OCR · Pillow · SQLAlchemy · SQLite (local) / Supabase PostgreSQL via psycopg v3 (hosted) · python-dotenv · pytest · ReportLab (synthetic PDFs).
Free tier / open source only. **No** LangChain, CrewAI, paid OCR, paid APIs or extra frameworks.

## Hard rules
1. The LLM only **extracts/structures** data. It never makes or influences the final approve/review decision. The decision engine is deterministic Python.
2. All uncertain financial cases → **human review**. Never auto-send payments or trigger external transactions.
3. Use `Decimal` (never `float`) for all money. Parse money from strings; quantize explicitly.
4. Never hardcode secrets. Config via env vars (`GEMINI_API_KEY`, `GEMINI_MODEL`, `DATABASE_URL`, `TESSERACT_CMD`). `.env` is git-ignored; ship `.env.example` only. Never log or print secrets.
5. Never assume an API call succeeded: check the real response, handle empty/blocked/malformed output, validate with Pydantic, retry with bounded backoff, then fail visibly.
6. Graceful fallbacks must be honest: surface the limitation in the UI/logs, never pretend.

## Architecture (keep modules separate)
Root `app.py` (Streamlit entry) + package `invoiceflow/`: `config` · `constants` · `schemas` (Pydantic) · `extraction/` (PyMuPDF text, OCR fallback) · `ai/` (Gemini client) · `database/` (SQLAlchemy models/connection/repositories/seed) · `services/` (normalizer, duplicate_detector, po_matcher, validation_engine, pipeline orchestrator emitting stage events) · `utils/` (logging, pdf_security) · `ui/` (Streamlit components/dashboard). `scripts/` for demo data + DB seeding; `sample_data/` for generated PDFs.
UI contains no business logic. Pipeline emits per-stage events consumed by the live view.

## Workflow for every stage
- Inspect existing files before editing; make only necessary changes.
- Run relevant tests; fix failures before moving on.
- Report files created/modified, plus any dependency/env setup.
- Never claim something works without having run it.
- Do not overwrite user files or delete data without asking.
- **Ask before**: git push, creating cloud resources (Supabase, Streamlit Cloud, GitHub repo), any external account action, installing system software.
- Prefer small, readable, typed functions; explicit exception handling; meaningful `logging`.

## Commands (Windows, PowerShell)
- Env: `uv venv --python 3.12 .venv` then `.venv\Scripts\activate`; `uv pip install -r requirements-dev.txt`
- Tests: `python -m pytest -q`
- App: `streamlit run app.py`
- Synthetic data: `python scripts/generate_demo_data.py`; DB init: `python scripts/seed_database.py`
- Lint/format: `ruff check .` and `ruff format --check .` (config in `ruff.toml`); both must be clean.

## Deployment (free: GitHub + Streamlit Community Cloud + Supabase + Gemini free tier)
- Guides: `docs/DEPLOYMENT.md` (steps), `docs/SECURITY_REVIEW.md` (findings). Entry point `app.py`; system package in `packages.txt`.
- Secrets only via env / Streamlit secrets; templates are `.env.example` and `.streamlit/secrets.toml.example` (placeholders only).
- This is a **demonstration with synthetic data**, not a production payment-approval system. Never add approve/pay actions without real authentication.
- **Never push, create accounts/projects/apps or deploy without explicit user approval.**
