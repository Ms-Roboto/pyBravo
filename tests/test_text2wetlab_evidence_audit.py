"""Saved simulator results must remain distinct from official Harbor evidence."""

import json

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
