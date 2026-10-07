"""The OT-2 benchmark runner must not mislabel a simulator pass as an official score."""

import json
from pathlib import Path

import pytest

from scripts import evaluate_text2wetlab as runner


def test_runner_reports_simulator_gate_separately_from_official_score(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Move 200 uL from a reservoir into two wells." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))

    generation_commands = []

    def fake_run(command, *, timeout):
        if command[1:3] == ["-m", "pybravo.evals.text2wetlab"]:
            generation_commands.append(command)
            (tmp_path / "split-200ul-two-wells" / "protocol.py").write_text("metadata = {}\n")
            (tmp_path / "split-200ul-two-wells" / "generation_trace.json").write_text(
                json.dumps({"static_validation_passed": True, "event_validation_passed": True,
                            "status": "simulated"}))
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_official_lint", lambda *args: {"status": "passed", "violations": []})
    monkeypatch.setattr(runner, "_official_runlog", lambda *args: {
        "status": "passed", "event_count": 5, "adapter_event_validation": {"status": "passed"},
        "local_rubric_audit": {"status": "supported"},
    })
    result = runner.run_task("split-200ul-two-wells", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"),
                             model_timeout=123, max_output_tokens=4096)
    assert result["status"] == "simulator_passed"
    assert result["ot2_simulator_gate"]["status"] == "passed"
    assert result["official_runlog_gate"]["status"] == "passed"
    assert result["official_score"] is None
    assert result["protocol_sha256"]
    assert generation_commands[0][generation_commands[0].index("--model-timeout") + 1] == "123"
    assert generation_commands[0][generation_commands[0].index("--max-output-tokens") + 1] == "4096"


def test_runner_never_executes_candidate_when_static_gate_is_unconfirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Transfer 100 uL into A1." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        if command[1:3] == ["-m", "pybravo.evals.text2wetlab"]:
            (tmp_path / "a1-a12-100ul" / "protocol.py").write_text("print('do not run')\n")
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_official_lint", lambda *args: {"status": "passed", "violations": []})
    result = runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "generation_safety_gates_unconfirmed"
    assert result["ot2_simulator_gate"] is None
    assert len(calls) == 1


def test_runner_never_executes_candidate_when_event_gate_is_false(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Transfer 100 uL into A1." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        (tmp_path / "a1-a12-100ul" / "protocol.py").write_text("metadata = {}\n")
        (tmp_path / "a1-a12-100ul" / "generation_trace.json").write_text(
            json.dumps({"static_validation_passed": True, "event_validation_passed": False,
                        "status": "simulated"}))
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "generation_safety_gates_unconfirmed"
    assert result["ot2_simulator_gate"] is None
    assert len(calls) == 1


def test_official_lint_failure_prevents_simulator_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Transfer 100 uL into A1." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        (tmp_path / "a1-a12-100ul" / "protocol.py").write_text("import os\n")
        (tmp_path / "a1-a12-100ul" / "generation_trace.json").write_text(
            json.dumps({"static_validation_passed": True, "event_validation_passed": True,
                        "status": "simulated"}))
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_official_lint", lambda *args: {
        "status": "failed", "violations": ["line 1: import os"],
    })
    result = runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "official_lint_failed_or_unavailable"
    assert result["official_protocol_lint"]["violations"] == ["line 1: import os"]
    assert result["ot2_simulator_gate"] is None
    assert len(calls) == 1


def test_rna_task_has_no_separate_lint_but_still_runs_simulator(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"RNA task with custom labware." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py"
        else b'{}' if name == runner.RNA_LABWARE else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        if command[1:3] == ["-m", "pybravo.evals.text2wetlab"]:
            (tmp_path / "opentrons-rna-extraction" / "protocol.py").write_text("metadata = {}\n")
            (tmp_path / "opentrons-rna-extraction" / "generation_trace.json").write_text(
                json.dumps({"static_validation_passed": True, "event_validation_passed": True,
                            "status": "simulated"}))
        return {"status": "passed", "returncode": 0}

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "_official_runlog", lambda *args: {
        "status": "passed", "event_count": 5, "adapter_event_validation": {"status": "passed"},
        "local_rubric_audit": {"status": "supported"},
    })
    result = runner.run_task("opentrons-rna-extraction", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["official_protocol_lint"] == {"status": "not_applicable", "violations": []}
    assert result["status"] == "simulator_passed"
    assert len(calls) == 2
    assert "--labware-dir" in calls[0]
    assert "-L" in calls[1]
    assert result["custom_labware_sha256"] == runner._digest(b'{}')


def test_rna_missing_custom_labware_blocks_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"RNA task with custom labware." if name == "instruction.md" else None))
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))
    result = runner.run_task("opentrons-rna-extraction", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "blocked_missing_custom_labware"
    assert result["official_score"] is None


def test_missing_paper_blocks_generation_instead_of_model_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Follow the method in /data/paper.txt." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))
    result = runner.run_task("ecoli-heat-shock-transformation", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"))
    assert result["status"] == "blocked_missing_source_paper"
    assert result["generation"] is None
    assert result["official_score"] is None


def test_paper_override_requires_pinned_digest_and_is_not_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Follow the method in /data/paper.txt." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    paper = tmp_path / "recovered-paper.txt"
    paper.write_bytes(b"source text")
    monkeypatch.setattr(runner, "ECOLI_PAPER_SHA256", runner._digest(b"source text"))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        return {"status": "failed", "returncode": 1}

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run_task("ecoli-heat-shock-transformation", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"), paper_override=paper)
    assert result["status"] == "generation_failed"
    assert "--paper-file" in calls[0]
    assert calls[0][calls[0].index("--paper-file") + 1] == str(paper)
    assert not (tmp_path / "ecoli-heat-shock-transformation" / "paper.txt").exists()

    monkeypatch.setattr(runner, "ECOLI_PAPER_SHA256", "0" * 64)
    result = runner.run_task("ecoli-heat-shock-transformation", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"), paper_override=paper)
    assert result["status"] == "paper_override_digest_mismatch"
    assert len(calls) == 1


def test_runner_passes_bounded_repair_count_to_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Transfer 100 uL into A1." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        return {"status": "failed", "returncode": 1}

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"), repair_attempts=5)
    assert result["status"] == "generation_failed"
    assert calls[0][calls[0].index("--repair-attempts") + 1] == "5"
    try:
        runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                        simulator=Path("/tmp/opentrons_simulate"), repair_attempts=6)
    except ValueError:
        pass
    else:
        raise AssertionError("Out-of-range repair count was accepted")


def test_runner_passes_bounded_patch_count_to_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_source_bytes", lambda task, name, root: (
        b"Transfer 100 uL into A1." if name == "instruction.md"
        else b"# pinned event logger" if name == "tests/runlog.py" else None))
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        return {"status": "failed", "returncode": 1}

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                             simulator=Path("/tmp/opentrons_simulate"), patch_attempts=3)
    assert result["status"] == "generation_failed"
    assert calls[0][calls[0].index("--patch-attempts") + 1] == "3"
    try:
        runner.run_task("a1-a12-100ul", output_dir=tmp_path,
                        simulator=Path("/tmp/opentrons_simulate"), patch_attempts=4)
    except ValueError:
        pass
    else:
        raise AssertionError("Out-of-range patch count was accepted")


def test_event_audit_flags_cross_sample_aspiration_but_not_stock_distribution():
    events = [
        {"kind": "pick", "instrument": "p300"},
        {"kind": "aspirate", "instrument": "p300", "labware": "reservoir on 2", "well": "A1"},
        {"kind": "dispense", "instrument": "p300", "labware": "sample on 1", "well": "A1"},
        {"kind": "aspirate", "instrument": "p300", "labware": "reservoir on 2", "well": "A1"},
        {"kind": "dispense", "instrument": "p300", "labware": "sample on 1", "well": "B1"},
        {"kind": "drop", "instrument": "p300"},
        {"kind": "pick", "instrument": "p300"},
        {"kind": "aspirate", "instrument": "p300", "labware": "sample on 1", "well": "A1"},
        {"kind": "aspirate", "instrument": "p300", "labware": "sample on 1", "well": "B1"},
        {"kind": "drop", "instrument": "p300"},
    ]
    assert runner._cross_well_aspiration_risks(events) == [{
        "tip_number": 2, "instrument": "p300", "labware": "sample on 1",
        "distinct_wells": 2, "example_wells": ["A1", "B1"],
    }]


def test_pinned_runlog_requires_file_except_for_rna_stdout_transport(tmp_path):
    output = tmp_path / "result.json"
    stdout_payload = {"ok": True, "events": [{"kind": "pick"}]}
    file_payload = {"ok": True, "events": [{"kind": "drop"}]}
    with pytest.raises(ValueError, match="required result file"):
        runner._runlog_payload(output, json.dumps(stdout_payload))
    assert runner._runlog_payload(output, json.dumps(stdout_payload),
                                  allow_stdout=True) == stdout_payload
    output.write_text(json.dumps(file_payload), encoding="utf-8")
    assert runner._runlog_payload(output, json.dumps(stdout_payload)) == file_payload
