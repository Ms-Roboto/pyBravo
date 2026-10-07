"""Experimental, staged local-Qwen ActionPlan for a pinned OT-2 task.

Qwen authors setup, stage order, and every scientific action in separate calls.
This script only compiles and checks each immutable prefix. It is not an
official Text2WetLab or Harbor score and cannot release a Bravo protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
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
    StageSpec,
    audit_prefix_material,
    material_handoff,
    merge_prefix,
    parse_setup,
    parse_stage,
    tip_handoff,
)
from pybravo.evals.text2wetlab.phased_reaction_math import (
    ReactionMathContext,
    ReactionMathPlan,
    audit_observed_premix,
    parse_and_audit_math,
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
        "initial_supplies": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "labware": {"type": "string"},
                "selection": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "integer"}},
                "wells": {"type": "array", "items": {"type": "string"}},
                "volume_ul": {"type": ["number", "null"]},
                "material_id": {"type": "string"},
                "source_material_name": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["labware", "selection", "volume_ul", "material_id",
                         "source_material_name", "evidence_refs"],
            "additionalProperties": False,
        }},
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

_MATH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stage_id": {"type": "string"},
        "reaction_count": {"type": "integer"},
        "final_volume_ul": {"type": "number"},
        "premix_labware": {"type": "string"},
        "premix_well": {"type": "string"},
        "premix_target_ul_per_reaction": {"type": "number"},
        "components": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "phase": {"enum": ["premix", "later"]},
                "volume_ul_per_reaction": {"type": ["number", "null"]},
                "source_material_ids": {"type": "array", "items": {"type": "string"}},
                "source_usage": {"enum": ["single", "each", "one_of"]},
                "source_labware": {"type": ["string", "null"]},
                "source_well": {"type": ["string", "null"]},
                "delivery": {"enum": ["robot", "manual"]},
                "basis": {"enum": ["direct", "calculated", "assumption"]},
                "assumption_note": {"type": ["string", "null"]},
                "stock_strength_x": {"type": ["number", "null"]},
                "target_strength_x": {"type": ["number", "null"]},
                "is_diluent": {"type": "boolean"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "phase", "volume_ul_per_reaction", "basis",
                         "evidence_refs"],
            "additionalProperties": False,
        }},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stage_id", "reaction_count", "final_volume_ul",
                 "premix_labware", "premix_well", "premix_target_ul_per_reaction",
                 "components", "evidence_refs"],
    "additionalProperties": False,
}

_SETUP_SYSTEM = """You are the local OT-2 scientific planner. Author only the JSON setup and an ordered outline of bounded stages; do not write Python, protocol actions, or final code in this call. Use the pinned task and cited scientific source lines, plus the installed read-only OT-2 catalog. The setup must include every fixed deck item, compatible module and pipette, and installed tip rack. Preserve explicit deck labels. Stage IDs must be unique snake_case; each stage has id, a concise goal, and exact evidence_refs (line IDs) covering every scientific requirement of that stage. Put sample loading before any treatment, and split a long procedure into small complete stages. Avoid one enormous stage. The number and sequence of stages come from the source; no downstream code inserts missing work.

Return keys labware, modules, pipettes, stages, initial_supplies. Labware: id, load_name, exactly one of integer slot or module_id, optional label. Module: id, exact catalog model, and slot for non-fixed modules only. Pipette: id, model, mount, tip_rack_ids. Stage: id, goal, evidence_refs. Optional initial-supply record: key `labware` (the loaded labware ID), selection ('all', 'wells_in_columns', or 'wells'), columns or wells as appropriate, volume_ul per selected well or null when unmeasured, material_id, source_material_name, evidence_refs. `source_material_name` must quote verbatim material words on the cited task line that also names the exact labware and each selected well. A positive initial volume needs the exact amount and units on that same line; a zero needs the same line to say that well starts empty. Split different materials into different records. Do not invent starting liquid. Unknown volume is null. Source claims are audited and remain model-authored, not a hardware reading. Return no unsupported labware, reagent, refill, or sample."""

_MATH_SYSTEM = """You are authoring a cited reaction-math subplan, before any pipetting actions. Return only JSON matching the schema. Do not write Python or pipette steps. For the current preparation stage, identify the cited reaction count and final volume per reaction. List every per-reaction component, including components added later after the premix. Separate phase=premix from phase=later. Derive premix_target_ul_per_reaction as final volume minus all later additions. List each premix reagent from its exact fixed-setup material ID, labware ID, and physical source well; source_material_ids is a one-item array and source_usage='single' for each premix reagent. For later components with several material IDs, source_usage='each' means the stated per-reaction volume is added separately from every listed material, while source_usage='one_of' means only one listed alternative is used per reaction. Do not merge distinct ingredients into one total when each requires a target concentration. Later additions may leave source_labware/source_well null until their action stage. Use cited line IDs for every component and name the basis as direct, calculated, or assumption. For an assumed volume, include an explicit assumption_note; never disguise a choice as a cited fact. Supply both stock_strength_x and target_strength_x only when their comparable values are supported by source facts. For one diluent, calculate its volume as the arithmetic remainder and set is_diluent=true. All component volumes are per reaction; do not multiply them by reaction_count in the JSON. Unknown volumes stay null. No protocol action is allowed until the arithmetic and physical source audit passes."""

_STAGE_SYSTEM = """You are authoring exactly one bounded, ordered ActionPlan stage for a pinned OT-2 task. Return only JSON with stage_id, evidence_refs, actions. Every action, including every action in a loop, must cite exact source line IDs in evidence_refs. Process actions may cite only the current stage's evidence_refs. Pickup/drop/refill may also cite the separately listed fixed instrument/tip task lines. If the stage cites paper lines, every non-tip action must cite a stage-specific paper line. Do not invent experimental steps, wells, reagents, volumes, or refills. The completed prefix's state and inventory are authoritative observations. If an absolute source volume is unknown, do not claim it is sufficient. A stage must finish with no tip attached and no liquid held. Complete the stage goal before the next stage; do not repeat earlier actions. When a validated model-authored reaction-math subplan is supplied, follow its per-reaction and batch component quantities exactly; do not revise its numbers in this action call.

Action kinds: pickup/drop with pipette; aspirate/dispense with pipette, labware, well, volume_ul; mix with pipette, labware, well, cycles, volume_ul; delay seconds; pause message for real operator intervention; comment message for a non-pipetting instruction without a stop; refill_tips pipette and message only if the task permits a physical refill and existing racks are exhausted; set_temperature/set_block_temperature/set_lid_temperature with module and celsius (block may have hold_seconds); open_lid/close_lid with module; magnet_engage with module and height_from_base_mm; magnet_disengage with module. For repetition, for_each has either explicit bindings or a catalog_wells selector and a short ordered actions body. Selector series specify binding, labware, mode ('all', 'wells_in_columns', or 'column_anchors'), and explicit columns when narrowing. 'wells_in_columns' walks every physical well in named columns; 'column_anchors' walks one full eight-channel anchor per column. Use '$binding' as the entire well field. A multichannel pipette may touch only catalog-listed multichannel anchors, including a catalog-verified long trough. Never use an eight-channel head on individual sample wells. Give each distinct reagent stock its own fresh tip: pick up, aspirate from that stock, dispense into the intermediate, and drop before touching another stock. If mixing the intermediate after all additions, pick up another fresh tip for that mixing action; do not reuse the tip that transferred the last stock. Each stroke must be within the installed pipette's minimum and the effective tip capacity, and each dispense must be funded by liquid in that tip. For a reaction premix, derive the per-reaction premix target by subtracting every separately added component (such as primers or template) from the cited final reaction volume. Multiply that target by the cited reaction count, derive the other premix components from cited source facts, and calculate the diluent as the remainder of the premix. Check both per-reaction and total arithmetic before returning the stage; if the sources do not establish required inputs, do not guess. Plan the total tip inventory across all stages; do not assume a refill unless the source explicitly permits it. Return a complete coherent stage, not trial steps followed by corrections."""

_STAGE_SYSTEM += (" A dispense may set location='top' only for a noncontact reagent "
                  "addition at or above the rim of an empty or untouched destination; "
                  "ordinary dispenses use the default well location. Aspirate and mix "
                  "cannot use location='top'.")


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


def _canonicalize_setup_supply_keys(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize only one unambiguous name for a loaded-labware reference."""
    normalized = json.loads(json.dumps(payload))
    changes: list[str] = []
    supplies = normalized.get("initial_supplies")
    if not isinstance(supplies, list):
        return normalized, changes
    for index, item in enumerate(supplies):
        if not isinstance(item, dict) or "labware_id" not in item:
            continue
        if "labware" in item:
            raise ActionPlanError(
                f"/initial_supplies/{index} supplies both labware and labware_id."
            )
        item["labware"] = item.pop("labware_id")
        changes.append(f"/initial_supplies/{index}/labware_id -> labware")
    return normalized, changes


_PROCESS_WORDS = re.compile(
    r"\b(?:add(?:ed|ing)?|transfer(?:red|ring)?|mix(?:ed|ing)?|incubat\w*|"
    r"heat(?:ed|ing)?|cool(?:ed|ing)?|wash(?:ed|ing)?|elut\w*|centrifug\w*|"
    r"purif\w*|extract\w*|digest\w*|amplif\w*|cycl\w*|transform\w*|"
    r"recover\w*|seal(?:ed|ing)?|prepar\w*)\b", re.I,
)

_PREPARATION_WELL = re.compile(
    r"`(?P<labware>[^`]+)`\s+well\s+(?P<well>[A-Z]+[1-9][0-9]*)\s*:.*"
    r"\bmaster\s+mix\b.*\bfor\s+(?P<count>[1-9][0-9]*)\s+reactions?\b",
    re.I,
)
_REACTION_VOLUME = re.compile(
    r"\b(?:PCRs?|reactions?)\s+(?:were|was|are|is)\s+"
    r"(?:performed|run|prepared)\s+in\s+(?P<volume>\d+(?:\.\d+)?)\s*"
    r"(?:µL|μL|uL)\s+volumes?\b",
    re.I,
)


def _setup_source_coverage(setup: PhasedSetup, cited_lines: str) -> dict[str, Any]:
    """Report uncited procedural-looking lines; never infer or insert stages."""
    cited = {ref for stage in setup.stages for ref in stage.evidence_refs}
    candidates: list[dict[str, str]] = []
    total = 0
    for line in cited_lines.splitlines():
        matched = re.match(r"^\[([^]]+)\] (.*)$", line)
        if matched is None:
            continue
        total += 1
        ref, quote = matched.groups()
        if ref not in cited and _PROCESS_WORDS.search(quote):
            candidates.append({"ref": ref, "quote": quote[:300]})
    return {
        "status": "needs_review" if candidates else "citation_coverage_only",
        "stage_cited_line_count": len(cited),
        "source_line_count": total,
        "uncited_procedural_candidate_count": len(candidates),
        "uncited_procedural_candidates": candidates[:60],
        "scientific_completeness_verified": False,
        "note": "Line citations do not prove the stage outline covers every procedure step.",
    }


def _setup_digest(setup: PhasedSetup) -> str:
    """Bind saved-stage replay to the exact parsed setup, including supplies."""
    return _sha(json.dumps(setup.model_dump(mode="json"), sort_keys=True,
                           separators=(",", ":")))


def _reaction_math_context(
    setup: PhasedSetup, spec: StageSpec, *, spans: dict,
    instruction: str, paper: str | None,
) -> ReactionMathContext | None:
    """Detect an unambiguous, source-cited preparation target and final size."""
    targets: list[tuple[str, str, int, str]] = []
    volumes: list[tuple[float, str]] = []
    for ref in spec.evidence_refs:
        span = spans.get(ref)
        if span is None:
            continue
        quote = (instruction if span.source == "instruction" else paper or "")[
            span.start:span.end
        ]
        if span.source == "instruction":
            matched = _PREPARATION_WELL.search(quote)
            if matched and re.search(r"\bempty\s+at\s+the\s+start\b", quote, re.I):
                targets.append((matched["labware"], matched["well"],
                                int(matched["count"]), ref))
        else:
            volumes.extend((float(matched["volume"]), ref)
                           for matched in _REACTION_VOLUME.finditer(quote))
    if len(targets) != 1 or len(volumes) != 1:
        return None
    labware_id, well, count, task_ref = targets[0]
    if labware_id not in {item.id for item in setup.labware}:
        return None
    return ReactionMathContext(
        spec.id, count, volumes[0][0], labware_id, well,
        task_ref, volumes[0][1],
    )


def _math_feedback(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return fixed issue codes and numeric values, never candidate prose."""
    allowed = {"code", "index", "expected", "observed", "material_id",
               "path", "error_type"}
    return [{key: value for key, value in issue.items()
             if key in allowed and (key == "code" or isinstance(value, (int, float))
                                    or (key == "material_id" and isinstance(value, str)
                                        and re.fullmatch(r"[A-Za-z0-9_]{1,48}", value))
                                    or (key in {"path", "error_type"} and
                                        isinstance(value, str) and
                                        re.fullmatch(r"[A-Za-z0-9_.]{1,80}", value)))}
            for issue in issues[:12]]


def _preparation_envelope_issues(
    setup: PhasedSetup, stage: StageDraft, *, spans: dict,
    instruction: str, paper: str | None, events: list[dict],
    event_labware: dict[str, str],
) -> list[dict[str, Any]]:
    """Check an explicitly cited reaction-count/volume upper bound.

    This is deliberately an upper bound, not an inferred recipe. A stage must
    cite both the task's named empty premix well/count and a paper reaction
    volume. Unrecognized wording yields no invented constraint.
    """
    task_bounds: list[tuple[str, str, int, str]] = []
    paper_volumes: list[tuple[float, str]] = []
    for ref in stage.evidence_refs:
        span = spans.get(ref)
        if span is None:
            continue
        quote = (instruction if span.source == "instruction" else paper or "")[
            span.start:span.end
        ]
        if span.source == "instruction":
            target = _PREPARATION_WELL.search(quote)
            if target and re.search(r"\bempty\s+at\s+the\s+start\b", quote, re.I):
                task_bounds.append((target["labware"], target["well"],
                                    int(target["count"]), ref))
        else:
            for volume in _REACTION_VOLUME.finditer(quote):
                paper_volumes.append((float(volume["volume"]), ref))
    if len(task_bounds) != 1 or len(paper_volumes) != 1:
        return []
    labware_id, well, count, task_ref = task_bounds[0]
    load = next((item for item in setup.labware if item.id == labware_id), None)
    if load is None:
        return []
    label = load.label or load.id
    observed_names = [name for name, load_name in event_labware.items()
                      if load_name == load.load_name and
                      (name == label or name.startswith(f"{label} on "))]
    if len(observed_names) != 1:
        return []
    bound = count * paper_volumes[0][0]
    running = 0.0
    peak = 0.0
    for event in events:
        if (event.get("labware") != observed_names[0] or
                event.get("well") != well):
            continue
        if event.get("kind") == "dispense":
            running += float(event.get("volume") or 0)
        elif event.get("kind") == "aspirate":
            running -= float(event.get("volume") or 0)
        peak = max(peak, running)
    if peak <= bound + 1e-6:
        return []
    return [{
        "code": "cited_preparation_exceeds_reaction_envelope",
        "labware": labware_id, "well": well,
        "observed_peak_ul": round(peak, 6), "maximum_final_reaction_ul": bound,
        "reaction_count": count, "reaction_volume_ul": paper_volumes[0][0],
        "source_refs": [task_ref, paper_volumes[0][1]],
        "message": "Prepared intermediate exceeds the cited count × final reaction "
                   "volume before other required additions.",
    }]


def _prefix_gate(
    setup: PhasedSetup, stages: list[StageDraft], *, instruction: str,
    paper: str | None, spans: dict, labware: dict, pipettes: dict,
    modules: dict, simulator: Path, task: str, task_dir: Path,
    dataset_root: Path | None, labware_dir: Path | None,
    math_plan: ReactionMathPlan | None = None, prior_event_count: int = 0,
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
    events_path = runlog.get("events_path")
    if not isinstance(events_path, str):
        return {"status": "runlog_rejected", "detail": "Pinned event path is missing."}, None
    events = json.loads(Path(events_path).read_text(encoding="utf-8"))
    envelope_issues = _preparation_envelope_issues(
        setup, stages[-1], spans=spans, instruction=instruction, paper=paper,
        events=events, event_labware=runlog.get("labware") or {},
    )
    math_action_issues = audit_observed_premix(
        math_plan, setup=setup, events=events,
        event_labware=runlog.get("labware") or {},
        start_event_index=prior_event_count,
    ) if math_plan is not None else []
    if safety.get("status") != "passed" or runlog.get("cross_well_aspiration_risk_count", 0):
        return {"status": "event_safety_rejected", "runlog": runlog,
                "preparation_envelope_issues": envelope_issues,
                "reaction_math_action_issues": math_action_issues}, None
    if envelope_issues:
        return {"status": "preparation_envelope_rejected",
                "preparation_envelope_issues": envelope_issues,
                "reaction_math_action_issues": math_action_issues}, None
    if math_action_issues:
        return {"status": "reaction_math_action_mismatch",
                "reaction_math_action_issues": math_action_issues}, None
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
        "status": "prefix_material_review_required" if ledger_issues else "prefix_passed",
        "protocol_path": str(protocol_path), "protocol_sha256": _sha(code),
        "simulator": {"status": simulation["status"]},
        "event_safety": safety,
        "pinned_event_count": len(events),
        "material_status": "needs_review" if ledger_issues else "passed",
        "material_issues": ledger_issues[:24],
        "tip_state": tips,
        "reaction_math_action_status": "passed" if math_plan else "not_applicable",
    }
    return result, handoff


def _retry_feedback(failure: dict[str, Any]) -> dict[str, Any]:
    """Expose only the failed mechanical path, never rubric or reference text."""
    status = failure.get("status")
    feedback: dict[str, Any] = {"status": status}
    if status == "source_or_shape_rejected":
        detail = str(failure.get("detail", ""))
        # Custom parser errors carry a fixed action path. Pydantic messages
        # may echo candidate values, so never return those to the model.
        feedback["detail"] = (detail[:300] if re.match(
            r"^(?:actions\[\d+\]|Stage cites|Expected stage|Equipment references)",
            detail,
        ) else "Stage JSON failed schema or source-scope validation.")
    elif status in {"compiler_rejected", "static_rejected",
                    "material_unresolved", "tip_state_unresolved"}:
        feedback["detail"] = str(failure.get("detail", "")).splitlines()[0][:300]
    elif status == "simulator_rejected":
        feedback["detail"] = "Simulator rejected the compiled prefix; raw output is untrusted and retained only in the local trace."
    elif status == "runlog_rejected":
        feedback["detail"] = "Pinned runlog rejected the compiled prefix; raw output is retained only in the local trace."
    elif status == "event_safety_rejected":
        runlog = failure.get("runlog") or {}
        feedback["detail"] = "Pinned event safety rejected this prefix."
        feedback["cross_well_aspiration_risk_count"] = int(
            runlog.get("cross_well_aspiration_risk_count") or 0
        )
        feedback["cross_well_aspiration_risks"] = [{
            "distinct_wells": int(item.get("distinct_wells") or 0),
            "example_wells": [well for well in item.get("example_wells", [])[:8]
                              if isinstance(well, str) and
                              re.fullmatch(r"[A-Z]+[1-9][0-9]*", well)],
        } for item in (runlog.get("cross_well_aspiration_risks") or [])[:3]
            if isinstance(item, dict)]
        feedback["preparation_envelope_issues"] = (
            failure.get("preparation_envelope_issues") or []
        )[:2]
        feedback["reaction_math_action_issues"] = _math_feedback(
            failure.get("reaction_math_action_issues") or []
        )
    elif status == "preparation_envelope_rejected":
        feedback["preparation_envelope_issues"] = (
            failure.get("preparation_envelope_issues") or []
        )[:2]
        feedback["reaction_math_action_issues"] = _math_feedback(
            failure.get("reaction_math_action_issues") or []
        )
    elif status == "reaction_math_action_mismatch":
        feedback["reaction_math_action_issues"] = _math_feedback(
            failure.get("reaction_math_action_issues") or []
        )
    elif status == "material_rejected":
        feedback["issues"] = (failure.get("issues") or [])[:8]
    return feedback


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
        "scientific_completeness_verified": False,
    }
    config = replace(LocalLLMConfig.from_env(), timeout_s=args.model_timeout,
                     max_tokens=args.max_output_tokens, retries=0,
                     enable_thinking=False)
    if config.base_url != "http://sparky.local:8000/v1":
        raise ValueError("This experiment permits only local Qwen at sparky.local:8000")
    catalog_text = _catalog_prompt(labware, pipettes, modules)
    saved_setup = getattr(args, "saved_setup", None)
    if saved_setup is not None:
        saved_path = saved_setup.expanduser().resolve()
        source_trace = saved_path.parent / "phased_action_ir_trace.json"
        if not source_trace.is_file():
            trace.update(status="saved_setup_rejected", detail="Source trace is missing.")
            return trace
        previous = json.loads(source_trace.read_text(encoding="utf-8"))
        setup_payload = json.loads(saved_path.read_text(encoding="utf-8"))
        if (previous.get("task") != args.task or previous.get("revision") != runner.REVISION
                or previous.get("instruction_sha256") != trace["instruction_sha256"]
                or previous.get("paper_sha256") != trace["paper_sha256"]
                or previous.get("paper_excerpt_sha256") != trace["paper_excerpt_sha256"]
                or (previous.get("setup_model") or {}).get("raw_payload_sha256") !=
                   _sha(json.dumps(setup_payload, sort_keys=True))):
            trace.update(status="saved_setup_rejected",
                         detail="Saved model setup does not match the pinned task and sources.")
            return trace
        trace["setup_model"] = {
            "reused_raw_payload_path": str(saved_path),
            "raw_payload_sha256": _sha(json.dumps(setup_payload, sort_keys=True)),
            "source_trace_path": str(source_trace),
        }
        (output / "qwen_setup.json").write_text(
            json.dumps(setup_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        try:
            setup_response = await completion([
                {"role": "system", "content": _SETUP_SYSTEM},
                {"role": "user", "content": "Pinned source lines:\n" + cited_lines
                 + "\n\nInstalled catalog:\n" + catalog_text},
            ], _SETUP_SCHEMA, config=config, schema_name="ot2_phased_setup")
        except Exception as exc:
            trace.update(status="setup_model_failed", detail=f"{type(exc).__name__}: {exc}")
            return trace
        setup_payload = setup_response.payload
        trace["setup_model"] = _model_record(setup_response, output / "qwen_setup.json")
    canonical, conversions = _canonicalize_deck_slots(setup_payload)
    try:
        canonical, key_conversions = _canonicalize_setup_supply_keys(canonical)
    except ActionPlanError as exc:
        trace.update(status="setup_rejected", detail=str(exc))
        return trace
    trace["setup_syntax_canonicalizations"] = [*conversions, *key_conversions]
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
    trace["setup_sha256"] = _setup_digest(setup)
    trace["setup_source_coverage"] = _setup_source_coverage(setup, cited_lines)
    accepted: list[StageDraft] = []
    handoff: dict[str, Any] = {
        "tips": tip_handoff(merge_prefix(setup, []), labware_catalog=labware,
                            pipette_catalog=pipettes),
        "materials": {"status": "initial volumes are model claims or unknown; no actions yet"},
        "completed_stage_ids": [], "event_count": 0,
    }
    equipment_refs = [ref for ref, span in spans.items()
                      if span.source == "instruction" and re.search(
                          r"\b(?:pipette|tiprack|tips?)\b",
                          instruction[span.start:span.end], re.I,
                      )]
    for index, spec in enumerate(setup.stages, 1):
        stage_dir = output / f"stage_{index:02d}_{spec.id}"
        stage_dir.mkdir(exist_ok=True)
        stage_trace: dict[str, Any] = {"stage_id": spec.id, "goal": spec.goal,
                                       "evidence_refs": spec.evidence_refs}
        trace["stages"].append(stage_trace)
        paper_lines = "\n".join(line for line in cited_lines.splitlines()
                                if any(line.startswith(f"[{ref}]")
                                       for ref in spec.evidence_refs if ref.startswith("paper.")))
        task_lines = "\n".join(line for line in cited_lines.splitlines()
                               if any(line.startswith(f"[{ref}]")
                                      for ref in [*spec.evidence_refs, *equipment_refs]
                                      if ref.startswith("task.")))
        stage_prompt = (
            "Current stage:\n" + json.dumps(spec.model_dump(), ensure_ascii=False)
            + "\n\nPinned task lines:\n" + task_lines
            + "\n\nFixed instrument/tip refs allowed for pickup/drop/refill only:\n"
            + json.dumps(equipment_refs)
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
        math_context = _reaction_math_context(
            setup, spec, spans=spans, instruction=instruction,
            paper=paper_for_model,
        )
        math_plan: ReactionMathPlan | None = None
        if math_context is not None:
            math_trace: dict[str, Any] = {
                "context": {
                    "stage_id": math_context.stage_id,
                    "reaction_count": math_context.reaction_count,
                    "final_volume_ul": math_context.final_volume_ul,
                    "premix_labware": math_context.premix_labware,
                    "premix_well": math_context.premix_well,
                    "source_refs": [math_context.task_ref, math_context.paper_ref],
                },
                "attempts": [],
            }
            stage_trace["reaction_math"] = math_trace
            all_task_lines = "\n".join(line for line in cited_lines.splitlines()
                                       if line.startswith("[task."))
            math_prompt = (
                "Current preparation stage:\n" + json.dumps(
                    spec.model_dump(), ensure_ascii=False,
                ) + "\n\nPinned task lines:\n" + all_task_lines
                + "\n\nStage-specific paper lines:\n" + paper_lines
                + "\n\nFixed loaded materials and sources:\n" + json.dumps(
                    [item.model_dump() for item in setup.initial_supplies],
                    ensure_ascii=False,
                )
                + "\n\nPreparation target is identified by the cited source line. "
                  "Derive the premix and later component quantities; do not copy "
                  "a prior failed action plan."
            )
            prior_math_payload: dict[str, Any] | None = None
            prior_math_issues: list[dict[str, Any]] = []
            for math_attempt_index in range(getattr(args, "math_retries", 1) + 1):
                math_attempt: dict[str, Any] = {"attempt": math_attempt_index + 1}
                math_trace["attempts"].append(math_attempt)
                messages = [
                    {"role": "system", "content": _MATH_SYSTEM},
                    {"role": "user", "content": math_prompt},
                ]
                if prior_math_payload is not None:
                    messages.extend([
                        {"role": "assistant", "content": json.dumps(
                            prior_math_payload, ensure_ascii=False,
                        )},
                        {"role": "user", "content":
                         "The math subplan failed its deterministic source or "
                         "reaction audit. Return a complete corrected math JSON "
                         "using only the cited source. Issues:\n" + json.dumps(
                             _math_feedback(prior_math_issues), ensure_ascii=False,
                         )},
                    ])
                try:
                    math_response = await completion(
                        messages, _MATH_SCHEMA, config=config,
                        schema_name="ot2_phased_reaction_math",
                    )
                except Exception as exc:
                    math_attempt.update(status="model_failed",
                                        detail=f"{type(exc).__name__}: {exc}")
                    math_trace["status"] = "model_failed"
                    trace["status"] = "reaction_math_model_failed"
                    return trace
                math_payload = math_response.payload
                math_path = stage_dir / (
                    "qwen_math.json" if math_attempt_index == 0 else
                    f"qwen_math_retry_{math_attempt_index}.json"
                )
                math_attempt["model"] = _model_record(math_response, math_path)
                prior_math_payload = math_payload
                math_plan, math_issues = parse_and_audit_math(
                    math_payload, context=math_context, setup=setup,
                    spans=spans, instruction=instruction, paper=paper_for_model,
                    labware_catalog=labware,
                    allowed_paper_refs=[ref for ref in spec.evidence_refs
                                        if ref.startswith("paper.")],
                )
                math_attempt["issues"] = math_issues
                math_attempt["status"] = "passed" if not math_issues else "rejected"
                if not math_issues:
                    break
                prior_math_issues = math_issues
            if math_issues or math_plan is None:
                math_trace["status"] = "rejected"
                trace["status"] = "reaction_math_rejected"
                return trace
            math_trace["status"] = "passed"
            math_trace["validated_plan_path"] = str(stage_dir / "validated_math.json")
            (stage_dir / "validated_math.json").write_text(
                json.dumps(math_plan.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            math_trace["assumption_count"] = sum(
                item.basis == "assumption" for item in math_plan.components
            )
            math_trace["scientific_completeness_verified"] = False
            stage_prompt += (
                "\n\nValidated model-authored reaction math (fixed for this "
                "action stage):\n" + json.dumps(
                    math_plan.model_dump(), ensure_ascii=False,
                )
            )
        attempts: list[dict[str, Any]] = []
        stage_trace["attempts"] = attempts
        previous_payload: dict[str, Any] | None = None
        previous_failure: dict[str, Any] | None = None
        max_retries = getattr(args, "stage_retries", 0)
        for attempt_index in range(max_retries + 1):
            attempt: dict[str, Any] = {"attempt": attempt_index + 1}
            attempts.append(attempt)
            saved_failed = getattr(args, "saved_failed_stage", None)
            if attempt_index == 0 and index == 1 and saved_failed is not None:
                saved_path = saved_failed.expanduser().resolve()
                source_trace_path = saved_path.parent.parent / "phased_action_ir_trace.json"
                if not source_trace_path.is_file():
                    trace.update(status="saved_stage_rejected",
                                 detail="Failed-stage source trace is missing.")
                    return trace
                prior_trace = json.loads(source_trace_path.read_text(encoding="utf-8"))
                stage_payload = json.loads(saved_path.read_text(encoding="utf-8"))
                prior_stages = prior_trace.get("stages") or []
                prior_stage = prior_stages[0] if prior_stages else {}
                prior_model = prior_stage.get("model") or {}
                prior_setup_path = source_trace_path.parent / "compiler_setup.json"
                if not prior_setup_path.is_file():
                    trace.update(status="saved_stage_rejected",
                                 detail="Saved stage has no exact parsed setup snapshot.")
                    return trace
                prior_parsed_setup = json.loads(prior_setup_path.read_text(encoding="utf-8"))
                prior_setup_sha = _sha(json.dumps(prior_parsed_setup, sort_keys=True,
                                                  separators=(",", ":")))
                if (prior_trace.get("task") != args.task
                        or prior_trace.get("revision") != runner.REVISION
                        or prior_trace.get("instruction_sha256") != trace["instruction_sha256"]
                        or prior_trace.get("paper_sha256") != trace["paper_sha256"]
                        or prior_trace.get("paper_excerpt_sha256") != trace["paper_excerpt_sha256"]
                        or (prior_trace.get("setup_model") or {}).get("raw_payload_sha256") !=
                           (trace.get("setup_model") or {}).get("raw_payload_sha256")
                        or prior_setup_sha != trace["setup_sha256"]
                        or (prior_trace.get("setup_sha256") is not None and
                            prior_trace["setup_sha256"] != trace["setup_sha256"])
                        or prior_stage.get("stage_id") != spec.id
                        or prior_model.get("raw_payload_sha256") !=
                           _sha(json.dumps(stage_payload, sort_keys=True))):
                    trace.update(status="saved_stage_rejected",
                                 detail="Saved failed stage does not match the pinned source.")
                    return trace
                attempt["model"] = {
                    "reused_raw_payload_path": str(saved_path),
                    "raw_payload_sha256": prior_model["raw_payload_sha256"],
                    "source_trace_path": str(source_trace_path),
                }
                (stage_dir / "qwen_stage.json").write_text(
                    json.dumps(stage_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            else:
                messages = [
                    {"role": "system", "content": _STAGE_SYSTEM},
                    {"role": "user", "content": stage_prompt},
                ]
                if previous_payload is not None and previous_failure is not None:
                    messages.extend([
                        {"role": "assistant", "content": json.dumps(
                            previous_payload, ensure_ascii=False,
                        )},
                        {"role": "user", "content":
                         "The preceding stage response failed a formal local gate. "
                         "Return a complete corrected stage JSON using only the "
                         "cited source and trusted catalog. Do not omit valid work. "
                         "Failure:\n" + json.dumps(
                             _retry_feedback(previous_failure), ensure_ascii=False,
                         )},
                    ])
                try:
                    stage_response = await completion(
                        messages, _STAGE_SCHEMA, config=config,
                        schema_name="ot2_phased_stage",
                    )
                except Exception as exc:
                    attempt.update(status="model_failed",
                                   detail=f"{type(exc).__name__}: {exc}")
                    stage_trace.update(status="model_failed", detail=attempt["detail"])
                    trace["status"] = "stage_model_failed"
                    return trace
                stage_payload = stage_response.payload
                response_path = stage_dir / (
                    "qwen_stage.json" if attempt_index == 0 else
                    f"qwen_stage_retry_{attempt_index}.json"
                )
                attempt["model"] = _model_record(stage_response, response_path)
            stage_trace["model"] = attempt["model"]
            previous_payload = stage_payload
            try:
                draft = parse_stage(
                    stage_payload, expected=spec, spans=spans,
                    instruction=instruction, paper=paper_for_model,
                    equipment_refs=equipment_refs,
                )
            except (ActionPlanError, ValueError) as exc:
                failure = {"status": "source_or_shape_rejected", "detail": str(exc)}
                attempt.update(failure)
                previous_failure = failure
                if attempt_index < max_retries:
                    continue
                stage_trace.update(failure)
                trace["status"] = "stage_rejected"
                return trace
            prefix_result, next_handoff = _prefix_gate(
                setup, [*accepted, draft], instruction=instruction,
                paper=paper_for_model, spans=spans, labware=labware,
                pipettes=pipettes, modules=modules, simulator=args.simulator,
                task=args.task, task_dir=stage_dir / f"attempt_{attempt_index + 1}",
                dataset_root=args.dataset_root,
                labware_dir=labware_dir,
                math_plan=math_plan, prior_event_count=handoff["event_count"],
            )
            attempt.update(prefix_result)
            if next_handoff is not None:
                stage_trace.update(prefix_result)
                break
            previous_failure = prefix_result
            if attempt_index == max_retries:
                stage_trace.update(prefix_result)
                trace["status"] = "stage_rejected"
                return trace
        accepted.append(draft)
        handoff = next_handoff
        stage_trace["handoff_path"] = str(stage_dir / "state_handoff.json")
        (stage_dir / "state_handoff.json").write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        max_new = getattr(args, "max_new_stages", None)
        if max_new is not None and len(accepted) >= max_new:
            prefix = merge_prefix(setup, accepted).model_dump()
            (output / "model_authored_prefix.json").write_text(
                json.dumps(prefix, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            math_assumptions = any(
                (stage.get("reaction_math") or {}).get("assumption_count", 0) > 0
                for stage in trace["stages"]
            )
            material_review = any(
                stage.get("material_status") == "needs_review"
                for stage in trace["stages"]
            )
            trace["status"] = (
                "partial_prefix_science_review_required"
                if math_assumptions or material_review else "partial_prefix_passed"
            )
            trace["review_reasons"] = {
                "model_assumptions": math_assumptions,
                "material": material_review,
                "source_coverage": trace["setup_source_coverage"]["status"] == "needs_review",
            }
            trace["remaining_stage_ids"] = [item.id for item in setup.stages[len(accepted):]]
            return trace
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
    material_review = any(
        stage.get("material_status") == "needs_review" for stage in trace["stages"]
    )
    math_assumptions = any(
        (stage.get("reaction_math") or {}).get("assumption_count", 0) > 0
        for stage in trace["stages"]
    )
    coverage_review = trace["setup_source_coverage"]["status"] == "needs_review"
    rubric_review = final.get("local_rubric_status") != "supported"
    trace["status"] = (
        "science_review_required"
        if final["status"] == "mechanical_gates_passed"
        and (material_review or math_assumptions or coverage_review or rubric_review)
        else final["status"]
    )
    trace["review_reasons"] = {
        "material": material_review, "source_coverage": coverage_review,
        "local_rubric": rubric_review, "model_assumptions": math_assumptions,
    }
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=runner.TASKS, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--paper-override", type=Path)
    parser.add_argument("--saved-setup", type=Path,
                        help="Reuse an exact local-Qwen setup with a matching source trace")
    parser.add_argument("--saved-failed-stage", type=Path,
                        help="Start a bounded repair from an exact saved local-Qwen stage")
    parser.add_argument("--model-timeout", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-stages", type=int, default=8,
                        help="Hard cap on model-authored stages and subsequent local calls")
    parser.add_argument("--max-new-stages", type=int,
                        help="Stop after this many accepted stage calls and save the partial prefix")
    parser.add_argument("--stage-retries", type=int, default=0,
                        help="Maximum local-Qwen repairs after a formal failed stage gate")
    parser.add_argument("--math-retries", type=int, default=1,
                        help="Maximum local-Qwen repairs of a reaction-math preflight")
    args = parser.parse_args()
    if (not args.simulator.is_file() or not 1 <= args.model_timeout <= 300
            or not 512 <= args.max_output_tokens <= 16000
            or not 1 <= args.max_stages <= 16
            or not 0 <= args.stage_retries <= 2
            or not 0 <= args.math_retries <= 2
            or (args.saved_failed_stage is not None and
                (args.saved_setup is None or args.stage_retries == 0))
            or (args.max_new_stages is not None and
                not 1 <= args.max_new_stages <= args.max_stages)):
        parser.error("Provide a simulator, 1–300 s timeout, 512–16000 output tokens, "
                     "and bounded stage counts")
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
