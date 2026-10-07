"""Saved simulator results must remain distinct from official Harbor evidence."""

import json
from copy import deepcopy

import pytest

from scripts import audit_text2wetlab_evidence as evidence


def _report(tmp_path, monkeypatch):
    task = "split-200ul-two-wells"
    task_dir = tmp_path / task
    task_dir.mkdir()
    files = {
        "protocol.py": b"metadata = {}\n",
        "instruction.md": b"Transfer from reservoir to two wells.\n",
        "official_runlog.py": b"# pinned runlog\n",
        "official_protocol_lint.py": b"# pinned lint\n",
    }
    for name, content in files.items():
        (task_dir / name).write_bytes(content)
    monkeypatch.setitem(evidence.RUNLOG_SHA256, task,
                        evidence._sha(task_dir / "official_runlog.py"))
    monkeypatch.setattr(evidence, "LINT_SHA256",
                        evidence._sha(task_dir / "official_protocol_lint.py"))
    case = {
        "task": task,
        "status": "simulator_passed",
        "official_score": None,
        "protocol_sha256": evidence._sha(task_dir / "protocol.py"),
        "instruction_sha256": evidence._sha(task_dir / "instruction.md"),
        "generation": {"status": "passed"},
        "official_protocol_lint": {"status": "passed", "source_sha256": evidence.LINT_SHA256},
        "ot2_simulator_gate": {"status": "passed"},
        "official_runlog_gate": {
            "status": "passed", "source_sha256": evidence.RUNLOG_SHA256[task],
            "adapter_event_validation": {"status": "passed"},
            "cross_well_aspiration_risk_count": 0,
            "local_rubric_audit": {
                "status": "needs_review",
                "items": [{"id": f"item_{index}", "status": "needs_review"} for index in range(5)],
            },
        },
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"dataset": evidence.DATASET, "revision": evidence.REVISION,
                                "official_score": None, "cases": [case]}))
    return path, case


def test_complete_local_evidence_still_has_no_official_score(tmp_path, monkeypatch):
    path, _ = _report(tmp_path, monkeypatch)
    audit = evidence.audit_reports([path])
    assert audit["mechanical_evidence_count"] == 1
    assert audit["official_score"] is None
    assert audit["cases"][0]["official_score"] is None
    assert audit["cases"][0]["needs_review"] == ["scientific_or_manual_review_remaining"]


def test_stale_candidate_and_failed_rubric_block_local_evidence(tmp_path, monkeypatch):
    path, case = _report(tmp_path, monkeypatch)
    (tmp_path / case["task"] / "protocol.py").write_text("metadata = {'apiLevel': '2.14'}\n")
    case["official_runlog_gate"]["local_rubric_audit"]["status"] = "failed"
    path.write_text(json.dumps({"dataset": evidence.DATASET, "revision": evidence.REVISION,
                                "official_score": None, "cases": [case]}))
    result = evidence.audit_reports([path])["cases"][0]
    assert not result["mechanical_evidence_complete"]
    assert "candidate_changed_since_report" in result["needs_review"]
    assert "local_rubric_missing_or_failed" in result["needs_review"]


def test_pinned_source_mismatch_is_visible(tmp_path, monkeypatch):
    path, case = _report(tmp_path, monkeypatch)
    (tmp_path / case["task"] / "official_runlog.py").write_text("# edited runlog\n")
    result = evidence.audit_reports([path])["cases"][0]
    assert "pinned_runlog_source_unconfirmed" in result["needs_review"]
    assert not result["mechanical_evidence_complete"]


def test_unverified_official_score_in_local_report_is_rejected(tmp_path, monkeypatch):
    path, case = _report(tmp_path, monkeypatch)
    path.write_text(json.dumps({"dataset": evidence.DATASET, "revision": evidence.REVISION,
                                "official_score": 1.0, "cases": [case]}))
    result = evidence.audit_reports([path])["cases"][0]
    assert "unverified_official_score_in_local_report" in result["needs_review"]
    assert result["official_score"] is None


def _recheck_report(tmp_path, monkeypatch):
    saved_root = tmp_path / "saved"
    saved_root.mkdir()
    prior_path, prior_case = _report(saved_root, monkeypatch)
    task = prior_case["task"]
    saved_task_dir = saved_root / task
    trace_path = saved_task_dir / "generation_trace.json"
    trace_path.write_text(json.dumps({
        "task_sha256": prior_case["instruction_sha256"],
        "status": "simulated", "static_validation_passed": True,
        "event_validation_passed": True,
        "attempts": [{"number": 1, "model": "qwen", "validation": "passed",
                      "simulation": "passed", "code_sha256": prior_case["protocol_sha256"]}],
    }))
    current_root = tmp_path / "rechecked"
    current_task_dir = current_root / task
    current_task_dir.mkdir(parents=True)
    for name in ("official_protocol_lint.py", "official_runlog.py"):
        (current_task_dir / name).write_bytes((saved_task_dir / name).read_bytes())
    case = deepcopy(prior_case)
    case["generation"] = None
    case["current_static_validation"] = {"status": "passed"}
    case["source_paper_sha256"] = None
    case["saved_candidate_provenance"] = {
        "status": "passed", "saved_run_root": str(saved_root.resolve()),
        "saved_report_path": str(prior_path.resolve()),
        "saved_report_sha256": evidence._sha(prior_path),
        "generation_trace_path": str(trace_path.resolve()),
        "generation_trace_sha256": evidence._sha(trace_path),
        "candidate_path": str((saved_task_dir / "protocol.py").resolve()),
        "candidate_sha256": prior_case["protocol_sha256"],
        "instruction_sha256": prior_case["instruction_sha256"],
        "source_paper_sha256": None, "model": "qwen", "attempt_number": 1,
        "accepted_via_patch": False, "prior_generation_status": "passed",
    }
    path = current_root / "report.json"
    report = {"dataset": evidence.DATASET, "revision": evidence.REVISION,
              "metric": "saved_candidate_current_local_gate_recheck",
              "saved_run_root": str(saved_root.resolve()),
              "official_score": None, "cases": [case]}
    path.write_text(json.dumps(report))
    return path, saved_root, current_root


def test_provenance_verified_recheck_has_complete_local_evidence(tmp_path, monkeypatch):
    path, _, current_root = _recheck_report(tmp_path, monkeypatch)
    audit = evidence.audit_reports([path])
    assert audit["mechanical_evidence_count"] == 1
    assert audit["official_score"] is None
    assert audit["cases"][0]["needs_review"] == ["scientific_or_manual_review_remaining"]
    assert not (current_root / "split-200ul-two-wells" / "protocol.py").exists()


@pytest.mark.parametrize("tamper,reason", [
    ("candidate", "saved_candidate_provenance_unconfirmed"),
    ("trace", "saved_candidate_provenance_unconfirmed"),
    ("prior_report", "saved_candidate_provenance_unconfirmed"),
    ("candidate_path", "saved_candidate_provenance_unconfirmed"),
    ("current_lint", "pinned_lint_source_unconfirmed"),
    ("current_runlog", "pinned_runlog_source_unconfirmed"),
    ("generation", "recheck_generation_must_be_unset"),
    ("current_static", "current_static_validation_unconfirmed"),
    ("metric", "generation_gate_unconfirmed"),
])
def test_tampered_recheck_cannot_pass_audit(tmp_path, monkeypatch, tamper, reason):
    path, saved_root, current_root = _recheck_report(tmp_path, monkeypatch)
    task = "split-200ul-two-wells"
    if tamper == "candidate":
        (saved_root / task / "protocol.py").write_text("changed\n")
    elif tamper == "trace":
        (saved_root / task / "generation_trace.json").write_text("{}")
    elif tamper == "prior_report":
        (saved_root / "report.json").write_text("{}")
    elif tamper == "current_lint":
        (current_root / task / "official_protocol_lint.py").write_text("changed\n")
    elif tamper == "current_runlog":
        (current_root / task / "official_runlog.py").write_text("changed\n")
    else:
        report = json.loads(path.read_text())
        case = report["cases"][0]
        if tamper == "candidate_path":
            case["saved_candidate_provenance"]["candidate_path"] = str(path)
        elif tamper == "generation":
            case["generation"] = {"status": "passed"}
        elif tamper == "current_static":
            case["current_static_validation"]["status"] = "failed"
        else:
            report["metric"] = "local_static_simulator_runlog_and_event_safety_gates"
        path.write_text(json.dumps(report))
    result = evidence.audit_reports([path])["cases"][0]
    assert not result["mechanical_evidence_complete"]
    assert reason in result["needs_review"]
