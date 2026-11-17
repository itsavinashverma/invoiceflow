# IMPLEMENTATION_PLAN.md

Each stage ends with: tests run and passing, files-changed report, no unverified claims.

## Stage 0 — Environment (needs user approval for installs)
- Create Python 3.12 venv via `uv`; install Tesseract via winget; create `.env` from `.env.example`.
- **Accept**: `python --version` = 3.12.x; `tesseract --version` works; imports of all libs succeed.

## Stage 1 — Scaffold
- `pyproject`/`requirements.txt`, `.gitignore` (`.env`, `*.db`, `.venv`), `.env.example`, package layout, `config.py`, logging, `.streamlit/config.toml`.
- **Accept**: `pytest` collects; config loads with and without `.env`; no secrets tracked.

## Stage 2 — Models & DB
- Pydantic models (Invoice, LineItem, PO, CheckResult, Decision, RunRecord); SQLAlchemy tables (vendors, purchase_orders, po_lines, invoices_processed, runs, run_events).
- **Accept**: CRUD tests on SQLite in-memory; Decimal round-trips exactly; Postgres URL accepted by engine factory.

## Stage 3 — Synthetic data
- ReportLab generator: POs seed + PDFs: happy path, amount mismatch, duplicate, scanned image-only PDF, missing-PO.
- **Accept**: PDFs open in PyMuPDF; scanned PDF has no text layer; seed loads into DB.

## Stage 4 — Extraction
- PyMuPDF text; OCR fallback with threshold and confidence; honest "OCR unavailable" path.
- **Accept**: digital PDF → method `text`; scanned → method `ocr` with text containing the invoice number; corrupt/empty/password PDF handled with clear errors.

## Stage 5 — Gemini integration
- List models, verify configured model; structured output with schema; response checks (blocked, empty, invalid JSON → bounded retry → failure); 429 handling.
- **Accept**: unit tests with a faked client for each failure mode; **live** test (opt-in marker) against real API returns a valid `Invoice` for a synthetic PDF.

## Stage 6 — Matching, validation, duplicates, decision
- Pure functions, Decimal math, rule IDs, reason codes.
- **Accept**: table-driven tests for each rule and boundary (tolerance edges); LLM output can never set the decision (test asserts decision depends only on checks).

## Stage 7 — Pipeline orchestrator
- Stages emit events (`started/succeeded/failed`, duration, detail); every run persisted, including failures.
- **Accept**: end-to-end test on all five synthetic invoices yields expected decisions (live Gemini test opt-in; fake-LLM test default).

## Stage 8 — Streamlit UI
- Pages: Process (upload + live stage view), Dashboard, Run history/detail, Data (POs). Sample-invoice quick buttons.
- **Accept**: app starts headless; run through the browser (or Streamlit `AppTest`) for upload → live stages → result; screenshots of edge cases.

## Stage 9 — Hardening
- Review for secrets, error paths, large/odd PDFs, rate limits; `/code-review` pass; fix findings.
- **Accept**: full test suite green; manual checklist of the four edge cases verified.

## Stage 10 — Docs & deployment config
- README (setup, architecture diagram, decision policy, limitations), `requirements.txt` pinned, `packages.txt` (`tesseract-ocr`) for Streamlit Cloud, secrets template, Supabase setup steps.
- **Accept**: fresh clone + README steps reproduce a working app locally.

## Stage 11 — Publish (only on explicit approval)
- `git init`, first commit, GitHub repo creation/push, Supabase project, Streamlit Cloud deploy.
- **Accept**: public URL processes a synthetic invoice and persists to Supabase; no secrets in repo history.

## Open decisions for the user
1. OK to install Python 3.12 (via uv) and Tesseract (via winget)?
2. Provide a Gemini API key (Google AI Studio free tier) in `.env`.
3. Git identity: keep `itsavinashverma` / `avinashverma70543@gmail.com`, or use another?
4. Proceed to Stage 0–1 when ready.
