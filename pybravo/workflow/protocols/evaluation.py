"""Read-only regression metrics from saved protocol revisions.

These metrics describe observed review activity and strict simulation results.
They do not turn a synthetic fixture or an unreviewed session into scientific
qualification evidence.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from .store import ProtocolStore


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def summarize_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate traceable coverage, review, and simulation outcomes."""
    simulation_status = Counter()
    agreement = Counter()
    release_basis = Counter()
    method_selections = 0
    curated_method_selections = 0
    bulk_method_proposals = 0
    top_candidate_selections = 0
    closest_candidate_selections = 0
    scientist_edits = 0
    unresolved_questions = 0
    validation_errors = 0
    with_pinned_methods = 0
    approved = 0
    for session in sessions:
        plan = session.get("plan") or {}
        decisions = _rows(plan.get("decisions")) if isinstance(plan, Mapping) else []
        simulation_status[str((session.get("simulation") or {}).get("status") or "not_run")] += 1
        approval = session.get("approval") or {}
        if approval:
            approved += 1
            release_basis[str(approval.get("qualification") or "unspecified")] += 1
        scientist_edits += sum(item.get("event") == "scientist_edit" for item in _rows(session.get("history")))
        report = session.get("validation") or {}
        unresolved_questions += len(_rows(report.get("questions") if report else plan.get("questions")))
        validation_errors += sum(issue.get("severity") == "error" for issue in _rows(report.get("issues")))
        if _contains_method_ref(plan.get("steps") if isinstance(plan, Mapping) else []):
            with_pinned_methods += 1
        for decision in decisions:
            if not str(decision.get("path") or "").endswith("/method_selection"):
                continue
            value = decision.get("value") or {}
            if not isinstance(value, Mapping):
                continue
            selected = value.get("selected") or {}
            offered = _rows(value.get("offered"))
            chosen = next((item for item in offered if item.get("method_id") == selected.get("method_id")
                           and item.get("revision") == selected.get("revision")), None)
            if not selected.get("method_id") or not selected.get("revision"):
                continue
            method_selections += 1
            if value.get("selection_mode") == "bulk_proposal":
                bulk_method_proposals += 1
            elif chosen is None:
                curated_method_selections += 1
            else:
                top_candidate_selections += chosen.get("rank") == 1
                closest_candidate_selections += chosen.get("match_kind") == "closest"
            verdict = value.get("expert_agreement")
            agreement[verdict if verdict in {"agree", "disagree"} else "unscored"] += 1
    return {
        "standard": "pybravo.protocol-evaluation-summary",
        "schema_version": "1.0.0",
        "sessions": len(sessions),
        "approved_sessions": approved,
        "sessions_with_pinned_methods": with_pinned_methods,
        "method_selection_events": method_selections,
        "bulk_method_proposals": bulk_method_proposals,
        "curated_method_selections": curated_method_selections,
        "top_candidate_selections": top_candidate_selections,
        "closest_candidate_selections": closest_candidate_selections,
        "explicit_expert_agreement": dict(sorted(agreement.items())),
        "scientist_edit_revisions": scientist_edits,
        "unresolved_questions": unresolved_questions,
        "validation_error_issues": validation_errors,
        "strict_simulation_status": dict(sorted(simulation_status.items())),
        "release_basis": dict(sorted(release_basis.items())),
        "coverage_note": "These counts include local drafts. Only approved records carry a release basis; synthetic tests are not scientist reviews.",
    }


def _contains_method_ref(steps: Any) -> bool:
    for step in _rows(steps):
        if step.get("method_ref"):
            return True
        if step.get("kind") == "repeat" and _contains_method_ref(step.get("steps")):
            return True
    return False


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Summarize saved Bravo protocol review and simulation evidence")
    parser.add_argument("--store", help="Protocol store directory; defaults to PYBRAVO_PROTOCOL_STORE")
    args = parser.parse_args()
    print(json.dumps(summarize_sessions(ProtocolStore(args.store).list("sessions")), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
