# PROJECT_SPEC.md — InvoiceFlow

## 1. Problem
AP teams manually read vendor PDF invoices, extract vendor/invoice no./date/PO ref/amounts/taxes/line items, find the PO, compare, check duplicates, spot discrepancies, and decide approve vs. human review. InvoiceFlow automates this with explainable results.

## 2. Functional requirements
| # | Capability | Notes |
|---|-----------|-------|
| F1 | Upload invoice PDF | Streamlit uploader; size/type validation |
| F2 | Digital extraction | PyMuPDF text per page |
| F3 | OCR fallback | If text is below a threshold, render pages (Pillow) → Tesseract. Record which method was used and OCR confidence |
| F4 | LLM structuring | Gemini via `google-genai`, JSON-schema constrained output, validated with Pydantic v2 |
| F5 | PO matching | By PO reference first; fallback to vendor + amount proximity; flag ambiguous/none |
| F6 | Deterministic validation | Decimal: line totals, subtotal, tax, grand total, currency, price/qty vs PO, over-PO-balance |
| F7 | Duplicate detection | Exact (vendor + invoice no.), file hash, fuzzy (vendor + amount + date window) |
| F8 | Decision engine | Rule-based: `AUTO_APPROVE_RECOMMENDED` / `HUMAN_REVIEW` / `REJECT_RECOMMENDED`, with reason codes. Nothing is actually paid |
| F9 | Explainability | Per-check pass/fail with expected vs. actual values and rule ID |
| F10 | Live execution view | Stage-by-stage status with timings, updated as stages run |
| F11 | Persistence | Every run (inputs, extraction, checks, decision, timings, errors) stored |
| F12 | Dashboard | Totals, decision mix, trends, filterable run table |
| F13 | Run details | Drill into any historical run |
| F14 | Synthetic data | Vendors, POs, invoice PDFs incl. one scanned (image-only) PDF |
| F15 | Four edge cases | See §4 |
| F16 | Tests, docs, GitHub-ready repo, Streamlit deployment config | |

## 3. Non-functional requirements
- Decimal for money; Pydantic validation at every boundary; type hints; explicit exceptions; structured logging.
- Secrets only via env / Streamlit secrets; `.env` git-ignored; `.env.example` provided.
- Same SQLAlchemy code for SQLite (default) and Postgres (`DATABASE_URL`).
- Gemini unavailable/misconfigured → run is recorded as `FAILED_EXTRACTION` and routed to human review. No fabricated data.
- Free tier only. Tesseract absent → OCR stage reports "unavailable"; digital PDFs still work.

## 4. Edge cases (synthetic demo)
1. **Amount mismatch**: invoice total/line price differs from PO beyond tolerance → human review.
2. **Duplicate invoice**: same vendor + invoice number (or re-uploaded identical file) → reject/review.
3. **Scanned (image-only) PDF**: triggers OCR; low confidence → human review, otherwise proceeds.
4. **Missing/unknown PO reference** (also covers over-billing vs. PO balance): no PO match → human review.
(Plus a clean happy-path invoice for contrast.)

## 5. Decision policy (initial)
- Auto-approve recommendation requires ALL: extraction confident, PO matched uniquely, all math checks pass within tolerance (default 0.01 absolute), not duplicate, within PO remaining balance, currency matches, vendor matches.
- Any failed or indeterminate check → HUMAN_REVIEW. Confirmed exact duplicate → REJECT_RECOMMENDED (still human-confirmed).
- Tolerances are configurable constants, documented.

## 6. Assumptions
- Single currency per invoice; multi-currency PO mismatch → review.
- POs live in the DB (seeded from synthetic data / CSV); no ERP integration.
- English-language invoices.
- Gemini free-tier rate limits apply; bounded retries.
- **Model name**: the brief specifies `gemini-3.8-flash`. This cannot be verified without an API key; at implementation we will call the models-list endpoint, confirm availability and free-tier access, and fall back to a verified GA Flash model if it is not available. Always overridable via `GEMINI_MODEL`.
- Deployment target: Streamlit Community Cloud + Supabase Postgres (created only with user approval).

## 7. Environment findings (2026-11-17)
| Item | Status | Action |
|------|--------|--------|
| Working dir | `D:\Documents\invoiceflow` empty | OK |
| Git | v2.55 installed; directory **not a repo**; global identity is `itsavinashverma` / a different email than the account email | Ask before `git init`; confirm commit identity |
| Python | Only **3.14.4** installed (spec wants 3.12) | `uv` is present → `uv venv --python 3.12` (downloads 3.12; ask first) |
| Tesseract | **Not installed**, not on PATH | `winget install UB-Mannheim.TesseractOCR` (5.4) after approval; set `TESSERACT_CMD` |
| API keys | No `GEMINI_API_KEY` / DB env vars set | User must create free key at Google AI Studio; put in `.env` |
| `gh` CLI | Not installed | Needed only if we create/push a GitHub repo; ask first |
| Dependency wheels | Dry-run resolve on 3.14 succeeded; target 3.12 for deployment parity (Streamlit Cloud) | Use 3.12 venv |
| Disk | ~159 GB free | OK |

## 8. Out of scope
Payments, ERP integration, authentication/multi-tenancy, paid services.
