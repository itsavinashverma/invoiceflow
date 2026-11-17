"""Streamlit entry point: ``streamlit run app.py``."""
from __future__ import annotations

import streamlit as st

from invoiceflow import constants
from invoiceflow.config import ConfigError, load_settings
from invoiceflow.ui import dashboard
from invoiceflow.utils.logging_config import setup_logging

st.set_page_config(page_title=constants.APP_NAME, page_icon="🧾", layout="wide")
setup_logging()

try:
    settings = load_settings()
except ConfigError as exc:
    st.error(f"Invalid configuration: {exc}")
    st.stop()

dashboard.render(settings)
