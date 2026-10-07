"""Run a pinned Text2WetLab OT-2 simulator smoke test for the local drafter.

This is deliberately *not* the official Harbor score. Harbor additionally runs
its own lint, anti-hack checks, and a rubric judge on the simulator log. The
dataset targets an Opentrons OT-2, while pyBravo's production target is Bravo.

Example:
    python scripts/evaluate_text2wetlab.py \
      --task split-200ul-two-wells --task a1-a12-100ul \
      --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
      --output-dir /tmp/pybravo-text2wetlab-baseline
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

DATASET = "EvanOLeary/Text2WetLab"
REVISION = "d7c8a9b93428997447eeaf2ee9e27ac3ce026872"
TASKS = (
    "a1-a12-100ul",
    "ampure-bead-cleanup",
    "colony-pcr-screening",
    "ecoli-heat-shock-transformation",
    "golden-gate-assembly",
    "opentrons-rna-extraction",
    "split-200ul-two-wells",
)
RNA_LABWARE = "environment/data/labware/thermo_96_wellplate_200ul.json"
ECOLI_PAPER_SHA256 = "768a5703630e593a3eb5be8cac6267af0cf26ba1b43dbc797d24b08cbaf3f18a"


def _source_path(task: str, name: str) -> str:
    if task not in TASKS or name not in {
        "instruction.md", "environment/data/paper.txt", "tests/protocol_lint.py",
        "tests/runlog.py", RNA_LABWARE,
    }:
        raise ValueError("Unknown benchmark task or source file")
    return f"tasks/harbor/{task}/{name}"


def _source_bytes(task: str, name: str, dataset_root: Path | None) -> bytes | None:
    source = _source_path(task, name)
    if dataset_root is not None:
        path = dataset_root / source
        return path.read_bytes() if path.is_file() else None
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{quote(source, safe='/')}"
    try:
        with urlopen(url, timeout=30) as response:
            return response.read()
    except OSError as exc:
        if name == "environment/data/paper.txt" or (task == "opentrons-rna-extraction"
                                                     and name == "tests/protocol_lint.py"
                                                     and isinstance(exc, HTTPError)
                                                     and exc.code == 404) or (name == RNA_LABWARE
                                                                              and isinstance(exc, HTTPError)
                                                                              and exc.code == 404):
            return None
        raise RuntimeError(f"Could not fetch pinned benchmark instruction: {url}") from exc


def _local_env() -> dict[str, str]:
    env = dict(os.environ)
    # The generation path must remain local even on hosts with cloud keys.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    return env


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(command: list[str], *, timeout: int) -> dict:
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, env=_local_env(), check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "timeout", "timeout_seconds": timeout,
                "stdout_tail": (exc.stdout or b"")[-1500:].decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-1500:],
                "stderr_tail": (exc.stderr or b"")[-1500:].decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-1500:]}
    return {"status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-1500:], "stderr_tail": completed.stderr[-1500:]}


def _official_lint(task: str, protocol: Path, task_dir: Path, dataset_root: Path | None) -> dict:
    """Apply the pinned task's own AST lint before another simulator run."""
    source = _source_bytes(task, "tests/protocol_lint.py", dataset_root)
    if source is None:
        return {"status": "not_applicable" if task == "opentrons-rna-extraction" else "unavailable",
                "violations": []}
    lint_file = task_dir / "official_protocol_lint.py"
    lint_file.write_bytes(source)
    code = ("import json, runpy, sys; "
            "check = runpy.run_path(sys.argv[1])['violations']; "
            "print(json.dumps(check(open(sys.argv[2], encoding='utf-8').read())))")
    completed = subprocess.run([sys.executable, "-I", "-c", code, str(lint_file), str(protocol)],
                               capture_output=True, text=True, timeout=10, env=_local_env(), check=False)
    try:
        violations = json.loads(completed.stdout)
        if not isinstance(violations, list) or not all(isinstance(item, str) for item in violations):
            raise ValueError("lint did not return a string array")
    except ValueError:
        return {"status": "error", "source_sha256": _digest(source),
                "detail": (completed.stderr or completed.stdout)[-1500:]}
    return {"status": "passed" if completed.returncode == 0 and not violations else "failed",
            "source_sha256": _digest(source), "violations": violations}


def _cross_well_aspiration_risks(events: list[dict]) -> list[dict]:
    """Flag a tip aspirating from distinct wells on one plate before its drop.

    This is a conservative review signal, not a contamination verdict: serial
    dilution may intentionally carry liquid forward. AMPure sample cleanup does
    not permit this pattern across different samples.
    """
    active: dict[str, dict] = {}
    findings: list[dict] = []
    tip_number = 0

    def finish(instrument: str) -> None:
        cycle = active.pop(instrument, None)
        if cycle is None:
            return
        for labware, wells in cycle["aspirate_wells"].items():
            if len(wells) > 1:
                findings.append({"tip_number": cycle["tip_number"], "instrument": instrument,
                                 "labware": labware, "distinct_wells": len(wells),
                                 "example_wells": sorted(wells)[:8]})

    for event in events:
        if not isinstance(event, dict):
            continue
        instrument = str(event.get("instrument") or "")
        kind = event.get("kind")
        if kind == "pick":
            finish(instrument)
            tip_number += 1
            active[instrument] = {"tip_number": tip_number, "aspirate_wells": {}}
        elif kind == "aspirate" and instrument in active:
            labware = str(event.get("labware") or "")
            well = str(event.get("well") or "")
            if labware and well:
                active[instrument]["aspirate_wells"].setdefault(labware, set()).add(well)
        elif kind == "drop":
            finish(instrument)
    for instrument in list(active):
        finish(instrument)
    return findings


def _runlog_payload(output: Path, stdout: str, *, allow_stdout: bool = False) -> dict:
    """Require file evidence except for the pinned RNA stdout-only logger."""
    if not output.is_file() and not allow_stdout:
        raise ValueError("Pinned runlog did not write its required result file")
    raw = output.read_text(encoding="utf-8") if output.is_file() else stdout
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("runlog response is not a JSON object")
    return payload


def _official_runlog(task: str, protocol: Path, task_dir: Path, simulator: Path,
                     dataset_root: Path | None, labware_dir: Path | None = None,
                     instruction: str | None = None) -> dict:
    """Use the task's pinned runlog gate and preserve its action events for review."""
    from pybravo.evals.text2wetlab.adapter import record_simulation_events, validate_event_safety
    from pybravo.evals.text2wetlab.rubric_audit import audit_rubric_coverage

    source = _source_bytes(task, "tests/runlog.py", dataset_root)
    if source is None:
        return {"status": "unavailable"}
    python = simulator.with_name("python")
    if not python.is_file():
        return {"status": "unavailable", "detail": "Simulator environment has no Python executable"}
    runlog_file = task_dir / "official_runlog.py"
    runlog_file.write_bytes(source)
    contact_labware_dir = labware_dir
    with tempfile.TemporaryDirectory(prefix="text2wetlab-runlog-") as temp:
        temp_dir = Path(temp)
        if labware_dir is None:
            labware_dir = temp_dir / "labware"
            labware_dir.mkdir()
        output = temp_dir / "result.json"
        command = [str(python), str(runlog_file), str(protocol), str(labware_dir), str(output)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                       env=_local_env(), check=False)
            payload = _runlog_payload(output, completed.stdout,
                                      allow_stdout=task == "opentrons-rna-extraction")
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            return {"status": "error", "source_sha256": _digest(source),
                    "detail": f"{type(exc).__name__}: {exc}"[-1500:]}
    if completed.returncode != 0 or payload.get("ok") is not True:
        return {"status": "failed", "source_sha256": _digest(source),
                "error": str(payload.get("error") or completed.stderr)[-1500:]}
    events = payload.get("events")
    if not isinstance(events, list):
        return {"status": "error", "source_sha256": _digest(source),
                "detail": "runlog did not return an events array"}
    events_file = task_dir / "official_events.json"
    events_file.write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
    # The pinned log is the scoring evidence. Run a separate trusted wrapper
    # around that same logger to retain dispense-height evidence omitted by the
    # pinned JSON format. Never substitute its output for the pinned log.
    try:
        annotated = record_simulation_events(
            protocol, event_logger_path=runlog_file, simulator_command=simulator,
            labware_dir=contact_labware_dir, timeout_s=600,
        )
    except Exception as exc:
        return {"status": "error", "source_sha256": _digest(source),
                "detail": f"Contact evidence unavailable: {type(exc).__name__}: {exc}"[-1500:]}
    clean_events = [{key: value for key, value in event.items()
                     if key != "at_or_above_well_rim"} for event in annotated.events]
    if clean_events != events or annotated.labware != (payload.get("labware") or {}):
        return {"status": "error", "source_sha256": _digest(source),
                "detail": "Contact wrapper changed the pinned runlog events or labware."}
    annotated_file = task_dir / "contact_evidence_events.json"
    annotated_file.write_text(json.dumps(annotated.events, indent=2) + "\n", encoding="utf-8")
    counts = Counter(str(event.get("kind")) for event in events if isinstance(event, dict))
    risks = _cross_well_aspiration_risks(events)
    semantic = validate_event_safety(annotated, instruction=instruction)
    rubric_audit = audit_rubric_coverage(task, events, protocol.read_text(encoding="utf-8"),
                                         payload.get("labware") or {})
    return {"status": "passed", "source_sha256": _digest(source),
            "events_path": str(events_file), "event_count": len(events),
            "contact_evidence_events_path": str(annotated_file),
            "event_kinds": dict(sorted(counts.items())),
            "cross_well_aspiration_risk_count": len(risks),
            "cross_well_aspiration_risks": risks[:10],
            "adapter_event_validation": {"status": semantic.status, "detail": semantic.detail,
                                         "event_count": semantic.event_count},
            "local_rubric_audit": rubric_audit,
            "labware": payload.get("labware") or {}}


def _evaluate_candidate(task: str, protocol: Path, task_dir: Path, simulator: Path,
                        dataset_root: Path | None, instruction: bytes,
                        labware_dir: Path | None, record: dict) -> dict:
    """Apply the current pinned local gates to a validated candidate."""
    record["protocol_sha256"] = _digest(protocol.read_bytes())
    record["official_protocol_lint"] = _official_lint(task, protocol, task_dir, dataset_root)
    if record["official_protocol_lint"]["status"] not in {"passed", "not_applicable"}:
        record["status"] = "official_lint_failed_or_unavailable"
        return record
    simulator_command = [str(simulator)]
    if labware_dir:
        simulator_command.extend(["-L", str(labware_dir)])
    simulator_command.append(str(protocol))
    record["ot2_simulator_gate"] = _run(simulator_command, timeout=600)
    if record["ot2_simulator_gate"]["status"] != "passed":
        record["status"] = "simulator_failed"
        return record
    record["official_runlog_gate"] = _official_runlog(task, protocol, task_dir, simulator,
                                                       dataset_root, labware_dir,
                                                       instruction.decode("utf-8"))
    runlog = record["official_runlog_gate"]
    if runlog["status"] != "passed":
        record["status"] = "official_runlog_failed_or_unavailable"
    elif (runlog.get("adapter_event_validation") or {}).get("status") != "passed":
        record["status"] = "simulator_passed_semantic_review_required"
    elif runlog.get("cross_well_aspiration_risk_count", 0):
        record["status"] = "simulator_passed_semantic_review_required"
    elif (runlog.get("local_rubric_audit") or {}).get("status") not in {"supported", "needs_review"}:
        record["status"] = "local_rubric_failed_or_unavailable"
    else:
        record["status"] = "simulator_passed"
    return record


def _saved_candidate_provenance(task: str, saved_run_root: Path, instruction: bytes,
                                source_paper: bytes | None) -> dict:
    """Check a prior Qwen trace and report before executing their saved code."""
    root = saved_run_root.expanduser().resolve()
    task_dir = root / task
    protocol = task_dir / "protocol.py"
    trace_path = task_dir / "generation_trace.json"
    report_path = root / "report.json"

    def failed(detail: str) -> dict:
        return {"status": "failed", "detail": detail, "saved_run_root": str(root)}

    try:
        report_bytes = report_path.read_bytes()
        trace_bytes = trace_path.read_bytes()
        report = json.loads(report_bytes)
        trace = json.loads(trace_bytes)
        candidate_bytes = protocol.read_bytes()
        saved_instruction = (task_dir / "instruction.md").read_bytes()
    except (OSError, ValueError) as exc:
        return failed(f"Saved report, trace, protocol, or instruction is missing or invalid: {exc}")
    if not isinstance(report, dict) or not isinstance(trace, dict):
        return failed("Saved report and generation trace must be JSON objects")
    if report.get("dataset") != DATASET or report.get("revision") != REVISION:
        return failed("Saved report is not for the pinned Text2WetLab revision")
    cases = report.get("cases")
    if not isinstance(cases, list):
        return failed("Saved report has no task cases")
    matches = [case for case in cases if isinstance(case, dict) and case.get("task") == task]
    if len(matches) != 1:
        return failed("Saved report must contain exactly one case for this task")
    case = matches[0]
    instruction_sha256 = _digest(instruction)
    if (_digest(saved_instruction) != instruction_sha256
            or case.get("instruction_sha256") != instruction_sha256
            or trace.get("task_sha256") != instruction_sha256):
        return failed("Saved instruction, report, or generation trace differs from the pinned instruction")
    prior_generation = case.get("generation")
    if not isinstance(prior_generation, dict) or prior_generation.get("status") != "passed":
        return failed("Saved report does not record a successful prior generation")
    expected_paper_sha256 = (_digest(source_paper) if source_paper is not None else
                             ECOLI_PAPER_SHA256 if task == "ecoli-heat-shock-transformation" else None)
    if case.get("source_paper_sha256") != expected_paper_sha256:
        return failed("Saved paper digest differs from the pinned source")
    scientific_source = trace.get("scientific_source")
    if scientific_source is not None and (not isinstance(scientific_source, dict)
                                          or scientific_source.get("source_sha256") != expected_paper_sha256):
        return failed("Generation trace scientific source digest differs from the pinned paper")
    candidate_sha256 = _digest(candidate_bytes)
    if (not re.fullmatch(r"[0-9a-f]{64}", str(case.get("protocol_sha256")))
            or case["protocol_sha256"] != candidate_sha256):
        return failed("Saved protocol differs from the candidate digest in the prior report")
    if (trace.get("status") != "simulated" or trace.get("static_validation_passed") is not True
            or ("event_validation_passed" in trace
                and trace["event_validation_passed"] is not True)):
        return failed("Generation trace does not confirm the accepted candidate's safety gates")
    attempts = trace.get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return failed("Generation trace has no accepted model attempt")
    attempt = attempts[-1]
    model = attempt.get("model")
    attempt_number = attempt.get("number")
    if (not isinstance(model, str) or "qwen" not in model.casefold()
            or attempt.get("validation") != "passed"
            or not isinstance(attempt_number, int) or attempt_number != len(attempts)):
        return failed("Accepted trace attempt is not a numbered, validated Qwen candidate")
    accepted_patch = attempt.get("accepted_via_patch") is True
    if accepted_patch:
        patches = attempt.get("patches")
        if not isinstance(patches, list) or not patches or not isinstance(patches[-1], dict):
            return failed("Accepted patch is missing from the generation trace")
        patch = patches[-1]
        patch_model = patch.get("model")
        prior_patch_hashes = [attempt.get("code_sha256")]
        prior_patch_hashes.extend(item.get("code_sha256") for item in patches[:-1]
                                  if isinstance(item, dict))
        if (patch.get("status") != "accepted" or patch.get("simulation") != "passed"
                or patch.get("event_validation") != "passed"
                or patch.get("input_code_sha256") not in prior_patch_hashes
                or not isinstance(patch_model, str) or "qwen" not in patch_model.casefold()
                or patch.get("code_sha256") != candidate_sha256):
            return failed("Accepted Qwen patch does not match the saved candidate")
        patch_number = patch.get("number")
        if not isinstance(patch_number, int) or patch_number != len(patches):
            return failed("Accepted patch has no numbered attempt")
        base_candidate = task_dir / f"candidate_attempt_{attempt_number}.py"
        if (not base_candidate.is_file()
                or _digest(base_candidate.read_bytes()) != attempt.get("code_sha256")):
            return failed("Original Qwen draft differs from its generation trace")
        patch_candidate = task_dir / f"candidate_attempt_{attempt_number}_patch_{patch_number}.py"
        if not patch_candidate.is_file() or _digest(patch_candidate.read_bytes()) != candidate_sha256:
            return failed("Accepted patch source file differs from the saved candidate")
    elif attempt.get("simulation") != "passed" or attempt.get("code_sha256") != candidate_sha256:
        return failed("Accepted Qwen attempt does not match the saved candidate")
    return {"status": "passed", "saved_run_root": str(root),
            "saved_report_path": str(report_path), "saved_report_sha256": _digest(report_bytes),
            "generation_trace_path": str(trace_path), "generation_trace_sha256": _digest(trace_bytes),
            "candidate_path": str(protocol), "candidate_sha256": candidate_sha256,
            "instruction_sha256": instruction_sha256,
            "source_paper_sha256": expected_paper_sha256,
            "model": model, "attempt_number": attempt.get("number"),
            "accepted_via_patch": accepted_patch,
            "prior_generation_status": "passed"}


def recheck_task(task: str, *, saved_run_root: Path, output_dir: Path, simulator: Path,
                 dataset_root: Path | None = None) -> dict:
    """Recheck a saved Qwen candidate without calling a model or writing protocol code."""
    if task not in TASKS:
        raise ValueError(f"Unknown Text2WetLab task: {task}")
    saved_root = saved_run_root.expanduser().resolve()
    target_root = output_dir.expanduser().resolve()
    if saved_root == target_root or saved_root in target_root.parents or target_root in saved_root.parents:
        raise ValueError("Recheck output must be separate from the saved candidate run")
    instruction = _source_bytes(task, "instruction.md", dataset_root)
    assert instruction is not None
    source_paper = _source_bytes(task, "environment/data/paper.txt", dataset_root)
    provenance = _saved_candidate_provenance(task, saved_root, instruction, source_paper)
    record = {"task": task, "instruction_sha256": _digest(instruction),
              "source_paper_sha256": _digest(source_paper) if source_paper is not None else
              ECOLI_PAPER_SHA256 if task == "ecoli-heat-shock-transformation" else None,
              "generation": None, "saved_candidate_provenance": provenance,
              "current_static_validation": None, "official_protocol_lint": None,
              "ot2_simulator_gate": None, "official_runlog_gate": None,
              "official_score": None}
    if provenance["status"] != "passed":
        record["status"] = "saved_candidate_provenance_unconfirmed"
        return record
    from pybravo.evals.text2wetlab.adapter import ProtocolValidationError, validate_ot2_source

    protocol = saved_root / task / "protocol.py"
    try:
        validate_ot2_source(protocol.read_text(encoding="utf-8"))
    except (UnicodeError, ProtocolValidationError) as exc:
        record["current_static_validation"] = {"status": "failed", "detail": str(exc)}
        record["status"] = "current_static_validation_failed"
        return record
    record["current_static_validation"] = {"status": "passed"}
    task_dir = target_root / task
    task_dir.mkdir(parents=True, exist_ok=True)
    labware_dir = None
    if task == "opentrons-rna-extraction":
        custom_labware = _source_bytes(task, RNA_LABWARE, dataset_root)
        if custom_labware is None:
            record["status"] = "blocked_missing_custom_labware"
            return record
        labware_dir = task_dir / "labware"
        labware_dir.mkdir(exist_ok=True)
        (labware_dir / Path(RNA_LABWARE).name).write_bytes(custom_labware)
        record["custom_labware_sha256"] = _digest(custom_labware)
    return _evaluate_candidate(task, protocol, task_dir, simulator, dataset_root,
                               instruction, labware_dir, record)


def run_task(task: str, *, output_dir: Path, simulator: Path,
             dataset_root: Path | None = None, generation_timeout: int = 1800,
             paper_override: Path | None = None, repair_attempts: int = 2,
             patch_attempts: int = 1, evidence_planning: bool = False,
             model_timeout: int = 300, max_output_tokens: int = 16_000) -> dict:
    """Generate a candidate and measure only the OT-2 simulator gate."""
    if not 0 <= repair_attempts <= 5:
        raise ValueError("repair_attempts must be between 0 and 5")
    if not 0 <= patch_attempts <= 3:
        raise ValueError("patch_attempts must be between 0 and 3")
    if not 1 <= model_timeout <= 300 or not 512 <= max_output_tokens <= 32_000:
        raise ValueError("Model timeout/tokens are outside the supported bounds")
    instruction = _source_bytes(task, "instruction.md", dataset_root)
    assert instruction is not None
    task_dir = output_dir / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_bytes(instruction)
    source_paper = _source_bytes(task, "environment/data/paper.txt", dataset_root)
    if paper_override is not None:
        override_paper = paper_override.read_bytes()
        expected_digest = (ECOLI_PAPER_SHA256 if task == "ecoli-heat-shock-transformation"
                           else _digest(source_paper) if source_paper is not None else None)
        if expected_digest is None or _digest(override_paper) != expected_digest:
            return {"task": task, "status": "paper_override_digest_mismatch",
                    "expected_paper_sha256": expected_digest, "official_score": None}
        source_paper = override_paper
    needs_paper = b"/data/paper.txt" in instruction
    record = {"task": task, "instruction_sha256": _digest(instruction),
              "source_paper_sha256": _digest(source_paper) if source_paper else None,
              "generation": None, "official_protocol_lint": None,
              "ot2_simulator_gate": None, "official_runlog_gate": None,
              "official_score": None}
    if needs_paper and source_paper is None:
        record["status"] = "blocked_missing_source_paper"
        return record
    labware_dir = None
    if task == "opentrons-rna-extraction":
        custom_labware = _source_bytes(task, RNA_LABWARE, dataset_root)
        if custom_labware is None:
            record["status"] = "blocked_missing_custom_labware"
            return record
        labware_dir = task_dir / "labware"
        labware_dir.mkdir(exist_ok=True)
        (labware_dir / Path(RNA_LABWARE).name).write_bytes(custom_labware)
        record["custom_labware_sha256"] = _digest(custom_labware)
    event_logger_source = _source_bytes(task, "tests/runlog.py", dataset_root)
    if event_logger_source is None:
        record["status"] = "blocked_missing_event_logger"
        return record
    event_logger = task_dir / "official_runlog.py"
    event_logger.write_bytes(event_logger_source)
    record["event_logger_sha256"] = _digest(event_logger_source)
    paper_file = None
    if source_paper:
        if paper_override is not None:
            paper_file = paper_override
        else:
            paper_file = task_dir / "paper.txt"
            paper_file.write_bytes(source_paper)
    command = [sys.executable, "-m", "pybravo.evals.text2wetlab",
               "--task-dir", str(task_dir), "--instruction-file", str(task_dir / "instruction.md"),
               "--simulator-command", str(simulator), "--event-logger", str(event_logger),
               "--repair-attempts", str(repair_attempts),
               "--patch-attempts", str(patch_attempts), "--rubric-task", task,
               "--model-timeout", str(model_timeout),
               "--max-output-tokens", str(max_output_tokens)]
    if paper_file:
        command.extend(["--paper-file", str(paper_file)])
    if labware_dir:
        command.extend(["--labware-dir", str(labware_dir)])
    if evidence_planning:
        command.append("--evidence-planning")
    record["generation"] = _run(command, timeout=generation_timeout)
    if record["generation"]["status"] != "passed":
        record["status"] = "generation_failed"
        return record
    # A candidate is executable code. Never run it merely because the model
    # wrote a file: the adapter must explicitly confirm its static safety gate.
    trace_path = task_dir / "generation_trace.json"
    try:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        trace = {}
    if (trace.get("status") != "simulated" or trace.get("static_validation_passed") is not True
            or trace.get("event_validation_passed") is not True):
        record["status"] = "generation_safety_gates_unconfirmed"
        return record
    protocol = task_dir / "protocol.py"
    if not protocol.is_file():
        record["status"] = "no_protocol_generated"
        return record
    return _evaluate_candidate(task, protocol, task_dir, simulator, dataset_root,
                               instruction, labware_dir, record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", choices=TASKS, required=True,
                        help="Repeat to run more tasks; no model calls are made for unselected tasks.")
    parser.add_argument("--dataset-root", type=Path,
                        help="Offline checkout of the pinned Hugging Face dataset.")
    parser.add_argument("--paper-override", action="append", default=[], metavar="TASK=PATH",
                        help="Use a locally recovered source paper after its pinned digest is checked.")
    parser.add_argument("--simulator", type=Path, required=True,
                        help="Path to opentrons_simulate 7.5.0 in an isolated environment.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--recheck-from", type=Path, metavar="SAVED_RUN_ROOT",
                        help="Recheck saved Qwen candidates and their traces without a model call. "
                             "The root must contain report.json and TASK/protocol.py.")
    parser.add_argument("--generation-timeout", type=int, default=1800)
    parser.add_argument("--model-timeout", type=int, default=300,
                        help="Per-request local Qwen timeout (1–300 seconds).")
    parser.add_argument("--max-output-tokens", type=int, default=16_000,
                        help="Maximum local Qwen response tokens (512–32000).")
    parser.add_argument("--repair-attempts", type=int, default=2,
                        help="Additional model repairs after the initial candidate (0–5).")
    parser.add_argument("--patch-attempts", type=int, default=1,
                        help="Local line-edit repairs after an event-safety failure (0–3).")
    parser.add_argument("--evidence-planning", action="store_true",
                        help="Ground and audit a local-model plan before code generation")
    args = parser.parse_args()
    if (not args.simulator.is_file() or args.generation_timeout < 1
            or not 0 <= args.repair_attempts <= 5 or not 1 <= args.model_timeout <= 300
            or not 512 <= args.max_output_tokens <= 32_000):
        parser.error("Provide a simulator, positive timeouts, 512–32000 tokens, and 0–5 repairs")
    paper_overrides: dict[str, Path] = {}
    for item in args.paper_override:
        task, sep, source = item.partition("=")
        if not sep or task not in TASKS or not source or task in paper_overrides:
            parser.error("Use each --paper-override as a unique known TASK=PATH")
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            parser.error(f"Paper override is not a file: {path}")
        paper_overrides[task] = path
    if args.recheck_from and paper_overrides:
        parser.error("--paper-override applies only to new generation runs")
    if args.recheck_from:
        saved_root = args.recheck_from.expanduser().resolve()
        target_root = args.output_dir.expanduser().resolve()
        if (saved_root == target_root or saved_root in target_root.parents
                or target_root in saved_root.parents):
            parser.error("Recheck output must be separate from the saved candidate run")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.recheck_from:
        cases = [recheck_task(task, saved_run_root=args.recheck_from,
                              output_dir=args.output_dir, simulator=args.simulator,
                              dataset_root=args.dataset_root)
                 for task in dict.fromkeys(args.task)]
    else:
        cases = [run_task(task, output_dir=args.output_dir, simulator=args.simulator,
                          dataset_root=args.dataset_root, generation_timeout=args.generation_timeout,
                          paper_override=paper_overrides.get(task), repair_attempts=args.repair_attempts,
                          patch_attempts=args.patch_attempts, evidence_planning=args.evidence_planning,
                          model_timeout=args.model_timeout, max_output_tokens=args.max_output_tokens)
                 for task in dict.fromkeys(args.task)]
    result = {"dataset": DATASET, "revision": REVISION,
              "metric": ("saved_candidate_current_local_gate_recheck" if args.recheck_from else
                         "local_static_simulator_runlog_and_event_safety_gates"),
              "official_score": None,
              "official_score_note": "Available standalone lint, the pinned runlog, OT-2 simulation, event safety, and local rubric audit are checked only for candidates that clear prior gates. Harbor anti-hack checks and rubric judge did not run.",
              "cases": cases,
              "simulator_passed": sum((c.get("ot2_simulator_gate") or {}).get("status") == "passed"
                                      and (c.get("official_runlog_gate") or {}).get("status") == "passed"
                                      for c in cases),
              "local_acceptance_passed": sum(c["status"] == "simulator_passed" for c in cases),
              "total_selected": len(cases)}
    if args.recheck_from:
        result["saved_run_root"] = str(args.recheck_from.expanduser().resolve())
    report = args.output_dir / "report.json"
    report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report), "simulator_passed": result["simulator_passed"],
                      "local_acceptance_passed": result["local_acceptance_passed"],
                      "total_selected": result["total_selected"], "official_score": None}))
    return 0 if result["local_acceptance_passed"] == result["total_selected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
