"""Experimental, staged local-Qwen ActionPlan for a pinned OT-2 task.

Qwen authors setup, stage order, and every scientific action in separate calls.
This script only compiles and checks each immutable prefix. It is not an
official Text2WetLab or Harbor score and cannot release a Bravo protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.evals.text2wetlab.action_ir import ActionPlanError, compile_actions
from pybravo.evals.text2wetlab.adapter import (
    EventLog,
    ProtocolValidationError,
    validate_event_safety,
    validate_ot2_source,
)
from pybravo.evals.text2wetlab.geometry import labware_geometry_context
from pybravo.evals.text2wetlab.phased_action_ir import (
    PhasedSetup,
    StageDraft,
    audit_prefix_material,
    material_handoff,
    merge_prefix,
    parse_setup,
    parse_stage,
    tip_handoff,
)
from pybravo.evals.text2wetlab.source_context import prepare_scientific_source
from pybravo.evals.text2wetlab.trusted_catalog import (
    load_trusted_catalog,
    load_trusted_module_catalog,
)
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse, structured_json
from scripts import evaluate_text2wetlab as runner
from scripts.experiment_text2wetlab_action_ir import (
    _canonicalize_deck_slots,
    _catalog_prompt,
    _check_compiled_plan,
    _line_spans,
    _sha,
)

_SETUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labware": {"type": "array", "items": {"type": "object"}},
        "modules": {"type": "array", "items": {"type": "object"}},
        "pipettes": {"type": "array", "items": {"type": "object"}},
        "stages": {"type": "array", "items": {"type": "object"}},
        "initial_supplies": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["labware", "pipettes", "stages", "initial_supplies"],
    "additionalProperties": False,
}

_STAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stage_id": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["stage_id", "evidence_refs", "actions"],
    "additionalProperties": False,
}

_SETUP_SYSTEM = """You are the local OT-2 scientific planner. Author only the JSON setup and an ordered outline of bounded stages; do not write Python, protocol actions, or final code in this call. Use the pinned task and cited scientific source lines, plus the installed read-only OT-2 catalog. The setup must include every fixed deck item, compatible module and pipette, and installed tip rack. Preserve explicit deck labels. Stage IDs must be unique snake_case; each stage has id, a concise goal, and exact evidence_refs (line IDs) covering every scientific requirement of that stage. Put sample loading before any treatment, and split a long procedure into small complete stages. Avoid one enormous stage. The number and sequence of stages come from the source; no downstream code inserts missing work.

Return keys labware, modules, pipettes, stages, initial_supplies. Labware: id, load_name, exactly one of integer slot or module_id, optional label. Module: id, exact catalog model, and slot for non-fixed modules only. Pipette: id, model, mount, tip_rack_ids. Stage: id, goal, evidence_refs. Optional initial-supply record: labware ID, selection ('all', 'wells_in_columns', or 'wells'), columns or wells as appropriate, volume_ul per selected well or null when unmeasured, material_id, evidence_refs. A non-null initial volume needs the exact amount and units in its cited source lines; do not invent starting liquid. Unknown volume is null. Source claims are audited and remain model-authored, not a hardware reading. Return no unsupported labware, reagent, refill, or sample."""

_STAGE_SYSTEM = """You are authoring exactly one bounded, ordered ActionPlan stage for a pinned OT-2 task. Return only JSON with stage_id, evidence_refs, actions. Every action, including every action in a loop, must cite exact source line IDs in evidence_refs. Use only the stage-specific paper lines and task lines supplied below; do not invent experimental steps, wells, reagent, volumes, or refills. The completed prefix's state and inventory are authoritative observations. If an absolute source volume is unknown, do not claim it is sufficient. A stage must finish with no tip attached and no liquid held. Complete the stage goal before the next stage; do not repeat earlier actions.

Action kinds: pickup/drop with pipette; aspirate/dispense with pipette, labware, well, volume_ul; mix with pipette, labware, well, cycles, volume_ul; delay seconds; pause message for real operator intervention; comment message for a non-pipetting instruction without a stop; refill_tips pipette and message only if the task permits a physical refill and existing racks are exhausted; set_temperature/set_block_temperature/set_lid_temperature with module and celsius (block may have hold_seconds); open_lid/close_lid with module; magnet_engage with module and height_from_base_mm; magnet_disengage with module. For repetition, for_each has either explicit bindings or a catalog_wells selector and a short ordered actions body. Selector series specify binding, labware, mode ('all', 'wells_in_columns', or 'column_anchors'), and explicit columns when narrowing. 'wells_in_columns' walks every physical well in named columns; 'column_anchors' walks one full eight-channel anchor per column. Use '$binding' as the entire well field. A multichannel pipette may touch only catalog-listed multichannel anchors, including a catalog-verified long trough. Never use an eight-channel head on individual sample wells. Each stroke must be within the installed pipette's minimum and the effective tip capacity, and each dispense must be funded by liquid in that tip. Plan the total tip inventory across all stages; do not assume a refill unless the source explicitly permits it. Return a complete coherent stage, not trial steps followed by corrections."""


def _model_record(response: StructuredResponse, payload_path: Path) -> dict[str, Any]:
    payload_path.write_text(json.dumps(response.payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return {
        "raw_payload_path": str(payload_path),
        "raw_payload_sha256": _sha(json.dumps(response.payload, sort_keys=True)),
        "model": response.metadata.get("model"),
        "elapsed_s": response.metadata.get("elapsed_s"),
        "usage": response.metadata.get("usage"),
    }


def _prefix_gate(
    setup: PhasedSetup, stages: list[StageDraft], *, instruction: str,
    paper: str | None, spans: dict, labware: dict, pipettes: dict,
    modules: dict, simulator: Path, task: str, task_dir: Path,
    dataset_root: Path | None, labware_dir: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Stop on the first compile, static, simulator, event, or ledger error."""
    task_dir.mkdir(parents=True, exist_ok=True)
    plan = merge_prefix(setup, stages)
    try:
        code = compile_actions(
            plan, labware_catalog=labware, pipette_catalog=pipettes,
            source_spans=spans, instruction=instruction, paper=paper,
            module_catalog=modules, refill_authorized="reset_tipracks()" in instruction,
        )
    except (ActionPlanError, ValueError) as exc:
        return {"status": "compiler_rejected", "detail": str(exc)}, None
    try:
        geometry = labware_geometry_context(
            instruction, simulator_command=simulator, labware_dir=labware_dir,
        )
        validate_ot2_source(code, geometry=geometry)
    except (ProtocolValidationError, ValueError) as exc:
        return {"status": "static_rejected", "detail": str(exc)}, None
    protocol_path = task_dir / "protocol.py"
    protocol_path.write_text(code, encoding="utf-8")
    simulator_command = [str(simulator)]
    if labware_dir is not None:
        simulator_command.extend(["-L", str(labware_dir)])
    simulator_command.append(str(protocol_path))
    simulation = runner._run(simulator_command, timeout=600)
    if simulation["status"] != "passed":
        return {"status": "simulator_rejected", "simulator": simulation}, None
    runlog = runner._official_runlog(
        task, protocol_path, task_dir, simulator, dataset_root,
        labware_dir, instruction,
    )
    if runlog["status"] != "passed":
        return {"status": "runlog_rejected", "runlog": runlog}, None
    safety = runlog.get("adapter_event_validation") or {}
    if (safety.get("status") != "passed" and safety.get("detail") ==
            "Requested on-deck heat shock has no timed high-temperature block command."):
        # A prefix before the heat-shock phase cannot yet satisfy a full-run
        # requirement. Still enforce contamination, ranges, and stroke safety
        # over the exact annotated pinned events. Once a pulse occurs, the
        # full transition rule applies and must pass at the stage boundary.
        contact_path = runlog.get("contact_evidence_events_path")
        if isinstance(contact_path, str):
            contact_events = json.loads(Path(contact_path).read_text(encoding="utf-8"))
            partial = validate_event_safety(
                EventLog(contact_events, runlog.get("labware") or {}),
                instruction=None,
            )
            safety = {"status": partial.status, "detail": partial.detail,
                      "event_count": partial.event_count,
                      "full_process_check": "pending_later_stage"}
    if safety.get("status") != "passed" or runlog.get("cross_well_aspiration_risk_count", 0):
        return {"status": "event_safety_rejected", "runlog": runlog}, None
    events_path = runlog.get("events_path")
    if not isinstance(events_path, str):
        return {"status": "runlog_rejected", "detail": "Pinned event path is missing."}, None
    events = json.loads(Path(events_path).read_text(encoding="utf-8"))
    try:
        ledger = audit_prefix_material(
            events, runlog.get("labware") or {}, setup, labware_catalog=labware,
        )
    except (ActionPlanError, ValueError) as exc:
        return {"status": "material_unresolved", "detail": str(exc)}, None
    ledger_issues = [
        {"code": issue.code, "severity": issue.severity,
         "message": issue.message, "event_index": issue.event_index}
        for issue in ledger.issues
    ]
    if ledger.has_errors:
        return {"status": "material_rejected", "issues": ledger_issues}, None
    try:
        tips = tip_handoff(plan, labware_catalog=labware, pipette_catalog=pipettes)
    except (ActionPlanError, ValueError) as exc:
        return {"status": "tip_state_unresolved", "detail": str(exc)}, None
    handoff = {"tips": tips, "materials": material_handoff(ledger),
               "completed_stage_ids": [stage.stage_id for stage in stages],
               "event_count": len(events)}
    result = {
        "status": "prefix_passed",
        "protocol_path": str(protocol_path), "protocol_sha256": _sha(code),
        "simulator": {"status": simulation["status"]},
        "event_safety": safety,
        "pinned_event_count": len(events),
        "material_status": "needs_review" if ledger_issues else "passed",
        "material_issues": ledger_issues[:24],
        "tip_state": tips,
    }
    return result, handoff


async def run_experiment(
    args: argparse.Namespace, *,
    completion: Callable[..., Awaitable[StructuredResponse]] = structured_json,
) -> dict[str, Any]:
    output = args.output_dir.expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if output == repository or repository in output.parents:
        raise ValueError("Experiment output must be outside the pyBravo repository")
    output.mkdir(parents=True, exist_ok=True)
    raw_instruction = runner._source_bytes(args.task, "instruction.md", args.dataset_root)
    if raw_instruction is None:
        return {"status": "blocked_missing_instruction", "official_score": None}
    instruction = raw_instruction.decode("utf-8")
    source_paper = runner._source_bytes(args.task, "environment/data/paper.txt", args.dataset_root)
    if args.paper_override is not None:
        override = args.paper_override.read_bytes()
        expected = (runner.ECOLI_PAPER_SHA256
                    if args.task == "ecoli-heat-shock-transformation"
                    else _sha(source_paper) if source_paper is not None else None)
        if expected is None or _sha(override) != expected:
            return {"status": "paper_override_digest_mismatch", "official_score": None}
        source_paper = override
    if b"/data/paper.txt" in raw_instruction and source_paper is None:
        return {"status": "blocked_missing_source_paper", "official_score": None}
    paper = source_paper.decode("utf-8") if source_paper else None
    labware_dir = None
    if args.task == "opentrons-rna-extraction":
        custom = runner._source_bytes(args.task, runner.RNA_LABWARE, args.dataset_root)
        if custom is None:
            return {"status": "blocked_missing_custom_labware", "official_score": None}
        labware_dir = output / "labware"
        labware_dir.mkdir(exist_ok=True)
        (labware_dir / Path(runner.RNA_LABWARE).name).write_bytes(custom)
    excerpt = prepare_scientific_source(paper, task_instruction=instruction) if paper else None
    paper_for_model = excerpt.text if excerpt else None
    spans, cited_lines = _line_spans(instruction, paper_for_model)
    labware, pipettes = load_trusted_catalog(
        instruction, simulator=args.simulator, labware_dir=labware_dir,
    )
    modules = load_trusted_module_catalog(
        instruction, simulator=args.simulator, labware=labware,
        labware_dir=labware_dir,
    )
    (output / "instruction.md").write_bytes(raw_instruction)
    if source_paper is not None:
        (output / "paper.txt").write_bytes(source_paper)
    trace: dict[str, Any] = {
        "task": args.task, "dataset": runner.DATASET, "revision": runner.REVISION,
        "instruction_sha256": _sha(raw_instruction),
        "paper_sha256": _sha(source_paper) if source_paper else None,
        "paper_excerpt_sha256": excerpt.excerpt_sha256 if excerpt else None,
        "catalog_load_names": sorted(labware),
        "catalog_pipette_names": sorted(pipettes),
        "catalog_module_names": sorted(modules),
        "stages": [], "official_score": None,
    }
    config = replace(LocalLLMConfig.from_env(), timeout_s=args.model_timeout,
                     max_tokens=args.max_output_tokens, retries=0,
                     enable_thinking=False)
    if config.base_url != "http://sparky.local:8000/v1":
        raise ValueError("This experiment permits only local Qwen at sparky.local:8000")
    catalog_text = _catalog_prompt(labware, pipettes, modules)
    try:
        setup_response = await completion([
            {"role": "system", "content": _SETUP_SYSTEM},
            {"role": "user", "content": "Pinned source lines:\n" + cited_lines
             + "\n\nInstalled catalog:\n" + catalog_text},
        ], _SETUP_SCHEMA, config=config, schema_name="ot2_phased_setup")
    except Exception as exc:
        trace.update(status="setup_model_failed", detail=f"{type(exc).__name__}: {exc}")
        return trace
    trace["setup_model"] = _model_record(setup_response, output / "qwen_setup.json")
    canonical, conversions = _canonicalize_deck_slots(setup_response.payload)
    trace["setup_syntax_canonicalizations"] = conversions
    try:
        setup = parse_setup(canonical, spans=spans, instruction=instruction,
                            paper=paper_for_model, labware_catalog=labware)
        compile_actions(
            {"labware": [item.model_dump() for item in setup.labware],
             "modules": [item.model_dump() for item in setup.modules],
             "pipettes": [item.model_dump() for item in setup.pipettes],
             "actions": []},
            labware_catalog=labware, pipette_catalog=pipettes,
            source_spans=spans, instruction=instruction, paper=paper_for_model,
            module_catalog=modules, refill_authorized="reset_tipracks()" in instruction,
        )
    except (ActionPlanError, ValueError) as exc:
        trace.update(status="setup_rejected", detail=str(exc))
        return trace
    if len(setup.stages) > getattr(args, "max_stages", 8):
        trace.update(
            status="setup_stage_limit_exceeded",
            detail=f"Model outlined {len(setup.stages)} stages; this run allows at most "
                   f"{getattr(args, 'max_stages', 8)}.",
        )
        return trace
    (output / "compiler_setup.json").write_text(
        json.dumps(setup.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    accepted: list[StageDraft] = []
    handoff: dict[str, Any] = {
        "tips": tip_handoff(merge_prefix(setup, []), labware_catalog=labware,
                            pipette_catalog=pipettes),
        "materials": {"status": "initial volumes are model claims or unknown; no actions yet"},
        "completed_stage_ids": [], "event_count": 0,
    }
    task_refs = [ref for ref in spans if ref.startswith("task.")]
    task_lines = "\n".join(line for line in cited_lines.splitlines()
                           if line.startswith("[task."))
    for index, spec in enumerate(setup.stages, 1):
        stage_dir = output / f"stage_{index:02d}_{spec.id}"
        stage_dir.mkdir(exist_ok=True)
        stage_trace: dict[str, Any] = {"stage_id": spec.id, "goal": spec.goal,
                                       "evidence_refs": spec.evidence_refs}
        trace["stages"].append(stage_trace)
        paper_lines = "\n".join(line for line in cited_lines.splitlines()
                                if any(line.startswith(f"[{ref}]")
                                       for ref in spec.evidence_refs if ref.startswith("paper.")))
        stage_prompt = (
            "Current stage:\n" + json.dumps(spec.model_dump(), ensure_ascii=False)
            + "\n\nPinned task lines:\n" + task_lines
            + "\n\nOnly these stage-specific paper lines:\n" + paper_lines
            + "\n\nFixed setup:\n" + json.dumps({
                "labware": [item.model_dump() for item in setup.labware],
                "modules": [item.model_dump() for item in setup.modules],
                "pipettes": [item.model_dump() for item in setup.pipettes],
                "initial_supplies": [item.model_dump() for item in setup.initial_supplies],
                "all_stage_goals": [{"id": item.id, "goal": item.goal}
                                    for item in setup.stages],
            }, ensure_ascii=False)
            + "\n\nTrusted catalog:\n" + catalog_text
            + "\n\nValidated state and inventory handoff:\n"
            + json.dumps(handoff, ensure_ascii=False)
        )
        try:
            stage_response = await completion([
                {"role": "system", "content": _STAGE_SYSTEM},
                {"role": "user", "content": stage_prompt},
            ], _STAGE_SCHEMA, config=config, schema_name="ot2_phased_stage")
        except Exception as exc:
            stage_trace.update(status="model_failed", detail=f"{type(exc).__name__}: {exc}")
            trace["status"] = "stage_model_failed"
            return trace
        stage_trace["model"] = _model_record(
            stage_response, stage_dir / "qwen_stage.json",
        )
        try:
            draft = parse_stage(
                stage_response.payload, expected=spec, spans=spans,
                instruction=instruction, paper=paper_for_model,
                shared_refs=task_refs,
            )
        except (ActionPlanError, ValueError) as exc:
            stage_trace.update(status="source_or_shape_rejected", detail=str(exc))
            trace["status"] = "stage_rejected"
            return trace
        prefix_result, next_handoff = _prefix_gate(
            setup, [*accepted, draft], instruction=instruction,
            paper=paper_for_model, spans=spans, labware=labware,
            pipettes=pipettes, modules=modules, simulator=args.simulator,
            task=args.task, task_dir=stage_dir, dataset_root=args.dataset_root,
            labware_dir=labware_dir,
        )
        stage_trace.update(prefix_result)
        if next_handoff is None:
            trace["status"] = "stage_rejected"
            return trace
        accepted.append(draft)
        handoff = next_handoff
        stage_trace["handoff_path"] = str(stage_dir / "state_handoff.json")
        (stage_dir / "state_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    merged = merge_prefix(setup, accepted).model_dump()
    (output / "model_authored_action_plan.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    final_dir = output / "final"
    final_dir.mkdir(exist_ok=True)
    final = _check_compiled_plan(
        merged, instruction=instruction, paper=paper_for_model,
        spans=spans, labware=labware, pipettes=pipettes,
        modules=modules, simulator=args.simulator, task=args.task,
        task_dir=final_dir, dataset_root=args.dataset_root,
        labware_dir=labware_dir,
        refill_authorized="reset_tipracks()" in instruction,
    )
    trace["final"] = final
    trace["status"] = final["status"]
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=runner.TASKS, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--paper-override", type=Path)
    parser.add_argument("--model-timeout", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-stages", type=int, default=8,
                        help="Hard cap on model-authored stages and subsequent local calls")
    args = parser.parse_args()
    if (not args.simulator.is_file() or not 1 <= args.model_timeout <= 300
            or not 512 <= args.max_output_tokens <= 16000
            or not 1 <= args.max_stages <= 16):
        parser.error("Provide a simulator, 1–300 s timeout, 512–16000 output tokens, "
                     "and 1–16 maximum stages")
    try:
        trace = asyncio.run(run_experiment(args))
    except (OSError, RuntimeError, ValueError) as exc:
        trace = {"task": args.task, "status": "experiment_failed",
                 "detail": f"{type(exc).__name__}: {exc}", "official_score": None}
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / "phased_action_ir_trace.json"
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(json.dumps({"trace": str(path), "status": trace["status"],
                      "official_score": None}))
    return 0 if trace["status"] == "mechanical_gates_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
