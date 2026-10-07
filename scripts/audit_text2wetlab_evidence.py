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


def _saved_recheck_reasons(case: dict, report_path: Path,
                           saved_run_root: str | None) -> list[str]:
    """Independently check the saved inputs named by a model-free recheck."""
    reasons: list[str] = []
    provenance = case.get("saved_candidate_provenance")
    if not isinstance(provenance, dict) or provenance.get("status") != "passed":
        return ["saved_candidate_provenance_unconfirmed"]
    if case.get("generation") is not None:
        reasons.append("recheck_generation_must_be_unset")
    if _status(case.get("current_static_validation")) != "passed":
        reasons.append("current_static_validation_unconfirmed")
    task = case.get("task")
    root_text = provenance.get("saved_run_root")
    if not isinstance(root_text, str) or not isinstance(saved_run_root, str):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    root = Path(root_text).expanduser().resolve()
    if root != Path(saved_run_root).expanduser().resolve():
        reasons.append("saved_candidate_provenance_unconfirmed")
    task_dir = root / str(task)
    paths = {
        "saved_report_path": root / "report.json",
        "generation_trace_path": task_dir / "generation_trace.json",
        "candidate_path": task_dir / "protocol.py",
    }
    for field, expected in paths.items():
        value = provenance.get(field)
        if not isinstance(value, str) or Path(value).expanduser().resolve() != expected:
            return reasons + ["saved_candidate_provenance_unconfirmed"]
    saved_report = paths["saved_report_path"]
    trace_path = paths["generation_trace_path"]
    protocol = paths["candidate_path"]
    if (_sha(saved_report) != provenance.get("saved_report_sha256")
            or _sha(trace_path) != provenance.get("generation_trace_sha256")
            or _sha(protocol) != provenance.get("candidate_sha256")
            or _sha(protocol) != case.get("protocol_sha256")):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    try:
        prior = json.loads(saved_report.read_text(encoding="utf-8"))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    if not isinstance(prior, dict) or not isinstance(trace, dict):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    prior_cases = prior.get("cases")
    matches = ([item for item in prior_cases
                if isinstance(item, dict) and item.get("task") == task]
               if isinstance(prior_cases, list) else [])
    instruction_sha256 = _sha(task_dir / "instruction.md")
    if (prior.get("dataset") != DATASET or prior.get("revision") != REVISION
            or len(matches) != 1 or instruction_sha256 is None
            or instruction_sha256 != case.get("instruction_sha256")
            or instruction_sha256 != provenance.get("instruction_sha256")
            or trace.get("task_sha256") != instruction_sha256):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    prior_case = matches[0]
    if (prior_case.get("instruction_sha256") != instruction_sha256
            or prior_case.get("protocol_sha256") != case.get("protocol_sha256")
            or prior_case.get("source_paper_sha256") != case.get("source_paper_sha256")
            or provenance.get("source_paper_sha256") != case.get("source_paper_sha256")
            or _status(prior_case.get("generation")) != "passed"
            or provenance.get("prior_generation_status") != "passed"
            or trace.get("status") != "simulated"
            or trace.get("static_validation_passed") is not True
            or ("event_validation_passed" in trace
                and trace["event_validation_passed"] is not True)):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    scientific_source = trace.get("scientific_source")
    if scientific_source is not None and (not isinstance(scientific_source, dict)
                                          or scientific_source.get("source_sha256")
                                          != case.get("source_paper_sha256")):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    attempts = trace.get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    attempt = attempts[-1]
    model = attempt.get("model")
    number = attempt.get("number")
    if (not isinstance(model, str) or "qwen" not in model.casefold()
            or model != provenance.get("model") or number != provenance.get("attempt_number")
            or number != len(attempts) or attempt.get("validation") != "passed"):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    digest = case.get("protocol_sha256")
    patched = attempt.get("accepted_via_patch") is True
    if patched != provenance.get("accepted_via_patch"):
        return reasons + ["saved_candidate_provenance_unconfirmed"]
    if patched:
        patches = attempt.get("patches")
        if not isinstance(patches, list) or not patches or not isinstance(patches[-1], dict):
            return reasons + ["saved_candidate_provenance_unconfirmed"]
        patch = patches[-1]
        patch_model = patch.get("model")
        prior_hashes = [attempt.get("code_sha256")]
        prior_hashes.extend(item.get("code_sha256") for item in patches[:-1]
                            if isinstance(item, dict))
        if (patch.get("status") != "accepted" or patch.get("simulation") != "passed"
                or patch.get("event_validation") != "passed"
                or patch.get("code_sha256") != digest
                or patch.get("input_code_sha256") not in prior_hashes
                or not isinstance(patch_model, str) or "qwen" not in patch_model.casefold()
                or patch.get("number") != len(patches)
                or _sha(task_dir / f"candidate_attempt_{number}.py") != attempt.get("code_sha256")
                or _sha(task_dir / f"candidate_attempt_{number}_patch_{len(patches)}.py") != digest):
            reasons.append("saved_candidate_provenance_unconfirmed")
    elif attempt.get("simulation") != "passed" or attempt.get("code_sha256") != digest:
        reasons.append("saved_candidate_provenance_unconfirmed")
    if (report_path.parent / str(task) / "protocol.py").exists():
        reasons.append("unexpected_recheck_protocol_copy")
    return reasons


def audit_case(case: dict, report_path: Path, *, report_revision: str | None,
               report_official_score: object = None, report_metric: str | None = None,
               saved_run_root: str | None = None) -> dict:
    task = case.get("task")
    reasons: list[str] = []
    if task not in TASKS:
        reasons.append("unknown_task")
    if report_revision != REVISION:
        reasons.append("different_dataset_revision")
    if case.get("official_score") is not None or report_official_score is not None:
        reasons.append("unverified_official_score_in_local_report")

    task_dir = report_path.parent / str(task)
    expected_digest = case.get("protocol_sha256")
    is_recheck = report_metric == "saved_candidate_current_local_gate_recheck"
    if is_recheck:
        reasons.extend(_saved_recheck_reasons(case, report_path, saved_run_root))
    else:
        protocol = task_dir / "protocol.py"
        if not isinstance(expected_digest, str) or not protocol.is_file():
            reasons.append("candidate_or_digest_missing")
        elif _sha(protocol) != expected_digest:
            reasons.append("candidate_changed_since_report")
        if _sha(task_dir / "instruction.md") != case.get("instruction_sha256"):
            reasons.append("instruction_source_changed_or_missing")

    lint_expected = "not_applicable" if task == "opentrons-rna-extraction" else "passed"
    gates = {
        "lint": _status(case.get("official_protocol_lint")),
        "simulator": _status(case.get("ot2_simulator_gate")),
        "pinned_runlog": _status(case.get("official_runlog_gate")),
    }
    if not is_recheck and _status(case.get("generation")) != "passed":
        reasons.append("generation_gate_unconfirmed")
    for name, wanted in (("lint", lint_expected),
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
                                    report_official_score=report.get("official_score"),
                                    report_metric=report.get("metric"),
                                    saved_run_root=report.get("saved_run_root")))

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
