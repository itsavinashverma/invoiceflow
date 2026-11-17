"""Process Invoice page: upload, pre-check, preview, run the pipeline with a live view, show the result.

Presentation only. Processing happens in ``services.pipeline`` on a worker thread (``ui.jobs``); the live view
displays the events the pipeline really emits.

Accidental repeats are harmless by design:
* the button is disabled while a run is starting/running and once this upload has been processed;
* the run lives in ``st.session_state`` as a job, so any re-run of the page (a second click, navigation,
  a widget change) re-attaches to the SAME job instead of starting another.
"""

from __future__ import annotations

import hashlib

import streamlit as st

from invoiceflow.database.connection import DatabaseUnavailableError, session_scope
from invoiceflow.exceptions import PdfValidationError
from invoiceflow.services import reporting
from invoiceflow.ui import context, theme
from invoiceflow.ui.jobs import PipelineJob
from invoiceflow.ui.manual_entry import manual_entry_form
from invoiceflow.ui.preview import render_page_png
from invoiceflow.ui.result_view import render_run_view
from invoiceflow.utils.pdf_security import PdfInfo, validate_pdf_upload

_KEY = "process_state"


def _state() -> dict:
    return st.session_state.setdefault(
        _KEY,
        {
            "n": 0,  # uploader generation: bumping it clears the file chooser
            "job": None,
            "start": None,
            "run_id": None,
            "done_key": None,
            "data": None,
            "filename": None,
            "ctype": None,
            "crash": None,
        },
    )


def _reset() -> None:
    state = _state()
    state.update(n=state["n"] + 1, job=None, start=None, run_id=None, done_key=None, data=None, filename=None, ctype=None, crash=None)


def render() -> None:
    settings = context.get_settings()
    theme.page_header("Process Invoice", "Upload an invoice PDF and watch it move through extraction, checks and the decision")
    theme.demo_banner()
    if not context.require_database():
        return
    state = _state()
    job: PipelineJob | None = state["job"]
    busy = job is not None or state["start"] is not None

    upload = st.file_uploader(
        "Invoice PDF",
        type=["pdf"],
        key=f"uploader_{state['n']}",
        disabled=busy,
        help=f"PDF only · up to {settings.max_upload_mb} MB · up to {settings.max_pdf_pages} pages. Synthetic invoices only, please.",
    )

    data = upload.getvalue() if upload is not None else None
    key = (upload.name, len(data), hashlib.sha256(data).hexdigest()) if upload is not None and data is not None else None
    info: PdfInfo | None = None
    if upload is not None and data is not None:
        try:
            info = validate_pdf_upload(
                data, upload.name, max_bytes=settings.max_upload_bytes, max_pages=settings.max_pdf_pages, content_type=upload.type
            )
        except PdfValidationError as exc:
            st.error(f"**This file cannot be processed.** {exc}")
        else:
            _upload_panel(upload.name, data, info, collapsed=busy or (state["done_key"] == key))

    already = key is not None and state["done_key"] == key
    disabled = info is None or busy or already
    label = "Processing…" if busy else ("Processed ✓" if already else "Process invoice")
    clicked = st.button(
        label,
        type="primary",
        disabled=disabled,
        key="process_btn",
        help="Runs every stage for real and records the result." if not disabled else None,
    )
    if already and not busy:
        st.caption("This file has been processed. Use **Process another invoice** to start over.")
    if clicked and info is not None and data is not None:
        state.update(start={"manual": None}, data=data, filename=upload.name, ctype=upload.type, done_key=None, run_id=None, crash=None)
        st.rerun()  # re-render with the button disabled before the run begins

    # ---- start / follow the job
    if state["job"] is not None:
        state["start"] = None  # a run is already active: a repeated request must never start a second one
    if state["start"] is not None and state["job"] is None:
        start = state["start"]
        state["job"] = PipelineJob(
            context.make_pipeline(settings),
            state["data"],
            state["filename"],
            state["ctype"],
            key=(state["filename"], len(state["data"]), hashlib.sha256(state["data"]).hexdigest()),
            manual=start["manual"],
        )
        state["start"] = None
    job = state["job"]
    if job is not None:
        _follow(job)
        state["job"] = None
        if job.exception is not None:
            # Only database-availability messages are known to be safe to show; anything else stays in the log.
            detail = (
                str(job.exception)
                if isinstance(job.exception, DatabaseUnavailableError)
                else "an unexpected error occurred (details are in the application log)"
            )
            state["crash"] = f"The run could not be completed or recorded: {detail}"
        else:
            state["run_id"], state["done_key"] = job.result.run_id, job.key  # type: ignore[union-attr]
        st.rerun()

    if state["crash"]:
        st.error(state["crash"])
    if state["run_id"] and (key is None or key == state["done_key"]):  # hide a stale result once a different file is chosen
        _result(state, settings)


def _upload_panel(name: str, data: bytes, info: PdfInfo, *, collapsed: bool) -> None:
    """File details beside a document preview. The preview folds away once a run starts, so the live view stays on screen."""
    left, right = st.columns([2, 3], gap="large")
    with left, st.expander("Document preview", expanded=not collapsed):
        page = 0
        if info.page_count > 1:
            page = st.selectbox(
                "Preview page", range(info.page_count), format_func=lambda i: f"Page {i + 1} of {info.page_count}", key="preview_page"
            )
        png = render_page_png(data, page, width_px=640)
        if png:
            st.image(png, caption=f"Preview: {name}", width="stretch")
        else:
            st.caption("A preview is not available for this document; it can still be processed.")
    with right:
        theme.kv_grid(
            [("File", name), ("Size", theme.fmt_bytes(info.size_bytes)), ("Pages", info.page_count), ("SHA-256", info.sha256[:16] + "…")]
        )
        for w in info.warnings:
            theme.callout("warning", w)
        st.caption(
            "The file passed the pre-checks (PDF signature, size, page count, not encrypted, not corrupt). "
            "Full validation runs again as the first pipeline stage."
        )


def _follow(job: PipelineJob) -> None:
    """Show the pipeline's real events as they arrive. Returns when the run has finished."""
    total = len(job.snapshot())
    with st.status("Processing invoice…", expanded=True) as status:
        bar = st.progress(0.0, text="Starting…")
        box = st.empty()
        while True:
            events = job.snapshot()
            finished = sum(e.status in ("COMPLETED", "FAILED", "SKIPPED") for e in events)
            running = next((e.title for e in events if e.status == "RUNNING"), None)
            status.update(label=f"Running: {running}" if running else "Processing invoice…", state="running", expanded=True)
            bar.progress(finished / total, text=f"{finished} of {total} stages finished")
            box.markdown(theme.stage_list_html(events), unsafe_allow_html=True)
            if job.done.wait(0.15):
                break
        events = job.snapshot()
        box.markdown(theme.stage_list_html(events), unsafe_allow_html=True)
        failed = job.exception is not None or any(e.status == "FAILED" for e in events)
        bar.progress(1.0, text="Finished")
        status.update(
            label="Processing failed" if failed else "Processing complete", state="error" if failed else "complete", expanded=failed
        )


def _result(state: dict, settings) -> None:
    with session_scope() as s:
        view = reporting.get_run_view(s, state["run_id"])
    if view is None:
        st.error("The stored result could not be found.")
        return
    st.divider()
    st.subheader("Result")
    render_run_view(view, key="proc")

    c1, c2, _ = st.columns([1.3, 1.3, 3])
    if c1.button("View in Processing History", key="goto_history"):
        st.session_state["history_open_run"] = view.run.run_id
        context.go("history")
    if c2.button("Process another invoice", key="another"):
        _reset()
        st.rerun()

    if view.error and view.error.manual_entry_possible and state["data"]:
        st.divider()
        st.markdown("#### Enter the invoice fields manually")
        st.caption(
            "The AI could not read this invoice. Fields you type go through the same normalisation, checks and decision rules, and "
            "are cross-checked against the document text. This creates a new run; the failed attempt stays in the history."
        )
        entered = manual_entry_form(None, key="manual")
        if entered is not None:
            state["start"] = {"manual": entered}
            st.rerun()
