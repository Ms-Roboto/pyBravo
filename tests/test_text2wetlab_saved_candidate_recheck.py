"""A saved Qwen protocol can be rechecked without changing or regenerating it."""

import json
import sys
from pathlib import Path

import pytest

from scripts import evaluate_text2wetlab as runner

TASK = "split-200ul-two-wells"
INSTRUCTION = b"Move 200 uL from a reservoir into two wells.\n"
CODE = b"metadata = {}\n"


def _saved_run(root: Path, *, patched: bool = False) -> tuple[Path, Path]:
    task_dir = root / TASK
    task_dir.mkdir(parents=True)
    protocol = task_dir / "protocol.py"
    protocol.write_bytes(CODE)
    (task_dir / "instruction.md").write_bytes(INSTRUCTION)
    attempt = {"number": 1, "model": "qwen", "validation": "passed",
               "simulation": "passed", "code_sha256": runner._digest(CODE)}
    if patched:
        attempt["code_sha256"] = runner._digest(b"original Qwen draft\n")
        attempt["simulation"] = "failed"
        attempt["accepted_via_patch"] = True
        attempt["patches"] = [{"number": 1, "model": "qwen", "status": "accepted",
                               "simulation": "passed", "event_validation": "passed",
                               "input_code_sha256": attempt["code_sha256"],
                               "code_sha256": runner._digest(CODE)}]
        (task_dir / "candidate_attempt_1_patch_1.py").write_bytes(CODE)
        (task_dir / "candidate_attempt_1.py").write_bytes(b"original Qwen draft\n")
    trace = {"task_sha256": runner._digest(INSTRUCTION), "status": "simulated",
             "static_validation_passed": True, "event_validation_passed": True,
             "attempts": [attempt]}
    (task_dir / "generation_trace.json").write_text(json.dumps(trace), encoding="utf-8")
    report = {"dataset": runner.DATASET, "revision": runner.REVISION,
              "cases": [{"task": TASK, "instruction_sha256": runner._digest(INSTRUCTION),
                         "source_paper_sha256": None, "protocol_sha256": runner._digest(CODE),
                         "generation": {"status": "passed"}}]}
    (root / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return protocol, task_dir / "generation_trace.json"


def _pin_source(monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        INSTRUCTION if name == "instruction.md" else None))


@pytest.mark.parametrize("patched", [False, True])
def test_recheck_uses_saved_candidate_and_current_gates_without_generation(tmp_path, monkeypatch,
                                                                            patched):
    source_root = tmp_path / "saved"
    protocol, _ = _saved_run(source_root, patched=patched)
    output_root = tmp_path / "rechecked"
    _pin_source(monkeypatch)
    monkeypatch.setattr("pybravo.evals.text2wetlab.adapter.validate_ot2_source",
                        lambda code: None)
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        assert command == ["/tmp/opentrons_simulate", str(protocol)]
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_official_lint", lambda *args: {
        "status": "passed", "violations": [],
    })
    monkeypatch.setattr(runner, "_official_runlog", lambda *args: {
        "status": "passed", "adapter_event_validation": {"status": "passed"},
        "cross_well_aspiration_risk_count": 0,
        "local_rubric_audit": {"status": "supported"},
    })
    result = runner.recheck_task(TASK, saved_run_root=source_root, output_dir=output_root,
                                 simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "simulator_passed"
    assert result["generation"] is None
    assert result["saved_candidate_provenance"]["status"] == "passed"
    assert result["saved_candidate_provenance"]["accepted_via_patch"] is patched
    assert result["protocol_sha256"] == runner._digest(CODE)
    assert result["current_static_validation"] == {"status": "passed"}
    assert len(calls) == 1
    assert protocol.read_bytes() == CODE
    assert not (output_root / TASK / "protocol.py").exists()
    assert not (output_root / TASK / "generation_trace.json").exists()


@pytest.mark.parametrize("tamper", ["protocol", "instruction", "trace_hash", "model",
                                    "report_digest", "revision", "generation"])
def test_recheck_refuses_unverified_provenance_before_execution(tmp_path, monkeypatch, tamper):
    source_root = tmp_path / "saved"
    protocol, trace_path = _saved_run(source_root)
    report_path = source_root / "report.json"
    if tamper == "protocol":
        protocol.write_text("print('changed')\n", encoding="utf-8")
    elif tamper == "instruction":
        (source_root / TASK / "instruction.md").write_text("changed\n", encoding="utf-8")
    elif tamper in {"trace_hash", "model"}:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace["attempts"][-1]["code_sha256" if tamper == "trace_hash" else "model"] = (
            "0" * 64 if tamper == "trace_hash" else "other-model")
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
    else:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if tamper == "report_digest":
            report["cases"][0]["protocol_sha256"] = "0" * 64
        elif tamper == "revision":
            report["revision"] = "another revision"
        else:
            report["cases"][0]["generation"]["status"] = "failed"
        report_path.write_text(json.dumps(report), encoding="utf-8")
    _pin_source(monkeypatch)
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("candidate executed before provenance was checked")))
    result = runner.recheck_task(TASK, saved_run_root=source_root,
                                 output_dir=tmp_path / "rechecked",
                                 simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "saved_candidate_provenance_unconfirmed"
    assert result["generation"] is None
    assert result["official_protocol_lint"] is None
    assert result["ot2_simulator_gate"] is None


def test_recheck_records_current_rubric_failure_without_accepted_status(tmp_path, monkeypatch):
    source_root = tmp_path / "saved"
    _saved_run(source_root)
    _pin_source(monkeypatch)
    monkeypatch.setattr("pybravo.evals.text2wetlab.adapter.validate_ot2_source",
                        lambda code: None)
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: {"status": "passed"})
    monkeypatch.setattr(runner, "_official_lint", lambda *args: {"status": "passed"})
    monkeypatch.setattr(runner, "_official_runlog", lambda *args: {
        "status": "passed", "adapter_event_validation": {"status": "passed"},
        "local_rubric_audit": {"status": "failed"},
    })
    result = runner.recheck_task(TASK, saved_run_root=source_root,
                                 output_dir=tmp_path / "rechecked",
                                 simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "local_rubric_failed_or_unavailable"
    assert result["official_runlog_gate"]["local_rubric_audit"]["status"] == "failed"
    assert result["official_score"] is None


def test_cli_recheck_routes_to_saved_runner_and_leaves_generation_unset(tmp_path, monkeypatch):
    simulator = tmp_path / "opentrons_simulate"
    simulator.write_text("", encoding="utf-8")
    output_root = tmp_path / "out"
    source_root = tmp_path / "saved"
    monkeypatch.setattr(runner, "run_task", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("generation runner called")))
    monkeypatch.setattr(runner, "recheck_task", lambda *args, **kwargs: {
        "task": TASK, "status": "simulator_passed", "generation": None,
        "ot2_simulator_gate": {"status": "passed"},
        "official_runlog_gate": {"status": "passed"},
    })
    monkeypatch.setattr(sys, "argv", ["evaluate_text2wetlab.py", "--task", TASK,
                                  "--recheck-from", str(source_root),
                                  "--simulator", str(simulator),
                                  "--output-dir", str(output_root)])
    assert runner.main() == 0
    report = json.loads((output_root / "report.json").read_text(encoding="utf-8"))
    assert report["metric"] == "saved_candidate_current_local_gate_recheck"
    assert report["cases"][0]["generation"] is None
    assert report["official_score"] is None
