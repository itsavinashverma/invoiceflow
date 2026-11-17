"""Visual system: colour tokens, one small stylesheet, and HTML helpers.

Rules of the system
* Status colours are reserved (good / warning / serious / critical) and ALWAYS appear with an icon and a
  label, never as colour alone. They are taken from a validated palette.
* Neutral surfaces use translucent greys so the same CSS reads correctly on Streamlit's light and dark themes.
* Anything that comes from an uploaded document is HTML-escaped before it reaches ``unsafe_allow_html``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal

import streamlit as st

from invoiceflow.schemas import StageEvent
from invoiceflow.utils.money import format_money

PRIMARY = "#2F5BEA"
GOOD, WARNING, SERIOUS, CRITICAL, NEUTRAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#8a8f98"


@dataclass(frozen=True)
class StatusStyle:
    label: str
    icon: str
    color: str
    tone: str  # css modifier


# Decision statuses
DECISIONS: dict[str, StatusStyle] = {
    "APPROVED": StatusStyle("Approved", "✔", GOOD, "good"),
    "REVIEW_REQUIRED": StatusStyle("Review required", "⚑", WARNING, "warning"),
    "DUPLICATE_BLOCKED": StatusStyle("Duplicate blocked", "⛔", SERIOUS, "serious"),
    "PROCESSING_FAILED": StatusStyle("Processing failed", "✖", CRITICAL, "critical"),
    "REJECTED": StatusStyle("Rejected", "✖", CRITICAL, "critical"),
}
IN_PROGRESS = StatusStyle("In progress", "◔", NEUTRAL, "neutral")

# Pipeline stage statuses
STAGES: dict[str, StatusStyle] = {
    "PENDING": StatusStyle("Pending", "○", NEUTRAL, "neutral"),
    "RUNNING": StatusStyle("Running", "▶", PRIMARY, "running"),
    "COMPLETED": StatusStyle("Completed", "✔", GOOD, "good"),
    "FAILED": StatusStyle("Failed", "✖", CRITICAL, "critical"),
    "SKIPPED": StatusStyle("Skipped", "↷", NEUTRAL, "neutral"),
}

CSS = f"""
<style>
:root {{
  --if-primary: {PRIMARY}; --if-good: {GOOD}; --if-warning: {WARNING}; --if-serious: {SERIOUS};
  --if-critical: {CRITICAL}; --if-neutral: {NEUTRAL};
  --if-line: rgba(128,128,128,.28); --if-soft: rgba(128,128,128,.08); --if-muted: rgba(128,128,128,.95);
}}
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1240px; }}
h1 {{ font-size: 1.75rem !important; font-weight: 650 !important; letter-spacing: -.01em; margin-bottom: .15rem; }}
h2, h3 {{ font-weight: 620 !important; letter-spacing: -.005em; }}
.if-sub {{ color: var(--if-muted); margin: 0 0 1.1rem 0; font-size: .98rem; }}
.if-card {{ border: 1px solid var(--if-line); border-radius: 12px; padding: 16px 18px; background: var(--if-soft); }}
.if-h {{ font-size: .78rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--if-muted); margin: 1.1rem 0 .5rem; }}
h4 {{ margin-top: 1.3rem !important; }}

/* metric cards: a responsive grid that wraps on small screens */
.if-metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(146px, 1fr)); gap: 12px; margin: .4rem 0 1.2rem; }}
.if-metric {{ border: 1px solid var(--if-line); border-radius: 12px; padding: 14px 16px; background: var(--if-soft); border-top: 3px solid var(--if-neutral); }}
.if-metric.good {{ border-top-color: var(--if-good); }} .if-metric.warning {{ border-top-color: var(--if-warning); }}
.if-metric.serious {{ border-top-color: var(--if-serious); }} .if-metric.critical {{ border-top-color: var(--if-critical); }}
.if-metric.primary {{ border-top-color: var(--if-primary); }}
.if-metric-label {{ font-size: .8rem; color: var(--if-muted); font-weight: 560; }}
.if-metric-value {{ font-size: 1.75rem; font-weight: 680; line-height: 1.25; letter-spacing: -.01em; overflow-wrap: anywhere; }}
.if-metric-note {{ font-size: .76rem; color: var(--if-muted); }}

/* badges: icon + label, tinted ring */
.if-badge {{ display: inline-flex; align-items: center; gap: .4em; padding: 2px 10px; border-radius: 999px; font-size: .82rem;
  font-weight: 600; border: 1px solid var(--c); background: color-mix(in srgb, var(--c) 14%, transparent); white-space: nowrap; }}
.if-badge .ic {{ color: var(--c); }}

/* callouts */
.if-callout {{ border: 1px solid var(--if-line); border-left: 4px solid var(--c); border-radius: 10px; padding: 12px 16px; margin: .5rem 0;
  background: color-mix(in srgb, var(--c) 9%, transparent); }}
.if-callout b {{ font-weight: 650; }}
.if-callout.good {{ --c: var(--if-good); }} .if-callout.warning {{ --c: var(--if-warning); }}
.if-callout.serious {{ --c: var(--if-serious); }} .if-callout.critical {{ --c: var(--if-critical); }}
.if-callout.neutral, .if-callout.running {{ --c: var(--if-neutral); }} .if-callout.info {{ --c: var(--if-primary); }}

/* check rows */
.if-check {{ display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--if-line); }}
.if-check:last-child {{ border-bottom: 0; }}
.if-check .ic {{ flex: 0 0 1.3em; font-weight: 700; color: var(--c); }}
.if-check .t {{ font-weight: 600; }} .if-check .d {{ color: var(--if-muted); font-size: .92rem; }}
.if-check.good {{ --c: var(--if-good); }} .if-check.critical {{ --c: var(--if-critical); }} .if-check.neutral {{ --c: var(--if-neutral); }}
.if-check.warning {{ --c: var(--if-warning); }}

/* live stage list */
.if-stages {{ width: 100%; }}
.if-stage {{ display: grid; grid-template-columns: 1.6em minmax(150px, 15em) 5.5em 1fr; gap: 6px 10px; align-items: baseline; padding: 7px 4px;
  border-bottom: 1px solid var(--if-line); }}
.if-stage:last-child {{ border-bottom: 0; }}
.if-stage .ic {{ color: var(--c); font-weight: 700; text-align: center; }}
.if-stage .n {{ font-weight: 600; }} .if-stage .ms {{ color: var(--if-muted); font-variant-numeric: tabular-nums; text-align: right; }}
.if-stage .m {{ color: var(--if-muted); font-size: .92rem; overflow-wrap: anywhere; }}
.if-stage.running {{ background: color-mix(in srgb, var(--if-primary) 8%, transparent); --c: var(--if-primary); }}
.if-stage.good {{ --c: var(--if-good); }} .if-stage.critical {{ --c: var(--if-critical); }} .if-stage.neutral {{ --c: var(--if-neutral); }}
.if-stage.pending {{ opacity: .62; }}

/* key/value grids */
.if-kv {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px 20px; }}
.if-kv .k {{ font-size: .76rem; color: var(--if-muted); text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }}
.if-kv .v {{ font-weight: 560; overflow-wrap: anywhere; }}

/* status distribution bars */
.if-bar {{ display: grid; grid-template-columns: minmax(130px, 11em) 1fr 4.5em; gap: 4px 12px; align-items: center; padding: 6px 0; }}
.if-bar .lbl {{ font-weight: 560; }} .if-bar .lbl .ic {{ color: var(--c); margin-right: .4em; }}
.if-bar .track {{ height: 10px; border-radius: 5px; background: var(--if-soft); overflow: hidden; }}
.if-bar .fill {{ height: 100%; border-radius: 0 5px 5px 0; background: var(--c); }}
.if-bar .val {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}

.if-demo {{ border: 1px dashed var(--if-line); border-radius: 10px; padding: 8px 12px; font-size: .84rem; color: var(--if-muted); margin: 0 0 1rem; }}
.if-empty {{ text-align: center; padding: 34px 18px; border: 1px dashed var(--if-line); border-radius: 14px; }}
.if-empty .big {{ font-size: 1.15rem; font-weight: 620; margin-bottom: .3rem; }}

@media (max-width: 640px) {{
  .block-container {{ padding: 1rem .8rem 2rem; }}
  h1 {{ font-size: 1.4rem !important; }}
  .if-metric-value {{ font-size: 1.4rem; }}
  .if-stage {{ grid-template-columns: 1.6em 1fr 5em; }} .if-stage .m {{ grid-column: 2 / -1; }}
  .if-bar {{ grid-template-columns: 1fr 4em; }} .if-bar .track {{ grid-column: 1 / -1; order: 3; }}
}}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(value: object) -> str:
    """HTML-escape anything that may originate from an uploaded document."""
    return html.escape("" if value is None else str(value), quote=True)


# ----------------------------------------------------------------------------- small components


def decision_style(status: str | None) -> StatusStyle:
    return DECISIONS.get(status or "", IN_PROGRESS)


def badge(style: StatusStyle) -> str:
    return f'<span class="if-badge" style="--c:{style.color}"><span class="ic">{style.icon}</span>{esc(style.label)}</span>'


def decision_badge(status: str | None) -> str:
    return badge(decision_style(status))


def decision_text(status: str | None) -> str:
    """Plain-text label for tables: icon + label, never colour alone."""
    s = decision_style(status)
    return f"{s.icon} {s.label}"


def page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.markdown(f'<p class="if-sub">{esc(subtitle)}</p>', unsafe_allow_html=True)


def demo_banner() -> None:
    st.markdown(
        '<div class="if-demo"><b>Demonstration product · synthetic data only.</b> No real payments or accounting entries are ever made, '
        "and no one can approve or pay anything here. Please do not upload real invoices: document text is sent to Google Gemini for extraction.</div>",
        unsafe_allow_html=True,
    )


def metric_cards(cards: list[dict]) -> None:
    """``cards``: label, value, tone (good/warning/serious/critical/primary), note. Values are escaped."""
    parts = []
    for c in cards:
        note = f'<div class="if-metric-note">{esc(c["note"])}</div>' if c.get("note") else ""
        parts.append(
            f'<div class="if-metric {esc(c.get("tone", ""))}"><div class="if-metric-label">{esc(c["label"])}</div>'
            f'<div class="if-metric-value">{esc(c["value"])}</div>{note}</div>'
        )
    st.markdown(f'<div class="if-metrics">{"".join(parts)}</div>', unsafe_allow_html=True)


def callout(tone: str, title: str, body: str = "") -> None:
    inner = f"<b>{esc(title)}</b>" + (f"<div>{esc(body)}</div>" if body else "")
    st.markdown(f'<div class="if-callout {esc(tone)}">{inner}</div>', unsafe_allow_html=True)


def check_rows(items: list[tuple[str, str, str]], tone: str, icon: str) -> None:
    """``items``: (title, detail, extra). One consistent row style per outcome (pass / fail / not run / warning)."""
    if not items:
        return
    rows = "".join(
        f'<div class="if-check {tone}"><span class="ic">{icon}</span><div><div class="t">{esc(t)}</div><div class="d">{esc(d)}</div></div></div>'
        for t, d, _ in items
    )
    st.markdown(f'<div class="if-card">{rows}</div>', unsafe_allow_html=True)


def kv_grid(items: list[tuple[str, object]]) -> None:
    cells = "".join(
        f'<div><div class="k">{esc(k)}</div><div class="v">{esc(v) if v not in (None, "") else "—"}</div></div>' for k, v in items
    )
    st.markdown(f'<div class="if-card"><div class="if-kv">{cells}</div></div>', unsafe_allow_html=True)


def stage_list_html(events: list[StageEvent]) -> str:
    rows = []
    for e in events:
        s = STAGES.get(e.status, STAGES["PENDING"])
        if e.status == "RUNNING":
            ms, msg = "running…", e.message or "In progress"
        else:
            ms = f"{e.duration_ms:,} ms" if e.duration_ms is not None else ""
            msg = e.message or ("Waiting" if e.status == "PENDING" else "")
        cls = "pending" if e.status == "PENDING" else s.tone
        rows.append(
            f'<div class="if-stage {s.tone} {cls}" title="{esc(s.label)}"><span class="ic">{s.icon}</span><span class="n">{esc(e.title)}</span>'
            f'<span class="ms">{esc(ms)}</span><span class="m">{esc(msg)}</span></div>'
        )
    return f'<div class="if-stages">{"".join(rows)}</div>'


def status_bars(counts: dict[str, int], order: list[str]) -> None:
    """Status distribution as direct-labelled horizontal bars (a table view is provided by the caller)."""
    total = sum(counts.values())
    if not total:
        return
    top = max(counts.get(k, 0) for k in order) or 1
    rows = []
    for key in order:
        n = counts.get(key, 0)
        s = decision_style(key)
        pct = f"{n / total:.0%}"
        rows.append(
            f'<div class="if-bar" style="--c:{s.color}"><div class="lbl"><span class="ic">{s.icon}</span>{esc(s.label)}</div>'
            f'<div class="track" role="img" aria-label="{esc(s.label)}: {n} ({pct})"><div class="fill" style="width:{n / top * 100:.1f}%"></div></div>'
            f'<div class="val">{n} <span style="font-weight:400;opacity:.7">· {pct}</span></div></div>'
        )
    st.markdown("".join(rows), unsafe_allow_html=True)


def empty_state(title: str, body: str) -> None:
    st.markdown(
        f'<div class="if-empty"><div class="big">{esc(title)}</div><div class="if-sub">{esc(body)}</div></div>', unsafe_allow_html=True
    )


# ----------------------------------------------------------------------------- formatting


def fmt_money(amount: Decimal | None, currency: str | None) -> str:
    return format_money(amount, currency) if amount is not None else "—"


def fmt_time(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") + " UTC" if dt else "—"


def fmt_bytes(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"
