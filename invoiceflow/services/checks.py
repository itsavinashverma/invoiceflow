"""Tiny constructors for :class:`CheckResult`, shared by the rule modules."""

from __future__ import annotations

from invoiceflow.schemas import CheckResult


def passed(check: str, title: str, detail: str) -> CheckResult:
    return CheckResult(check=check, title=title, passed=True, detail=detail)


def failed(check: str, title: str, detail: str, reason_code: str) -> CheckResult:
    return CheckResult(check=check, title=title, passed=False, detail=detail, reason_code=reason_code)


def skipped(check: str, title: str, why: str) -> CheckResult:
    """A check that could not run. Reported separately; it is neither a pass nor a failure."""
    return CheckResult(check=check, title=title, passed=False, detail=why, reason_code=None)
