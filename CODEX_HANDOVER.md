# InvoiceFlow handover to Codex

Audit snapshot: 2026-11-19. The UTC date-filter test was corrected with user approval. PostgreSQL integration coverage and CI were then added. No application decision logic was changed.

## Current architecture

- Python 3.12.10 and Streamlit 1.67.0; `app.py` delegates to `invoiceflow.ui.app_shell`.
- SQLAlchemy 2.1.9 with versioned schema migrations; SQLite is the default local database and PostgreSQL is configured through `DATABASE_URL`.
- `invoiceflow.services.pipeline.InvoicePipeline` coordinates PDF intake, text extraction/OCR, Gemini extraction, schema validation, normalization, duplicate checks, PO matching, deterministic financial validation, decision, persistence, and stage events.
- PyMuPDF extracts digital text. `pytesseract` calls the locally installed Tesseract 5.4.0 for scanned pages.
- The Google `google-genai` SDK provides structured extraction. Pydantic validates its output. The model does not choose invoice decisions.
- The rules use `Decimal`; SQLAlchemy repositories persist invoices, runs, events, extraction records, and purchase orders.
- Streamlit pages cover processing, dashboard, history/details, purchase orders, manual entry, and system health.
- Demo PDFs and purchase orders are synthetic. Uploads are processed in memory; the application is labelled as a demonstration and does not pay or post invoices.

## Repository and environment

- Git branch: `main`; working tree was clean at audit start. `origin` is configured to the InvoiceFlow GitHub repository.
- The local virtual environment contains the pinned runtime and development dependencies. Tesseract is installed and available.
- Local settings resolve to PostgreSQL; the configured database reports schema version 4 and all six expected tables. PostgreSQL integration tests ran against unique temporary schemas, then dropped them; a follow-up query found zero leftover test schemas. The application's existing schema and records were not modified.
- A Gemini key is configured locally, but the live synthetic-invoice test received HTTP 504 after three retries. The access-code gate is disabled locally.
- A temporary SQLite database was used to start Streamlit. `/_stcore/health` returned HTTP 200 (`ok`).
- No deployed app URL is present in the repository, and no browser surface was available in the session. The public deployment, hosted app persistence, and visual browser check remain unverified.

## Implementation audit

| Component | Status | Evidence and issue |
|---|---|---|
| PDF upload and validation | COMPLETE AND VERIFIED | `invoiceflow/services/pipeline.py`, `invoiceflow/utils/pdf_security.py`; validation and UI tests passed. |
| Digital PDF extraction | COMPLETE AND VERIFIED | `invoiceflow/extraction/pdf_extractor.py`; extraction and scenario tests passed. |
| OCR | COMPLETE AND VERIFIED | Tesseract 5.4.0 is installed; scanned-PDF scenario tests ran with real OCR and passed. |
| AI extraction | IMPLEMENTED BUT UNVERIFIED | A real synthetic-invoice request was attempted; Gemini returned HTTP 504 after three attempts, so extraction could not be verified. |
| Schema validation | COMPLETE AND VERIFIED | Pydantic schemas and tests; AI response schema/failure cases passed. |
| Data normalization | COMPLETE AND VERIFIED | `invoiceflow/services/normalizer.py`; normalization tests passed. |
| Duplicate detection | COMPLETE AND VERIFIED | File hash and normalized vendor/invoice number; duplicate and concurrency scenarios passed. |
| PO matching | COMPLETE AND VERIFIED | Reference-based matching, vendor/status/currency checks, and suggestions for missing PO; matching tests passed. |
| Financial validation | COMPLETE AND VERIFIED | Decimal arithmetic, tax basis, tolerance, partial invoices and remaining PO capacity; validation/scenario tests passed. |
| Decision engine | COMPLETE AND VERIFIED | Deterministic checks and approval invariants; decision and fault-injection scenarios passed. |
| Database | COMPLETE AND VERIFIED | SQLite suite passed; PostgreSQL migrations, exact money/UTC round trips, and concurrent duplicate processing passed in isolated schemas. The test workflow starts PostgreSQL 16. |
| Live execution view | COMPLETE AND VERIFIED | Pipeline stage events are persisted and exposed in the UI; Streamlit AppTest coverage passed. |
| Dashboard | COMPLETE AND VERIFIED | Dashboard/reporting tests passed. |
| Processing history | COMPLETE AND VERIFIED | Reporting tests pass. Date-filter fixture bounds now derive from its UTC run timestamps, avoiding local-time dependence. |
| Edge cases | COMPLETE AND VERIFIED | Synthetic cases cover scanned PDFs, duplicate uploads, PO overage, missing PO, closed PO, currency/vendor mismatch and technical failures. |
| Tests and lint | COMPLETE AND VERIFIED | Offline suite and opt-in PostgreSQL suite passed; CI workflow runs both plus Ruff. The separate live Gemini check is currently unavailable (HTTP 504). |
| Visual UI check | IMPLEMENTED BUT UNVERIFIED | Streamlit AppTest coverage exists. A screenshot-based browser check could not run because no browser/UI surface was available. |
| Deployment | IMPLEMENTED BUT UNVERIFIED | Deployment files and guide exist; no hosted app URL was available for end-to-end persistence testing. The guide's push command matches `main`. |

## Test and runtime results

Commands run from `.venv`:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

- Initial pytest run: **495 passed, 1 failed, 1 deselected** (112.59 seconds). The sole failure was `tests/test_reporting.py::test_search_filters`: the test used local `date.today()` while `reporting.search_runs` filters inclusive UTC calendar days. At audit time, local time was 2026-11-19 00:39 +05:30 and UTC date was still 2026-11-18.
- Corrected pytest run: **496 passed, 1 deselected** (111.14 seconds). The fixture derives filter bounds from the UTC dates of its own run timestamps, including the case where fixture runs span UTC midnight.
- Focused reporting tests after the change: **9 passed**.
- PostgreSQL integration tests (`pytest tests/test_postgres_integration.py -m postgres -q`): **2 passed** against the configured PostgreSQL in unique temporary schemas; follow-up check found **0** leftover schemas.
- Live Gemini test (synthetic standard invoice, `pytest tests/test_ai_extraction.py -m live -q -rs`): **skipped** after Gemini returned HTTP 504 for three attempts. This is an unsuccessful verification, not a pass.
- Deployment config tests: **21 passed** after correcting the branch instruction.
- Ruff: clean; 86 files already formatted.
- App startup: Streamlit started against an isolated temporary SQLite database; health endpoint returned 200.
- Configured PostgreSQL: application schema v4 remained intact; integration tests created and removed only unique test schemas.
- Browser inventory returned no browser/app surfaces; native UI automation was unavailable. No visual screenshot was captured.
- OCR: Tesseract available; scanned-PDF tests passed.

## Known work and design notes

- The timezone-sensitive reporting test has been corrected. The history UI labels its date inputs “From (UTC)” and “To (UTC)”, matching the query semantics.
- Gemini live verification is pending a successful provider response; the observed failure was HTTP 504 after bounded retries.
- No hosted app URL was supplied or found in the repository, so deployment persistence could not be tested. `docs/DEPLOYMENT.md` now uses the current `main` branch in its push checklist.
- `PROJECT_SPEC.md` and parts of `IMPLEMENTATION_PLAN.md` still describe the initial scaffold as unfinished; update those planning notes as remaining work is completed.
- `.github/workflows/tests.yml` now runs PostgreSQL integration tests against a disposable PostgreSQL 16 service. GitHub Actions has not yet run this workflow.
- Visual browser review remains outstanding because the browser/UI tools were unavailable; the existing Streamlit AppTest checks are not a substitute for a screenshot review.

## Useful commands

```powershell
.venv\Scripts\Activate.ps1
python -m pytest -q
python -m pytest -q -m live       # makes a real Gemini request; requires explicit authorization for this audit
python -m pytest -m postgres -q   # requires INVOICEFLOW_TEST_POSTGRES_URL
ruff check .
ruff format --check .
streamlit run app.py
python scripts/seed_database.py
```

Use synthetic sample PDFs in `sample_data/invoices/`. Never report a live Gemini or deployment check as verified unless it actually succeeded.
