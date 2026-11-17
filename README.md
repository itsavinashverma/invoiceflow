# InvoiceFlow

AI-assisted invoice processing. The LLM only extracts and structures data; approve/review decisions are made by deterministic Python, and anything uncertain goes to a human.

**Status:** project scaffold only. The app starts and shows system readiness; invoice processing is not implemented yet.

## Setup (Windows, PowerShell)

```powershell
uv venv --python 3.12 .venv
.venv\Scripts\activate
uv pip install -r requirements-dev.txt
copy .env.example .env     # then edit; the app also starts with no API key
```

Tesseract OCR is needed for scanned PDFs (Windows: install it, or set `TESSERACT_CMD`). Without it the app says so instead of failing silently.

## Run

```powershell
streamlit run app.py
python -m pytest -q
python scripts/seed_database.py      # initialise the SQLite schema
```

## Configuration

Order of precedence: environment variable, Streamlit secrets, `.env`, default.

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | unset | optional at startup |
| `GEMINI_MODEL` | `gemini-3.8-flash` | |
| `DATABASE_URL` | local SQLite `data/invoiceflow.db` | PostgreSQL URLs (`postgres://`, `postgresql://`) are rewritten to the psycopg v3 driver |
| `MAX_UPLOAD_MB` | 10 | |
| `MAX_PDF_PAGES` | 10 | |
| `TESSERACT_CMD` | auto-detect | PATH, then default Windows install path |

`GEMINI_TIMEOUT_SECONDS` (default 60) sets the request timeout.

## AI extraction

`invoiceflow/ai/` uses the official `google-genai` SDK with Gemini structured output (JSON schema from the `InvoiceExtraction` Pydantic model). The LLM only extracts; money stays a string until `services/normalizer.py` turns it into `Decimal`. Every attempt (AI or manual) is stored as an audit record: model, timestamp, raw response, validated JSON, warnings, and evidence snippets checked against the document text.

- At most 3 API calls per document, only for transient errors (429 with a short suggested delay, 5xx, timeouts, malformed output). Auth, blocked and bad-request errors fail immediately.
- If Gemini is unavailable or unusable, the UI says so and offers manual entry, which goes through the same normalizer. No AI result is ever invented.
- Offline tests never call the API. The opt-in live test: `python -m pytest -m live`.

## Streamlit Community Cloud

Main file `app.py`; Python 3.12. `packages.txt` installs Tesseract. Paste secrets (see `.streamlit/secrets.toml.example`) into the app's Secrets panel and use a PostgreSQL `DATABASE_URL`, since the container filesystem is ephemeral.
