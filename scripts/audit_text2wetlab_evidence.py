"""Audit saved Text2WetLab reports without converting local passes to a Harbor score.

The official Harbor verifier additionally runs anti-hack checks and a rubric
judge. This script never runs either one; it checks whether a saved local report
is internally consistent and identifies evidence still missing for each task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

if __package__:
    from scripts.evaluate_text2wetlab import DATASET, REVISION, TASKS
else:
    from evaluate_text2wetlab import DATASET, REVISION, TASKS

# SHA-256 of the pinned dataset's verifier files. These identify what the local
# runner used; they do not imply its anti-hack gate or judge was executed.
LINT_SHA256 = "2d71c2f7718063a2b56117408f6f89cadd790104704a669d9d8dc174603eb8c7"
RUNLOG_SHA256 = {
    task: ("3823451799e24bd3ba17088706936cb7fd9d92aab3879875e8b838430f96a690"
           if task == "ecoli-heat-shock-transformation" else
           "98acd3206431791e47894b3e57912a8da6cad9488723bb36c0234dabed9c4fab"
           if task == "opentrons-rna-extraction" else
           "f873e6af14aff3269b805ca0014e8382fce1c8360224a74c7f8dfdce1ae9396a")
    for task in TASKS
}


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _status(value: object) -> str | None:
    return value.get("status") if isinstance(value, dict) else None


def audit_case(case: dict, report_path: Path, *, report_revision: str | None,
               report_official_score: object = None) -> dict:
    task = case.get("task")
    reasons: list[str] = []
    if task not in TASKS:
        reasons.append("unknown_task")
    if report_revision != REVISION:
        reasons.append("different_dataset_revision")
    if case.get("official_score") is not None or report_official_score is not None:
        reasons.append("unverified_official_score_in_local_report")

    task_dir = report_path.parent / str(task)
    protocol = task_dir / "protocol.py"
    expected_digest = case.get("protocol_sha256")
    if not isinstance(expected_digest, str) or not protocol.is_file():
        reasons.append("candidate_or_digest_missing")
    elif _sha(protocol) != expected_digest:
        reasons.append("candidate_changed_since_report")
    if _sha(task_dir / "instruction.md") != case.get("instruction_sha256"):
        reasons.append("instruction_source_changed_or_missing")

    lint_expected = "not_applicable" if task == "opentrons-rna-extraction" else "passed"
    gates = {
        "generation": _status(case.get("generation")),
        "lint": _status(case.get("official_protocol_lint")),
        "simulator": _status(case.get("ot2_simulator_gate")),
        "pinned_runlog": _status(case.get("official_runlog_gate")),
    }
    for name, wanted in (("generation", "passed"), ("lint", lint_expected),
                         ("simulator", "passed"), ("pinned_runlog", "passed")):
        if gates[name] != wanted:
            reasons.append(f"{name}_gate_unconfirmed")

    runlog = case.get("official_runlog_gate") or {}
    pinned_runlog = RUNLOG_SHA256.get(task)
    if (pinned_runlog is None or runlog.get("source_sha256") != pinned_runlog
            or _sha(task_dir / "official_runlog.py") != pinned_runlog):
        reasons.append("pinned_runlog_source_unconfirmed")
    if task != "opentrons-rna-extraction":
        lint = case.get("official_protocol_lint") or {}
        if (lint.get("source_sha256") != LINT_SHA256
                or _sha(task_dir / "official_protocol_lint.py") != LINT_SHA256):
            reasons.append("pinned_lint_source_unconfirmed")
    if _status(runlog.get("adapter_event_validation")) != "passed":
        reasons.append("event_safety_unconfirmed")
    if runlog.get("cross_well_aspiration_risk_count") != 0:
        reasons.append("cross_well_risk_unresolved")
    rubric = runlog.get("local_rubric_audit") or {}
    items = rubric.get("items")
    valid_items = (isinstance(items, list) and len(items) == 5
                   and all(isinstance(item, dict) for item in items))
    item_ids = [item.get("id") for item in items] if valid_items else []
    if (rubric.get("status") not in {"supported", "needs_review"}
            or not valid_items or len(set(item_ids)) != 5
            or any(item.get("status") not in {"supported", "needs_review"} for item in items)):
        reasons.append("local_rubric_missing_or_failed")
    if case.get("status") != "simulator_passed":
        reasons.append("current_local_acceptance_not_passed")

    return {
        "task": task,
        "report": str(report_path),
        "candidate_sha256": expected_digest if isinstance(expected_digest, str) else None,
        "mechanical_evidence_complete": not reasons,
        "local_rubric_status": rubric.get("status"),
        "needs_review": sorted(set(reasons + (["scientific_or_manual_review_remaining"]
                                                  if rubric.get("status") == "needs_review" else []))),
        "official_verification_required": ["pinned_anti_hack_gate", "pinned_rubric_judge"],
        "official_score": None,
    }


def audit_reports(paths: list[Path]) -> dict:
    cases: list[dict] = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("cases"), list):
            raise ValueError(f"Invalid Text2WetLab report: {path}")
        if report.get("dataset") != DATASET:
            raise ValueError(f"Unexpected dataset in {path}")
        for case in report["cases"]:
            if not isinstance(case, dict):
                raise ValueError(f"Invalid task record in {path}")
            cases.append(audit_case(case, path, report_revision=report.get("revision"),
                                    report_official_score=report.get("official_score")))

    complete = {case["task"] for case in cases if case["mechanical_evidence_complete"]}
    return {
        "dataset": DATASET,
        "revision": REVISION,
        "metric": "saved_local_evidence_integrity_only",
        "task_count": len(TASKS),
        "mechanical_evidence_tasks": sorted(complete),
        "mechanical_evidence_count": len(complete),
        "missing_mechanical_evidence_tasks": sorted(set(TASKS) - complete),
        "official_score": None,
        "official_score_note": "The pinned Harbor anti-hack gate and rubric judge have not been run by this audit.",
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True,
                        help="Saved local evaluation report.json; repeat for multiple runs")
    parser.add_argument("--output", type=Path, help="Write the audit as JSON")
    args = parser.parse_args()
    audit = audit_reports(args.report)
    rendered = json.dumps(audit, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
