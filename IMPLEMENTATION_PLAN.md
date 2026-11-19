# InvoiceFlow implementation plan

This plan reflects the existing implementation as audited on 2026-11-19. See [CODEX_HANDOVER.md](CODEX_HANDOVER.md) for architecture and evidence. No application code was changed in the initial audit.

## P0 — Complete: restore a reliable offline test run

- Fixed the UTC/local date mismatch in `tests/test_reporting.py::test_search_filters` by deriving bounds from fixture run timestamps.
- Confirmed that the Processing History UI labels its date filters as UTC.
- Verification: reporting tests **9 passed**; full offline suite **496 passed, 1 deselected**.

## P1 — Verify external integrations before presenting a live demo

- Gemini extraction was attempted with the synthetic standard invoice. The configured provider returned HTTP 504 after three attempts; rerun when the service is available.
- Deployment guide uses the current `main` branch. No hosted app URL is present in the repository, so deployed persistence still needs a target URL.
- Verify persistence and the sample edge cases on the hosted app once its URL is available.

## P2 — Improve integration confidence

- Added `tests/test_postgres_integration.py` for migration/schema, money/UTC round trips, and concurrent duplicate processing. It passed against isolated schemas in the configured PostgreSQL database.
- Added `.github/workflows/tests.yml` with a disposable PostgreSQL 16 service. The hosted GitHub workflow has not yet run.
- Repeat a visual browser check at desktop and phone widths before a release; no browser surface was available during this audit.
- Keep `PROJECT_SPEC.md`, `README.md`, and this plan aligned with the implemented behavior and known limitations.

## P3 — Optional enhancements

- Consider extra monitoring or operational reporting only if the live demonstration needs it. Do not add fuzzy duplicate matching or external accounting actions without a demonstrated requirement; the current conservative deterministic workflow is the intended scope.

## Completed implementation

The existing repository already includes PDF validation and extraction, real Tesseract OCR, Gemini structured extraction, Pydantic validation, normalization, duplicate detection, PO matching, Decimal-based financial checks, deterministic decisions, persistence, stage events, Streamlit dashboard/history/process pages, synthetic edge cases, and deployment configuration. These features and their test evidence are summarized in [CODEX_HANDOVER.md](CODEX_HANDOVER.md).
