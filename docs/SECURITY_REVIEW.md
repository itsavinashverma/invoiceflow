# Security review

**Scope:** the InvoiceFlow repository and a public Streamlit Community Cloud + Supabase + Gemini-free-tier deployment.
**Classification:** a **demonstration system with synthetic data**, *not* a production payment-approval system. It is not designed or reviewed for real financial data.

Each finding below was checked by running something, not by reading code alone. "Evidence" says what was run.

## Summary

| # | Area | Result |
|---|---|---|
| 1 | Exposed secrets | **Pass.** No key, password or database host in any file or in git history. |
| 2 | Unsafe file handling | **Pass.** In-memory processing, signature/size/page/encryption checks, nothing written to disk, no PDF stored. |
| 3 | Unrestricted file size | **Pass.** Enforced twice: by Streamlit's server (10 MB) and by the app. |
| 4 | Unsafe debug output | **Fixed.** Visitors now see only generic error text; one place that echoed raw exception text was corrected. |
| 5 | Incorrect approval behaviour | **Pass.** Only the deterministic engine can approve; no UI action changes an approval; history invariants tested. |
| 6 | Publicly exposed sensitive information | **Mitigated.** Optional access code added; residual risk documented (shared demo data). |
| 7 | Missing environment variables | **Pass.** The app starts with no configuration and degrades visibly. |
| 8 | Unexpected external services | **Pass.** Only Gemini and the configured database; telemetry off. |
| 9 | Dependencies | **Pass.** `pip-audit`: no known vulnerabilities in all 66 resolved packages. |
| 10 | Demonstration labelling | **Pass.** Stated in the README, every page, the sidebar and the deployment guide. |

## Details

### 1. Exposed secrets
- Took the real `GEMINI_API_KEY`, database password, host and user from the local `.env` and searched the working tree and **every commit** for them: 0 matches. `.env` was never committed and is git-ignored (`.env`, `.env.*`, `.streamlit/secrets.toml`, `*.db`, `data/`).
- Pattern scan (Gemini-style keys, database URLs with credentials, private keys) over every committable file: only deliberately fake values in `tests/` and placeholders in the templates.
- Enforced permanently by `tests/test_deployment.py` (secret scan, `.gitignore` rules, templates contain placeholders only, valid TOML, no byte-order marks).
- Secrets are redacted before logging or display (`safe_summary`, `redact`, `safe_error_reason`); the Health page and error messages never show keys or passwords (browser-verified: neither the API key nor the access code appears in the rendered page).
- **Found and fixed:** several config files had a UTF-8 byte-order mark left by editing, which breaks strict TOML parsing and can confuse line-oriented tools. Removed; a test now prevents recurrence.

### 2. Unsafe file handling
- Uploads are read into memory and processed from bytes. App code writes no files (searched for `open(`, temp files, `write_bytes`): the only disk writes are the SQLite database and logs.
- Validation does not trust the file name or MIME type: `.pdf` extension on the base name, `%PDF-` signature in the first 1 KiB, size, page limit, password-protected and corrupt files rejected, active content (JavaScript, launch actions, embedded files) flagged and never executed (PyMuPDF does not run PDF scripts or fetch links).
- The original file name is reduced to a base name, stripped of control characters and truncated before it is stored.
- Spreadsheet-formula injection: the CSV report prefixes any untrusted cell that starts with `= + - @` (tested).
- HTML injection: every document-derived string placed into HTML is escaped (tested with `<script>` content); document text otherwise goes through Streamlit widgets.
- Prompt injection: document text is delimited with a random nonce, the model is told it is untrusted data, and the model can only produce a structured extraction, never a decision.
- Uploaded PDFs are **not stored**; only hashes, extracted fields, decisions and events (a test searches the database file for PDF bytes).

### 3. File size
- `.streamlit/config.toml` sets `server.maxUploadSize = 10` (enforced by Streamlit before the app sees the data) and the app enforces `MAX_UPLOAD_MB` and `MAX_PDF_PAGES` again.
- **Browser-verified:** a 12 MB file is refused with "File must be 10.0MB or smaller" and cannot be processed. The Health page warns if `MAX_UPLOAD_MB` is set above the server cap.
- Rendering is bounded too: OCR images are capped at 5000 px on the longest side, and OCR has a per-page timeout.

### 4. Debug output
- No `print`, `traceback` or `st.exception` in application code (searched). `client.showErrorDetails = "none"` makes Streamlit show visitors only generic error messages; details go to the server log.
- **Fixed:** the Process page used to show the raw text of an unexpected crash. It now shows a generic message and keeps details in the log (tested with an exception containing a fake password and file path).
- Logs contain stage names, run ids and error codes, not API keys or database credentials. On a hosted platform the logs are visible only to the app owner.

### 5. Approval behaviour
- The LLM only extracts. The decision is made by pure Python rules, with precedence `PROCESSING_FAILED > DUPLICATE_BLOCKED > REVIEW_REQUIRED > APPROVED`, and a last-line guard that refuses to return `APPROVED` unless an open PO, an invoice number and a total exist and every check passed.
- No UI element can approve, reject or pay (tested: no such button on any page). The one repository function that could change a status, `update_invoice_decision`, is called by nothing and is documented as unsafe to expose without authentication.
- **Executed evidence:** scenario tests plus fault injection at every pipeline stage, with history-wide invariants re-checked after each (no `APPROVED` without a fully successful run; no PO overspend; no invoice approved twice; no run left hanging). Mutation testing (deliberately breaking five rules) was caught every time.
- Concurrency: a database lock serialises decisions. SQLite and PostgreSQL integration tests cover two simultaneous uploads; the PostgreSQL case ran against a temporary schema in the configured database and confirmed one approval plus one duplicate block. GitHub Actions now has a disposable PostgreSQL 16 service for this test; that hosted workflow has not yet run.

### 6. Publicly exposed sensitive information
- A public URL means anyone can open the app, see **all** stored runs (shared demo data) and make Gemini calls with **your** quota.
- **Mitigation added:** an optional `ACCESS_CODE` gate (secret, constant-time comparison, per-session and global lockout). **Browser-verified:** with the gate on, no navigation or page content is reachable, even by direct URL (`/process`), and the code never appears in the page.
- This is a shared passcode, not authentication. For a private demo prefer Streamlit's per-viewer sharing; for anything real, add proper authentication first.
- **Residual risk:** with no access code, the demo is open to anyone with the link. Keep the data synthetic.

### 7. Missing environment variables
- Starting from a fresh checkout with **no** configuration (no `.env`, no key, a database path whose folders do not exist) renders the app, creates the schema, and reports "GEMINI_API_KEY is not set" honestly (tested, and verified by copying only the committable files to a new folder, running all 496 tests there and starting the server).
- Invalid values produce a clear message naming the setting. On a hosted platform with SQLite the app shows a warning that data will be lost on restart.

### 8. External services
- Runtime network use is limited to: **Google Gemini** (document text, via the official SDK), the configured **database** (SQLite file or Supabase PostgreSQL), and, at build time on Streamlit Cloud, PyPI and the OS package repository. Searched the code for other network calls: none. Streamlit usage telemetry is disabled.
- On the free Gemini tier Google may use submitted content to improve its products and humans may review it. Hence: **synthetic data only**.

### 9. Dependencies
- All dependencies are pinned. `pip-audit` on the full resolved Linux set (66 packages): **no known vulnerabilities**. Dev tools (pytest, ruff) are not in `requirements.txt`, so they are not deployed.
- Ruff with security rules (`S`) is part of the lint run: clean.

### 10. Demonstration labelling
- README (top banner), every page (banner), the sidebar ("Not a production payment-approval system: nothing is ever paid or posted"), the Health page, and the deployment guide all say so.

## Remaining risks and recommendations

1. Set `ACCESS_CODE` (or private sharing) on any public deployment; rotate the Gemini key if it was ever pasted anywhere else.
2. Use synthetic documents only; the free Gemini tier is not suitable for confidential data.
3. Before any real use: add authentication and roles, an audit trail of human decisions, per-user data separation, a paid Gemini tier or other provider with suitable data terms, backups, and a full security assessment.
4. Free-tier services can pause or throttle; a failed run is reported and recorded, never approved.
