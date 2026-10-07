"""One-shot local-Qwen ActionPlan experiment for a pinned Text2WetLab task.

This is a separate OT-2 benchmark experiment, not a Bravo workflow generator or
an official Harbor score. Qwen supplies every ordered experimental action;
the deterministic compiler supplies only code syntax and catalog validation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

if __package__ in {None, ""}:
    # Direct `python scripts/...py` puts scripts/, rather than the repository,
    # on sys.path. Keep the experiment runnable in both forms.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pybravo.evals.text2wetlab.action_ir import (
    ActionPlanError,
    LabwareFacts,
    PipetteFacts,
    SourceSpan,
    compile_actions,
)
from pybravo.evals.text2wetlab.adapter import ProtocolValidationError, validate_ot2_source
from pybravo.evals.text2wetlab.geometry import labware_geometry_context
from pybravo.evals.text2wetlab.source_context import prepare_scientific_source
from pybravo.evals.text2wetlab.trusted_catalog import load_trusted_catalog
from pybravo.workflow.protocols.llm import LocalLLMConfig, StructuredResponse, structured_json
from scripts import evaluate_text2wetlab as runner
from scripts.experiment_text2wetlab_science_patch import _check_candidate

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labware": {"type": "array", "items": {"type": "object"}},
        "modules": {"type": "array", "items": {"type": "object"}},
        "pipettes": {"type": "array", "items": {"type": "object"}},
        "actions": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["labware", "pipettes", "actions"],
    "additionalProperties": False,
}

_SYSTEM = """You are the local OT-2 action planner. Return one JSON ActionPlan, never Python code, explanation, or a benchmark answer from memory. The user instruction and any supplied paper lines are the only evidence for experimental content. You must supply all reagent choices, labware and instrument selections, deck slots, volumes, well mappings, tip actions, waits, and module actions in execution order. The compiler does not invent or insert any experimental step. Cite at least one exact line ID in evidence_refs for every action, including every action inside a loop. Citations locate text; they do not excuse an unsupported experimental choice. If a choice is unspecified, make the narrowest workable assumption while preserving the fixed deck and inventory. Use only the read-only installed catalog names and wells below. Do not create a source reagent, position, or refill that the instruction does not allow.

ActionPlan keys: labware, modules (optional), pipettes, actions.
Labware item: id, load_name, exactly one of slot or module_id, optional label. Preserve every explicitly required deck label. Pipette item: id, model, mount (left/right), tip_rack_ids.
Every action has kind and evidence_refs (array of provided line IDs). Supported actions:
- pickup/drop: pipette
- aspirate/dispense: pipette, labware, well, volume_ul
- mix: pipette, labware, well, cycles, volume_ul
- delay: seconds; pause: message
- refill_tips: pipette, message (only when expressly permitted and the rack is exhausted)
- set_temperature, set_block_temperature, set_lid_temperature: module, celsius; block may have hold_seconds
- open_lid/close_lid: module; magnet_engage: module, height_from_base_mm; magnet_disengage: module
- for_each: evidence_refs, either bindings (array of dictionaries mapping symbolic well names to actual well names) OR selector, and actions (ordered primitive actions). In a loop action, a well is exactly "$binding_name". A selector can use trusted catalog wells: {"kind":"catalog_wells","series":[{"binding":"well_name","labware":"labware_id","mode":"all" or "column_anchors","columns":[...] }],"relation":"zip" or "same_name"}. Choose only the wells named by the task; a selector over all plate wells is not a shortcut for a stated subset.

One tip must be attached before liquid actions and dropped empty. Never dispense more than aspirated or exceed the smaller of pipette and tip capacities. Do not use a multichannel pipette on a 1-well reservoir or irregular rack. Do not cross-contaminate distinct samples. Count pickups before adding a refill action. A protocol's final reaction volume must equal its actual additions, including any premix. Output only the final coherent actions, not an exploratory attempt followed by a correction."""


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _line_spans(instruction: str, paper: str | None) -> tuple[dict[str, SourceSpan], str]:
    """Offer short, exact passages that Qwen may cite by stable line ID."""
    spans: dict[str, SourceSpan] = {}
    rendered: list[str] = []
    for source_name, source in (("instruction", instruction), ("paper", paper)):
        if source is None:
            continue
        prefix = "task" if source_name == "instruction" else "paper"
        for line_number, match in enumerate(re.finditer(r"(?m)^[^\n]*\S[^\n]*$", source), 1):
            span_id = f"{prefix}.L{line_number}"
            spans[span_id] = SourceSpan(source_name, match.start(), match.end())
            rendered.append(f"[{span_id}] {match.group()}")
    return spans, "\n".join(rendered)


def _catalog_prompt(
    labware: dict[str, LabwareFacts], pipettes: dict[str, PipetteFacts],
) -> str:
    display = {"labware": {}, "pipettes": {}}
    for name, facts in labware.items():
        display["labware"][name] = {
            "well_count": len(facts.wells),
            "first_column": [well for well in facts.ordered_wells
                             if re.fullmatch(r"[A-Z]+1", well)][:8],
            "last_well": facts.ordered_wells[-1],
            "tiprack": facts.is_tiprack,
            "tip_capacity_ul": facts.tip_capacity_ul,
            "multichannel_column_anchors": sorted(facts.multichannel_anchor_wells),
            **({"all_wells": facts.ordered_wells} if len(facts.wells) <= 24 else {}),
        }
    for name, facts in pipettes.items():
        display["pipettes"][name] = {
            "min_volume_ul": facts.min_volume_ul,
            "max_volume_ul": facts.max_volume_ul,
            "channels": facts.channels,
        }
    return json.dumps(display, ensure_ascii=False, sort_keys=True)


def _canonicalize_deck_slots(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert only exact decimal slot strings; never change an experimental value."""
    canonical = deepcopy(payload)
    changes: list[str] = []
    for section in ("labware", "modules"):
        items = canonical.get(section)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            slot = item.get("slot")
            if isinstance(slot, str) and re.fullmatch(r"(?:[1-9]|1[01])", slot):
                item["slot"] = int(slot)
                changes.append(f"/{section}/{index}/slot: {slot!r} -> {item['slot']}")
    return canonical, changes


def _check_compiled_plan(
    payload: dict[str, Any], *, instruction: str, paper: str | None,
    spans: dict[str, SourceSpan], labware: dict[str, LabwareFacts],
    pipettes: dict[str, PipetteFacts], simulator: Path, task: str,
    task_dir: Path, dataset_root: Path | None, labware_dir: Path | None,
    refill_authorized: bool,
) -> dict[str, Any]:
    """Compile first, then apply the existing independent pinned gates."""
    try:
        code = compile_actions(
            payload, labware_catalog=labware, pipette_catalog=pipettes,
            source_spans=spans, instruction=instruction, paper=paper,
            refill_authorized=refill_authorized,
        )
    except (ActionPlanError, ValueError) as exc:
        return {"status": "action_plan_rejected", "detail": str(exc),
                "official_score": None}
    geometry = labware_geometry_context(
        instruction, simulator_command=simulator, labware_dir=labware_dir,
    )
    try:
        validate_ot2_source(code, geometry=geometry)
    except ProtocolValidationError as exc:
        return {"status": "static_rejected", "detail": str(exc),
                "official_score": None}
    protocol = task_dir / "protocol.py"
    protocol.write_text(code, encoding="utf-8")
    checked = _check_candidate(
        task, code, protocol, task_dir, simulator, dataset_root,
        labware_dir, instruction,
    )
    science = checked.get("local_rubric_audit") or {}
    failed = [
        {"rubric_item": item.get("id"), "check": check.get("name")}
        for item in science.get("items", []) for check in item.get("checks", [])
        if check.get("status") == "failed"
    ] if isinstance(science, dict) else []
    return {
        "status": ("local_science_failed" if checked["status"] == "mechanical_gates_passed"
                   and failed else checked["status"]),
        "protocol_path": str(protocol),
        "protocol_sha256": _sha(code), "pinned_gates": checked,
        "local_rubric_status": science.get("status") if isinstance(science, dict) else None,
        "failed_observable_science_checks": failed, "official_score": None,
    }


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
    assert raw_instruction is not None
    instruction = raw_instruction.decode("utf-8")
    source_paper = runner._source_bytes(args.task, "environment/data/paper.txt", args.dataset_root)
    if args.paper_override is not None:
        override = args.paper_override.read_bytes()
        expected = (runner.ECOLI_PAPER_SHA256
                    if args.task == "ecoli-heat-shock-transformation"
                    else _sha(source_paper) if source_paper is not None else None)
        if expected is None or _sha(override) != expected:
            return {"status": "paper_override_digest_mismatch", "official_score": None,
                    "expected_paper_sha256": expected}
        source_paper = override
    paper = source_paper.decode("utf-8") if source_paper else None
    if b"/data/paper.txt" in raw_instruction and paper is None:
        return {"status": "blocked_missing_source_paper", "official_score": None}
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
    trace: dict[str, Any] = {
        "task": args.task, "dataset": runner.DATASET, "revision": runner.REVISION,
        "instruction_sha256": _sha(raw_instruction),
        "paper_sha256": _sha(source_paper) if source_paper else None,
        "paper_excerpt_sha256": excerpt.excerpt_sha256 if excerpt else None,
        "catalog_load_names": sorted(labware), "catalog_pipette_names": sorted(pipettes),
        "official_score": None,
    }
    (output / "instruction.md").write_bytes(raw_instruction)
    if source_paper:
        (output / "paper.txt").write_bytes(source_paper)
    config = replace(LocalLLMConfig.from_env(), timeout_s=args.model_timeout,
                     max_tokens=args.max_output_tokens, retries=0,
                     enable_thinking=False)
    if config.base_url != "http://sparky.local:8000/v1":
        raise ValueError("This experiment permits only local Qwen at sparky.local:8000")
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "Pinned task and scientific source lines:\n" + cited_lines
         + "\n\nInstalled read-only OT-2 catalog:\n" + _catalog_prompt(labware, pipettes)},
    ]
    try:
        response = await completion(
            messages, _PLAN_SCHEMA, config=config, schema_name="ot2_action_plan",
        )
    except Exception as exc:
        trace.update(status="model_failed", detail=f"{type(exc).__name__}: {exc}")
        return trace
    trace["model"] = response.metadata.get("model")
    trace["model_elapsed_s"] = response.metadata.get("elapsed_s")
    trace["model_usage"] = response.metadata.get("usage")
    trace["model_payload_sha256"] = _sha(json.dumps(response.payload, sort_keys=True))
    (output / "qwen_action_plan.json").write_text(
        json.dumps(response.payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    canonical, conversions = _canonicalize_deck_slots(response.payload)
    trace["syntax_canonicalizations"] = conversions
    (output / "compiler_input.json").write_text(
        json.dumps(canonical, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    result = _check_compiled_plan(
        canonical, instruction=instruction, paper=paper_for_model,
        spans=spans, labware=labware, pipettes=pipettes,
        simulator=args.simulator, task=args.task, task_dir=output,
        dataset_root=args.dataset_root, labware_dir=labware_dir,
        refill_authorized="reset_tipracks()" in instruction,
    )
    trace.update(result)
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=runner.TASKS, required=True)
    parser.add_argument("--simulator", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--paper-override", type=Path,
                        help="Exact pinned paper bytes when the dataset omits the source")
    parser.add_argument("--model-timeout", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    args = parser.parse_args()
    if (not args.simulator.is_file() or not 1 <= args.model_timeout <= 300
            or not 512 <= args.max_output_tokens <= 16000):
        parser.error("Provide a simulator, 1–300 s model timeout, and 512–16000 output tokens")
    try:
        trace = asyncio.run(run_experiment(args))
    except (OSError, RuntimeError, ValueError) as exc:
        trace = {"task": args.task, "status": "experiment_failed",
                 "detail": f"{type(exc).__name__}: {exc}", "official_score": None}
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    trace_path = output / "action_ir_trace.json"
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print(json.dumps({"trace": str(trace_path), "status": trace["status"],
                      "official_score": None}))
    return 0 if trace["status"] == "mechanical_gates_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
