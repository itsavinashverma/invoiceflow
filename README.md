# InvoiceFlow

**AI-assisted accounts-payable invoice processing, with deterministic, explainable decisions.**

> **Demonstration system, not a production payment-approval system.** InvoiceFlow uses synthetic data (fictional vendors,
> purchase orders and invoices). It never pays anything and never writes to an accounting system. It has no user accounts,
> so it offers no approve, reject or payment actions. Do not upload real invoices (see [Gemini and data privacy](#gemini-api-configuration)).

## Overview

InvoiceFlow takes a vendor invoice PDF, reads it (including scanned, image-only PDFs through OCR), extracts the invoice fields with
Google Gemini, checks them against a purchase order and the invoice history, and returns one of four outcomes with a full explanation:

| Outcome | Meaning |
|---|---|
| `APPROVED` | Cleared every automated check (vendor, PO, currency, arithmetic, balance, duplicates). Nothing is paid. |
| `REVIEW_REQUIRED` | Something is missing, mismatched or uncertain; a person must look. The reasons and the exact actions are listed. |
| `DUPLICATE_BLOCKED` | The same file, or the same vendor and invoice number, was already processed. |
| `PROCESSING_FAILED` | The document could not be processed (corrupt PDF, OCR unavailable, AI outage...). Never an approval. |

## Problem statement

Accounts-payable teams read vendor invoices by hand: find the vendor, invoice number, PO reference and amounts, locate the purchase
order, check what has already been billed against it, spot duplicates and arithmetic errors, and decide whether to approve.
It is slow and error-prone, and the costly mistakes (paying twice, paying over the PO, paying a look-alike vendor) are exactly the ones
people miss. InvoiceFlow automates the reading and the checking, and keeps a human in charge of anything uncertain.

## Features

- **Digital and scanned PDFs**: PyMuPDF text extraction with automatic per-page OCR fallback (Tesseract).
- **AI extraction with structured output** (Gemini), validated against a schema; the AI only reads, it never decides.
- **Deterministic decision engine**: PO matching, vendor comparison (legal form aware, no fuzzy matching), duplicate detection (file hash and vendor + invoice number),
  `Decimal` arithmetic, PO balance and tolerance, partial invoices against one PO, closed-PO and currency rules.
- **Explained decisions**: reasons, checks passed/failed/not run, PO balance working, and the human actions required.
- **Live execution view**: every pipeline stage is recorded as it really runs (status, timings, explanation).
- **Honest failure handling**: nothing is approved on a failure; the failed stage and everything already extracted are kept; manual entry is offered when the AI is unavailable.
- **History and reporting**: searchable history, per-run execution history, CSV report, dashboard.
- **Safe by design**: no PDF is stored, secrets never displayed, optional access code, configurable limits.

## Architecture

```
 Streamlit UI (app.py -> invoiceflow/ui)        presentation only, no business logic
        |  reads                    |  starts a run on a worker thread (live events)
        v                           v
 services/reporting.py        services/pipeline.py  --- InvoicePipeline.process(pdf)
 (read models, CSV)            |
                               |  hash -> validate -> text -> OCR -> AI -> validate structure -> normalise
                               |  -> [ one locked transaction: duplicates -> PO match -> financial checks -> decision -> save ]
                               v
   extraction/ (PyMuPDF, Tesseract)   ai/ (Gemini provider)   services/ (normaliser, po_matcher, duplicate_detector,
                                                               validation_engine, decision_service, maintenance)
                               v
   database/ (SQLAlchemy models, versioned migrations, repositories)  ->  SQLite (local)  |  PostgreSQL / Supabase (hosted)
```

| Package | Role |
|---|---|
| `invoiceflow/ui` | Pages, theme, live view, result view. Calls services; contains no rules. |
| `invoiceflow/services/pipeline.py` | The orchestrator: stages, events, failure handling, one locked transaction for the decision stages. |
| `invoiceflow/services/validation_engine.py` etc. | The pure decision rules (no database, UI or LLM). |
| `invoiceflow/ai` | Gemini client (retry, rate limits, schema validation) behind an `AiProvider` interface. |
| `invoiceflow/extraction` | PDF text and OCR. |
| `invoiceflow/database` | Models, migrations (additive, versioned), repositories, seed loader, exact-money column type. |
| `scripts/` | `seed_database.py`, `generate_demo_data.py` (synthetic POs and invoice PDFs). |
| `tests/` | 496 offline tests; AI test doubles live only here. |

**Decision policy** (details in `services/validation_engine.py`): precedence `PROCESSING_FAILED` > `DUPLICATE_BLOCKED` > `REVIEW_REQUIRED` > `APPROVED`; every check runs so all reasons are kept.
`remaining = PO total - approved so far`; `allowed overage = PO total x tolerance% / 100` (rounded down to the cent); `maximum = remaining + allowed overage`.
Only `APPROVED` invoices count toward a PO, so several partial invoices can share one PO. The amount compared is the invoice total **excluding tax** by default (`PO_AMOUNT_BASIS`).
Without a PO number the engine only *suggests* candidate POs; it never applies one.

## Technology stack

Python 3.12 · Streamlit · `google-genai` (Gemini Developer API) · Pydantic v2 · PyMuPDF · pytesseract + Tesseract OCR · Pillow · SQLAlchemy 2 ·
SQLite (local) / PostgreSQL via psycopg 3 (Supabase) · ReportLab (synthetic PDFs) · pytest · Ruff. Free tiers and open source only; no paid APIs.

## Installation and local execution (Windows, PowerShell)

### 1. Python 3.12

```powershell
winget install Python.Python.3.12        # or download it from https://www.python.org/downloads/
py -3.12 --version                       # must print Python 3.12.x
```

### 2. Get the code and create a virtual environment

```powershell
git clone https://github.com/<you>/invoiceflow.git
cd invoiceflow
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1               # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python --version                         # 3.12.x, from .venv
```

(With [uv](https://docs.astral.sh/uv/): `uv venv --python 3.12 .venv`, activate, then `uv pip install -r requirements-dev.txt`.)

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements-dev.txt      # runtime packages + pytest + ruff (use requirements.txt for runtime only)
```

### 4. Install Tesseract OCR (needed only for scanned PDFs)

```powershell
winget install UB-Mannheim.TesseractOCR  # or the installer from https://github.com/UB-Mannheim/tesseract/wiki
tesseract --version                      # open a NEW terminal first if the command is not found
```

The app finds `C:\Program Files\Tesseract-OCR\tesseract.exe` or a `tesseract` on `PATH` automatically. Otherwise set `TESSERACT_CMD` to the full path.
Without Tesseract, digital PDFs still work and scanned PDFs stop with an actionable message.

### 5. Configure

```powershell
Copy-Item .env.example .env              # then edit .env (it is git-ignored and must never be committed)
```

Minimum for AI extraction: `GEMINI_API_KEY` (see [below](#gemini-api-configuration)). Everything else has a default; the app starts with no configuration at all.

### 6. Initialise the database and sample data

```powershell
python scripts/seed_database.py          # creates/migrates the schema (SQLite file in data/) and loads the 12 demo POs; safe to re-run
python scripts/generate_demo_data.py     # optional: regenerates the synthetic PDFs in sample_data/ (they are already committed)
```

The app also creates and migrates the database on start-up, and the Purchase Orders page has a "Load synthetic demo purchase orders" button.

### 7. Run

```powershell
streamlit run app.py
```

Open the URL it prints (normally http://localhost:8501). Try the PDFs in `sample_data/invoices/` (a clean digital one, a multi-page one, a scanned one, one with no PO, one against a closed PO...).

## Configuration reference

Lookup order for every setting: environment variable, then Streamlit secrets, then `.env`, then the default. Secrets are never displayed or logged.

| Setting | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | unset | Gemini key. Optional at startup; without it AI extraction is reported as unavailable. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model used for extraction. |
| `GEMINI_TIMEOUT_SECONDS` | `60` | Per-request timeout. |
| `DATABASE_URL` | local SQLite `data/invoiceflow.db` | `postgresql://` / `postgres://` / `postgresql+psycopg://` URLs are converted to the psycopg driver. |
| `ACCESS_CODE` | unset | If set, visitors must enter it before using the app (recommended for any public deployment). |
| `MAX_UPLOAD_MB` / `MAX_PDF_PAGES` | `10` / `10` | App limits. Streamlit also enforces `server.maxUploadSize` (10 MB) in `.streamlit/config.toml`; raise both together. |
| `AMOUNT_TOLERANCE` | `0.01` | Absolute rounding tolerance for invoice arithmetic. |
| `PO_AMOUNT_BASIS` | `pre_tax` | `pre_tax` (total minus tax) or `grand_total`: which amount is compared with the PO. |
| `TESSERACT_CMD` | auto-detect | Path to `tesseract.exe` if not found automatically. |

## Gemini API configuration

1. In [Google AI Studio](https://aistudio.google.com/) choose *Get API key* and create a key. **Do not enable billing** for this project; the free tier is enough.
2. Locally, put it in `.env`:
   ```
   GEMINI_API_KEY=your-key-here
   GEMINI_MODEL=gemini-3.8-flash
   ```
   In a deployment, put the same names in the platform's secrets (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)); the repository never contains a key.
3. On the *About / System Health* page, *Test Gemini connection* makes one small real request to verify the key and the model.

**Free tier limits.** The free tier is rate-limited (requests per minute, per day and tokens per minute, which differ by model and can change). Check the limits that apply to your project in
[Google's rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits) and in AI Studio. InvoiceFlow makes at most 3 calls per document, retries only transient errors,
honours a short server-suggested wait, and fails fast with a clear message on a long one. If the model is overloaded or the quota is spent, the run is recorded as failed and manual entry is offered. Some models have also been
intermittently overloaded (HTTP 503); try another model via `GEMINI_MODEL`.

**Data privacy: use synthetic invoices only.** On the free (unpaid) tier, Google's terms allow the content you submit and the model's responses to be used to improve Google's products and machine-learning technologies, and human reviewers may read it
([Gemini API additional terms](https://ai.google.dev/gemini-api/terms_preview), [data logging and sharing](https://ai.google.dev/gemini-api/docs/logs-policy)). InvoiceFlow sends the extracted text of each uploaded invoice to Gemini, so never upload real or confidential documents to this demo.

## Database configuration

- **Local default: SQLite.** With `DATABASE_URL` unset, data lives in `data/invoiceflow.db` (git-ignored) and survives restarts on your machine.
- **Hosted: PostgreSQL.** A hosted platform's disk is temporary, so **SQLite there is not durable** (the app warns when it detects this). Use Supabase's free PostgreSQL:
  1. Create a free project at [supabase.com](https://supabase.com/) and save the database password.
  2. Open *Project -> Connect* and copy the **Transaction pooler** connection string (port `6543`). Prefer the pooler: the direct connection is IPv6-only on the free tier and is unreachable from many hosts.
  3. Use it as `DATABASE_URL`. SQLAlchemy needs the psycopg driver, so `postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres`
     (a plain `postgresql://` URL is converted automatically). URL-encode special characters in the password.
  4. The schema is created and migrated automatically on first start. The engine disables psycopg server-side prepared statements, which Supabase's transaction pooler does not support.
- Uploaded PDFs are **never stored**: only hashes, extracted fields, decisions and processing events.
- `python scripts/seed_database.py` initialises whichever database `DATABASE_URL` points to.

## Testing and quality

```powershell
python -m pytest -q                      # 496 offline tests; no real Gemini call is ever made
python -m pytest -q -m live              # opt-in: one real Gemini call (needs GEMINI_API_KEY)
ruff check . ; ruff format --check .     # lint and formatting
```

- The AI is a test double in every automated test (`tests/fakes.py`, labelled `TEST-DOUBLE`, refused outside pytest). Test documents are synthetic.
- OCR tests run the real Tesseract path when installed and are **skipped with a reason** when it is not.
- `tests/test_scenarios.py` runs the demonstration scenarios and technical failure cases end to end, with fault injection at every pipeline stage, and re-checks history-wide invariants after each one.
- `tests/test_deployment.py` checks configuration sources, the access gate, repository hygiene (no secrets, no oversized files) and the Streamlit Cloud files.

## Deployment

Free path: **GitHub** (code) + **Streamlit Community Cloud** (app) + **Supabase** (PostgreSQL) + **Gemini free tier**.
Full step-by-step instructions, secrets format, verification checklist and troubleshooting are in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.
In short: push to GitHub, create the Supabase project, create the Streamlit app with `app.py` as the entry point and Python 3.12, paste the secrets, deploy, then run the test checklist.
`packages.txt` installs Tesseract on Streamlit Cloud; `requirements.txt` pins every dependency.

## Security

The security review, with the evidence for each finding, is in **[docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md)**. Highlights: no secrets in the repository or its history,
uploads processed in memory only with size/page/type limits, generic error messages for visitors, no approval or payment actions, and an optional access code.

## Known limitations

- **Demonstration only.** No authentication or roles, so no human approve/reject workflow exists; "approved" means "cleared the automated checks", not "paid".
- **AI accuracy is not guaranteed.** Extraction was validated on synthetic invoices; real invoices vary. Anything uncertain is routed to review, but the system cannot know what it misreads confidently.
- **OCR** quality depends on the scan and the Tesseract version (English only). Poor scans are sent to review or fail explicitly.
- **Shared data in a public demo.** All visitors see the same history; with the access code unset anyone with the URL can use your Gemini quota.
- **Multiple currencies are never converted**; totals are shown per currency. PO amounts are compared net of tax by default; change `PO_AMOUNT_BASIS` if your POs include tax.
- **Free-tier services** (Gemini rate limits, Supabase project pausing, Streamlit app sleeping) can cause delays or temporary unavailability.
- Concurrency is verified on SQLite and manually on Supabase; there is no automated PostgreSQL CI.
- The UI was checked manually in one browser at desktop and phone widths; there is no automated visual testing.
