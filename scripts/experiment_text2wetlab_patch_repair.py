"""Bounded local-model experiment for focused OT-2 protocol repair.

The saved candidate, task instruction, and simulator diagnostic are the only
inputs. The local model authors line edits; this script applies them without
hand-written protocol changes and requires all ordinary safety gates.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from pybravo.evals.text2wetlab.adapter import (
    ProtocolValidationError,
    record_simulation_events,
    simulate_protocol,
    validate_event_safety,
    validate_ot2_source,
)
from pybravo.evals.text2wetlab.patch_repair import (
    LINE_EDIT_SCHEMA,
    PatchError,
    apply_line_patch,
    line_patch_messages,
    preserve_existing_task_actions,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, structured_json


def _digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def run_experiment(args: argparse.Namespace) -> dict:
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = args.source.read_text(encoding="utf-8")
    instruction = args.instruction.read_text(encoding="utf-8")
    paper = args.paper.read_text(encoding="utf-8") if args.paper else None
    validate_ot2_source(source)
    base_simulation = simulate_protocol(args.source, simulator_command=args.simulator)
    if base_simulation.status != "passed":
        raise ValueError(f"Saved candidate must already pass Opentrons simulation: {base_simulation.detail}")
    baseline = record_simulation_events(
        args.source, event_logger_path=args.event_logger,
        simulator_command=args.simulator,
    )
    baseline_validation = validate_event_safety(baseline, instruction=instruction)
    trace = {
        "status": "running",
        "source_path": str(args.source.resolve()),
        "source_sha256": _digest(source),
        "instruction_sha256": _digest(instruction),
        "paper_sha256": _digest(paper) if paper else None,
        "baseline_event_validation": {
            "status": baseline_validation.status,
            "detail": baseline_validation.detail,
        },
        "attempts": [],
        "official_score": None,
    }
    if baseline_validation.status == "passed":
        trace["status"] = "already_passed"
        return trace
    current_source = source
    diagnostic = baseline_validation.detail
    config = replace(LocalLLMConfig.from_env(), max_tokens=2048,
                     timeout_s=args.model_timeout, retries=0, enable_thinking=False)
    for number in range(1, args.max_attempts + 1):
        attempt = {"number": number, "input_source_sha256": _digest(current_source),
                   "input_diagnostic": diagnostic}
        trace["attempts"].append(attempt)
        try:
            response = await structured_json(
                line_patch_messages(current_source, instruction=instruction,
                                    diagnostic=diagnostic, scientific_source=paper),
                LINE_EDIT_SCHEMA, config=config, schema_name="ot2_line_repair",
            )
        except Exception as exc:
            attempt["status"] = "model_failed"
            attempt["diagnostic"] = f"{type(exc).__name__}: {exc}"
            trace["status"] = "failed_model"
            break
        patch = response.payload
        patch_path = output / f"patch_attempt_{number}.json"
        patch_path.write_text(json.dumps(patch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        attempt.update({
            "patch_path": str(patch_path),
            "model_elapsed_s": response.metadata.get("elapsed_s"),
            "model_usage": response.metadata.get("usage"),
        })
        try:
            patched = apply_line_patch(current_source, patch)
            validate_ot2_source(patched)
        except (PatchError, ProtocolValidationError) as exc:
            diagnostic = f"Proposed patch is invalid: {exc}"
            attempt["status"] = "static_rejected"
            attempt["diagnostic"] = diagnostic
            continue
        candidate_path = output / f"candidate_patch_{number}.py"
        candidate_path.write_text(patched, encoding="utf-8")
        attempt["candidate_path"] = str(candidate_path)
        attempt["candidate_sha256"] = _digest(patched)
        simulation = simulate_protocol(candidate_path, simulator_command=args.simulator)
        attempt["simulation"] = simulation.status
        if simulation.status != "passed":
            diagnostic = simulation.detail
            attempt["status"] = "simulation_rejected"
            attempt["diagnostic"] = diagnostic
            continue
        try:
            event_log = record_simulation_events(
                candidate_path, event_logger_path=args.event_logger,
                simulator_command=args.simulator,
            )
        except Exception as exc:
            diagnostic = f"Structured event logger failed: {type(exc).__name__}: {exc}"
            attempt["status"] = "event_logger_rejected"
            attempt["diagnostic"] = diagnostic
            continue
        invariant_error = preserve_existing_task_actions(baseline, event_log)
        if invariant_error:
            diagnostic = invariant_error
            attempt["status"] = "task_facts_rejected"
            attempt["diagnostic"] = invariant_error
            continue
        validation = validate_event_safety(event_log, instruction=instruction)
        attempt["event_validation"] = validation.status
        attempt["event_detail"] = validation.detail
        if validation.status == "passed":
            trace["status"] = "passed_local_gates"
            trace["accepted_candidate"] = str(candidate_path)
            trace["accepted_sha256"] = _digest(patched)
            break
        current_source = patched
        diagnostic = validation.detail
        attempt["status"] = "event_rejected"
    if trace["status"] == "running":
        trace["status"] = "failed_bounded"
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--instruction", required=True, type=Path)
    parser.add_argument("--paper", type=Path)
    parser.add_argument("--event-logger", required=True, type=Path)
    parser.add_argument("--simulator", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--model-timeout", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 3 or not 1 <= args.model_timeout <= 300:
        parser.error("Use 1–3 patch attempts and a 1–300 second model timeout.")
    trace = asyncio.run(run_experiment(args))
    trace_path = args.output_dir / "patch_trace.json"
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"trace": str(trace_path), "status": trace["status"],
                      "official_score": None}))
    return 0 if trace["status"] in {"passed_local_gates", "already_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
