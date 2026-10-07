"""Versioned evidence required for a completed physical rehearsal.

This module has no native dependencies, so read-only planning and release
checks can inspect saved reports even when SuperDex is not installed.
"""

from __future__ import annotations

from typing import Any

# Version the pyBravo integration separately from the native SDK version. A
# task-only or older unmarked pass cannot imply that current checks ran.
PHYSICAL_SIMULATION_CONTRACT = "pybravo.superdex-rehearsal.v1"


def is_checked_physical_report(report: Any) -> bool:
    """Validate the report attached to a successful workflow completion.

    Zero-motion workflows (for example Wait or Manual only) are valid. Their
    zero counters must remain visible; do not imply a trajectory was checked.
    Callers must also establish workflow completion and unchanged input hashes.
    """
    if not isinstance(report, dict):
        return False
    counts = [report.get(name) for name in ("moves_checked", "samples_checked", "contact_queries")]
    if not all(type(count) is int and count >= 0 for count in counts):
        return False
    if counts[0] > 0 and counts[1] == 0:
        return False
    return (
        report.get("contract_version") == PHYSICAL_SIMULATION_CONTRACT
        and report.get("engine") == "SuperDex"
        and isinstance(report.get("engine_version"), str)
        and bool(report["engine_version"].strip())
        and report.get("status") == "checked"
        and not report.get("last_error")
    )
