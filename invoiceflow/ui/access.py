"""Optional access gate for a publicly reachable deployment.

When ``ACCESS_CODE`` is set (as a secret), visitors must enter it before any page renders. This protects a
free-tier Gemini quota and the demo data from casual public use. It is a *shared passcode*, not user
authentication: there are no accounts, so it does not make the app fit for real financial data or real
approval/payment actions. For private demos, Streamlit Community Cloud's own viewer allow-list is stronger.
"""

from __future__ import annotations

import hmac
import time

import streamlit as st

from invoiceflow.config import Settings

MAX_ATTEMPTS_PER_SESSION = 5
LOCKOUT_FAILURES = 10  # wrong codes across ALL visitors within the window...
LOCKOUT_WINDOW_SECONDS = 600  # ...lock the gate for the rest of the window
_failures: list[float] = []  # process-wide timestamps of wrong attempts (reset on restart)


def reset_lockout() -> None:
    """Clear the process-wide failure log (used by tests)."""
    _failures.clear()


def _recent_failures(now: float) -> int:
    _failures[:] = [t for t in _failures if now - t < LOCKOUT_WINDOW_SECONDS]
    return len(_failures)


def require_access(settings: Settings) -> None:
    """Return normally if access is granted (or no gate is configured); otherwise show the form and stop the page."""
    if not settings.access_code:
        return
    state = st.session_state
    if state.get("_access_granted"):
        return

    st.title("InvoiceFlow")
    st.caption("Demonstration product · synthetic data only")
    now = time.monotonic()
    if _recent_failures(now) >= LOCKOUT_FAILURES:
        st.error("Too many incorrect access codes were entered recently. Please try again in a few minutes.")
        st.stop()
    attempts = state.get("_access_attempts", 0)
    if attempts >= MAX_ATTEMPTS_PER_SESSION:
        st.error("Too many incorrect attempts in this session. Reload the page to try again.")
        st.stop()

    with st.form("access_gate"):
        code = st.text_input("Access code", type="password", help="Ask the person who shared this demo with you.")
        submitted = st.form_submit_button("Continue")
    if submitted:
        if hmac.compare_digest(code.encode("utf-8"), settings.access_code.encode("utf-8")):  # constant-time comparison
            state["_access_granted"] = True
            state["_access_attempts"] = 0
            st.rerun()
        state["_access_attempts"] = attempts + 1
        _failures.append(now)
        st.error("Incorrect access code.")
    st.stop()
