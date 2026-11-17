"""Helpers for driving Streamlit pages with AppTest."""

import re

from streamlit.testing.v1 import AppTest


def page(module: str, timeout: int = 60) -> AppTest:
    """Run one page's ``render()`` in isolation (without the navigation shell)."""
    return AppTest.from_string(f"from invoiceflow.ui import {module}\n{module}.render()", default_timeout=timeout).run()


def html(at: AppTest) -> str:
    """All markdown/HTML emitted by the page, for content assertions."""
    return "\n".join(m.value for m in at.markdown)


def alltext(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [e.value for e in at.error] + [w.value for w in at.warning] + [i.value for i in at.info]
    parts += [s.value for s in at.success] + [c.value for c in at.caption] + [t.value for t in at.text]
    return "\n".join(map(str, parts))


def metric(at: AppTest, label: str) -> str:
    """The value shown on the dashboard card with this label."""
    m = re.search(rf'if-metric-label">{re.escape(label)}</div><div class="if-metric-value">([^<]*)</div>', html(at))
    assert m, f"metric card {label!r} not found"
    return m.group(1)


def last_df(at: AppTest):
    return at.dataframe[-1].value


def no_exceptions(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
