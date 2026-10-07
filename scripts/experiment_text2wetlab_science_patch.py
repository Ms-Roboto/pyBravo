"""Try bounded, source-grounded Qwen line repairs on a simulated OT-2 draft.

This experiment writes only beneath an output directory outside the repository.
It does not edit the input protocol or award a Text2WetLab score. Run it after
the normal generator has made a simulator-passing but scientifically incomplete
candidate, then review any improved candidate before using it elsewhere.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pybravo.evals.text2wetlab.adapter import ProtocolValidationError, validate_ot2_source
from pybravo.evals.text2wetlab.patch_repair import (
    LINE_EDIT_SCHEMA,
    PatchError,
    apply_line_patch,
    line_patch_messages,
)
from pybravo.evals.text2wetlab.rubric_audit import _transfers
from pybravo.evals.text2wetlab.source_context import prepare_scientific_source
from pybravo.workflow.protocols.llm import LocalLLMConfig, structured_json

try:
    from scripts import evaluate_text2wetlab as runner
except ModuleNotFoundError:
    # `python scripts/experiment_...py` puts scripts/, rather than the repo
    # root, on sys.path. Editable installs still provide pybravo itself.
    import evaluate_text2wetlab as runner


def _sha(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _failed_checks(audit: dict[str, Any]) -> int:
    return sum(check["status"] == "failed" for item in audit["items"]
               for check in item["checks"])


def _resource_calls(source: str) -> list[str]:
    """Keep the task-fixed labware, module and pipette declarations verbatim."""
    tree = ast.parse(source)
    fixed_methods = {"load_labware", "load_module", "load_instrument", "load_adapter"}
    return [ast.dump(node, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in fixed_methods]


def _transfer_mapping(task: str, events: list[dict]) -> list[tuple[str, str, str, str]]:
    return [(transfer.source_labware, transfer.source_well,
             transfer.destination_labware, transfer.destination_well)
            for transfer in _transfers(task, events)]


def _subsequence(earlier: list[tuple], later: list[tuple]) -> bool:
    cursor = iter(later)
    return all(any(candidate == item for candidate in cursor) for item in earlier)


def _preserve_fixed_facts(task: str, baseline_source: str, baseline_events: list[dict],
                          candidate_source: str, candidate_events: list[dict],
                          baseline_labware: dict, candidate_labware: dict) -> str | None:
    if _resource_calls(candidate_source) != _resource_calls(baseline_source):
        return "The proposal changed a fixed labware, deck, module, or pipette declaration."
    if candidate_labware != baseline_labware:
        return "The proposal changed loaded labware or deck bindings."
    original = _transfer_mapping(task, baseline_events)
    proposed = _transfer_mapping(task, candidate_events)
    if not original or not proposed or not _subsequence(original, proposed):
        return "The proposal removed or reordered an existing source-to-destination mapping."
    return None


def science_patch_messages(source: str, *, instruction: str, scientific_source: str | None,
                           diagnostic: str) -> list[dict[str, str]]:
    """Reuse the exact line-patch format with evidence-based quantity freedom."""
    messages = line_patch_messages(source, instruction=instruction,
                                   diagnostic=diagnostic, scientific_source=scientific_source)
    messages[0]["content"] = (
        "Repair the OT-2 Python protocol using only the supplied task and scientific "
        "source. Return JSON with only an edits array. Each edit has 1-based "
        "start_line, inclusive end_line, and replacement text. To insert before "
        "line N, use start_line=N and end_line=N-1. Make no more than eight "
        "localized edits; do not rewrite the whole protocol. Preserve the task's "
        "fixed labware, deck slots, pipettes, tip racks, primer/template mapping, "
        "assembly mapping, and contamination policy. Correct liquid quantities "
        "and add a missing stage only when the supplied task or paper supports it. "
        "Recalculate every reaction total from its actual components. Do not "
        "invent a stock, pretreatment, predilution, or manual action to conceal "
        "an out-of-range liquid stroke. Preserve explicit off-deck handoffs. "
        "Do not mention benchmark grading, reference solutions, or simulation-only "
        "branches in the protocol. The proposal will face static, pinned lint, "
        "simulator, event, deck/mapping, and independent scientific checks. "
        "Do not return a complete protocol or Markdown."
    )
    return messages


def _pinned_sources(task: str, dataset_root: Path | None,
                    paper_override: Path | None) -> tuple[str, str | None]:
    raw_instruction = runner._source_bytes(task, "instruction.md", dataset_root)
    if raw_instruction is None:
        raise ValueError("Pinned task instruction is unavailable")
    paper = runner._source_bytes(task, "environment/data/paper.txt", dataset_root)
    if paper_override is not None:
        replacement = paper_override.read_bytes()
        expected = (runner.ECOLI_PAPER_SHA256 if task == "ecoli-heat-shock-transformation"
                    else _sha(paper) if paper is not None else None)
        if expected is None or _sha(replacement) != expected:
            raise ValueError("Paper override does not match the pinned source digest")
        paper = replacement
    if b"/data/paper.txt" in raw_instruction and paper is None:
        raise ValueError("The task requires a pinned scientific paper that is unavailable")
    return raw_instruction.decode("utf-8"), paper.decode("utf-8") if paper else None


def _check_candidate(task: str, source: str, path: Path, task_dir: Path,
                     simulator: Path, dataset_root: Path | None,
                     labware_dir: Path | None, instruction: str) -> dict[str, Any]:
    """Execute only after static validation and the pinned task lint pass."""
    result: dict[str, Any] = {"source_sha256": _sha(source)}
    try:
        validate_ot2_source(source)
    except ProtocolValidationError as exc:
        return {**result, "status": "static_rejected", "detail": str(exc)}
    lint = runner._official_lint(task, path, task_dir, dataset_root)
    result["pinned_lint"] = lint
    if lint["status"] not in {"passed", "not_applicable"}:
        return {**result, "status": "lint_rejected"}
    command = [str(simulator)]
    if labware_dir is not None:
        command.extend(["-L", str(labware_dir)])
    command.append(str(path))
    simulation = runner._run(command, timeout=600)
    result["ot2_simulator_gate"] = simulation
    if simulation["status"] != "passed":
        return {**result, "status": "simulator_rejected"}
    runlog = runner._official_runlog(task, path, task_dir, simulator, dataset_root,
                                     labware_dir, instruction)
    result["pinned_runlog"] = runlog
    if runlog["status"] != "passed":
        return {**result, "status": "runlog_rejected"}
    if runlog["adapter_event_validation"]["status"] != "passed":
        return {**result, "status": "event_safety_rejected"}
    if runlog.get("cross_well_aspiration_risk_count", 0):
        return {**result, "status": "cross_well_review_required"}
    result["local_rubric_audit"] = runlog["local_rubric_audit"]
    result["status"] = "mechanical_gates_passed"
    return result


async def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir.expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if output == repository or repository in output.parents:
        raise ValueError("Experiment output must be outside the pyBravo repository")
    if args.source.resolve() == output / "original_candidate.py":
        raise ValueError("Experiment output would overwrite the input protocol")
    output.mkdir(parents=True, exist_ok=True)
    instruction, paper = _pinned_sources(args.task, args.dataset_root, args.paper_override)
    excerpt = prepare_scientific_source(paper, task_instruction=instruction) if paper else None
    source = args.source.read_text(encoding="utf-8")
    (output / "original_candidate.py").write_text(source, encoding="utf-8")
    (output / "instruction.md").write_text(instruction, encoding="utf-8")
    if paper is not None:
        (output / "scientific_source.txt").write_text(paper, encoding="utf-8")
    labware_dir = None
    if args.task == "opentrons-rna-extraction":
        labware = runner._source_bytes(args.task, runner.RNA_LABWARE, args.dataset_root)
        if labware is None:
            raise ValueError("Pinned RNA custom labware is unavailable")
        labware_dir = output / "labware"
        labware_dir.mkdir(exist_ok=True)
        (labware_dir / Path(runner.RNA_LABWARE).name).write_bytes(labware)
    trace: dict[str, Any] = {
        "dataset": runner.DATASET, "revision": runner.REVISION, "task": args.task,
        "source_path": str(args.source.resolve()), "source_sha256": _sha(source),
        "instruction_sha256": _sha(instruction), "paper_sha256": _sha(paper) if paper else None,
        "excerpt_sha256": excerpt.excerpt_sha256 if excerpt else None,
        "excerpt_line_spans": excerpt.line_spans if excerpt else (),
        "attempts": [], "official_score": None,
    }
    baseline_dir = output / "baseline"
    baseline_dir.mkdir(exist_ok=True)
    baseline = _check_candidate(args.task, source, args.source, baseline_dir, args.simulator,
                                args.dataset_root, labware_dir, instruction)
    trace["baseline"] = baseline
    if baseline["status"] != "mechanical_gates_passed":
        trace["status"] = "baseline_not_eligible"
        return trace
    baseline_audit = baseline["local_rubric_audit"]
    if not _failed_checks(baseline_audit):
        trace["status"] = "baseline_needs_review" if baseline_audit["status"] == "needs_review" else "baseline_supported_locally"
        return trace
    baseline_runlog = baseline["pinned_runlog"]
    baseline_events = json.loads(Path(baseline_runlog["events_path"]).read_text(encoding="utf-8"))
    current_source = source
    current_failure_count = _failed_checks(baseline_audit)
    # The private adapter helper reports only verbatim evidence passages to Qwen,
    # never hidden check IDs or local scores.
    from pybravo.evals.text2wetlab.adapter import EventLog, _scientific_audit
    _, diagnostic = _scientific_audit(args.task,
                                      EventLog(baseline_events, baseline_runlog["labware"]),
                                      source, instruction, excerpt.text if excerpt else None)
    assert diagnostic is not None
    config = replace(LocalLLMConfig.from_env(), max_tokens=args.model_max_tokens,
                     timeout_s=args.model_timeout, retries=0, enable_thinking=False)
    if config.base_url != "http://sparky.local:8000/v1":
        raise ValueError("This experiment is restricted to the local Qwen at sparky.local:8000")
    for number in range(1, args.max_attempts + 1):
        attempt_dir = output / f"attempt_{number}"
        attempt_dir.mkdir(exist_ok=True)
        attempt: dict[str, Any] = {"number": number, "input_sha256": _sha(current_source),
                                   "source_grounded_diagnostic": diagnostic}
        trace["attempts"].append(attempt)
        try:
            response = await structured_json(
                science_patch_messages(current_source, instruction=instruction,
                                       scientific_source=excerpt.text if excerpt else None,
                                       diagnostic=diagnostic),
                LINE_EDIT_SCHEMA, config=config, schema_name="ot2_science_line_repair",
            )
        except Exception as exc:
            attempt["status"] = "model_failed"
            attempt["detail"] = f"{type(exc).__name__}: {exc}"
            break
        attempt["model"] = response.metadata.get("model")
        attempt["model_elapsed_s"] = response.metadata.get("elapsed_s")
        attempt["model_usage"] = response.metadata.get("usage")
        (attempt_dir / "qwen_patch.json").write_text(
            json.dumps(response.payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        try:
            candidate = apply_line_patch(current_source, response.payload)
        except (PatchError, SyntaxError) as exc:
            attempt["status"] = "patch_rejected"
            attempt["detail"] = str(exc)
            continue
        candidate_path = attempt_dir / "candidate.py"
        candidate_path.write_text(candidate, encoding="utf-8")
        attempt["candidate_path"] = str(candidate_path)
        gates = _check_candidate(args.task, candidate, candidate_path, attempt_dir,
                                 args.simulator, args.dataset_root, labware_dir, instruction)
        attempt["gates"] = gates
        if gates["status"] != "mechanical_gates_passed":
            attempt["status"] = gates["status"]
            # A static or simulator failure is actionable without exposing
            # hidden rubric details. The next edit still uses the same source.
            diagnostic = str(gates.get("detail") or gates.get("ot2_simulator_gate", {}).get("stderr_tail")
                             or gates.get("pinned_runlog", {}).get("error") or diagnostic)
            continue
        candidate_runlog = gates["pinned_runlog"]
        candidate_events = json.loads(Path(candidate_runlog["events_path"]).read_text(encoding="utf-8"))
        mapping_issue = _preserve_fixed_facts(
            args.task, source, baseline_events, candidate, candidate_events,
            baseline_runlog["labware"], candidate_runlog["labware"],
        )
        if mapping_issue is not None:
            attempt["status"] = "fixed_facts_rejected"
            attempt["detail"] = mapping_issue
            diagnostic = mapping_issue
            continue
        audit = gates["local_rubric_audit"]
        failure_count = _failed_checks(audit)
        attempt["failed_observable_checks"] = failure_count
        if failure_count < current_failure_count:
            trace["best_candidate"] = str(candidate_path)
            trace["best_candidate_sha256"] = _sha(candidate)
            trace["best_failed_observable_checks"] = failure_count
            current_source = candidate
            current_failure_count = failure_count
        if failure_count:
            attempt["status"] = "scientific_gaps_remain"
            _, grounded = _scientific_audit(
                args.task, EventLog(candidate_events, candidate_runlog["labware"]),
                candidate, instruction, excerpt.text if excerpt else None,
            )
            if grounded is not None:
                diagnostic = grounded
            continue
        if audit["status"] == "needs_review":
            attempt["status"] = "scientist_review_required"
            trace["status"] = "improved_candidate_needs_review"
            return trace
        attempt["status"] = "supported_by_local_checks"
        trace["status"] = "improved_candidate_supported_locally"
        return trace
    trace["status"] = "bounded_without_full_local_coverage"
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=runner.TASKS, required=True)
    parser.add_argument("--source", type=Path, required=True,
                        help="Existing simulator-passing, Qwen-authored protocol.py")
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--paper-override", type=Path)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--model-timeout", type=int, default=300)
    parser.add_argument("--model-max-tokens", type=int, default=4096)
    args = parser.parse_args()
    if (not args.source.is_file() or not args.simulator.is_file()
            or not 1 <= args.max_attempts <= 3 or not 1 <= args.model_timeout <= 300
            or not 512 <= args.model_max_tokens <= 8192):
        parser.error("Provide source/simulator files, 1–3 attempts, 1–300 s timeout, and 512–8192 tokens")
    trace = asyncio.run(run_experiment(args))
    output = args.output_dir.expanduser().resolve()
    (output / "science_patch_trace.json").write_text(
        json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    print(json.dumps({"trace": str(output / "science_patch_trace.json"),
                      "status": trace["status"], "official_score": None}))
    return 0 if trace["status"] == "improved_candidate_supported_locally" else 1


if __name__ == "__main__":
    raise SystemExit(main())
