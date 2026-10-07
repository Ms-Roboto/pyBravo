"""Repair pinned Text2WetLab Designer drafts with local Qwen only.

The default invocation audits saved drafts without contacting the model or
writing a workflow. Pass ``--run`` to request bounded repairs. Accepted
repairs become *new*, unreviewed Designer drafts; their predecessors remain
available. No script-authored protocol steps are added.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from evaluate_text2wetlab import ECOLI_PAPER_SHA256, REVISION, TASKS, _source_bytes
from generate_text2wetlab_designer import (
    DEFAULT_API,
    MODEL_URL,
    _active_context,
    _hardware_issues,
    _node_issues,
    _post_draft,
    _scientific_pattern_issues,
)

from pybravo.evals.text2wetlab.source_context import prepare_scientific_source
from pybravo.head_mode import head_geometry_for_type
from pybravo.types import HeadType
from pybravo.workflow.drafter.llm import DrafterConfig, draft_workflow
from pybravo.workflow.drafter.scientific_patterns import audit_scientific_patterns
from pybravo.workflow.drafter.scientific_repair import (
    repair_scientific_workflow,
    validate_scientific_repair_candidate,
)
from pybravo.workflow.storage import WorkflowStorage


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pinned_source(
    task: str, provenance: dict[str, Any], *, dataset_root: Path | None,
    ecoli_paper: Path | None,
) -> tuple[str, str]:
    if (provenance.get("source_kind") != "text2wetlab_pinned_task"
            or provenance.get("source_id") != task
            or provenance.get("dataset_revision") != REVISION
            or provenance.get("model") != "qwen"):
        raise ValueError(f"{task}: saved draft is not a pinned, local-Qwen Text2WetLab source.")
    instruction_bytes = _source_bytes(task, "instruction.md", dataset_root)
    if instruction_bytes is None or _digest(instruction_bytes) != provenance.get("source_sha256"):
        raise ValueError(f"{task}: pinned instruction digest differs from the saved draft.")
    instruction = instruction_bytes.decode("utf-8")
    expected = provenance.get("source_paper_sha256") or provenance.get("paper_sha256")
    paper = None
    if expected or b"/data/paper.txt" in instruction_bytes:
        paper = (_source_bytes(task, "environment/data/paper.txt", dataset_root)
                 if ecoli_paper is None or task != "ecoli-heat-shock-transformation"
                 else ecoli_paper.read_bytes())
    if expected:
        if paper is None or _digest(paper) != expected:
            raise ValueError(f"{task}: pinned paper digest differs from the saved draft; supply the exact paper.")
    elif b"/data/paper.txt" in instruction_bytes:
        raise ValueError(f"{task}: the instruction requires a paper absent from saved provenance.")
    if task == "ecoli-heat-shock-transformation" and ecoli_paper is not None:
        if paper is None or _digest(paper) != ECOLI_PAPER_SHA256:
            raise ValueError("The supplied heat-shock paper does not match the pinned source digest.")
    excerpt = (prepare_scientific_source(
        paper.decode("utf-8"), task_instruction=instruction,
    ).text if expected and paper else "")
    return instruction, excerpt


def _authoring_workflow(saved: dict[str, Any]) -> dict[str, Any]:
    """Remove storage and release fields before showing the draft to Qwen."""
    workflow = deepcopy(saved)
    for key in ("id", "created", "modified", "protocol_generated_draft",
                "protocol_generated_root_id", "protocol_generated_provenance",
                "protocol_draft_status", "protocol_draft_issues", "protocol_draft_validation_stale"):
        workflow.pop(key, None)
    return workflow


def _upstream_tip_locations(workflow: dict[str, Any], node_id: int) -> set[str]:
    """Find the nearest proposed tip pickup on every upstream flow path."""
    graph = workflow.get("graph") or {}
    nodes = {node.get("id"): node for node in graph.get("nodes") or []
             if isinstance(node, dict)}
    parents: dict[int, set[int]] = {}
    for link in graph.get("links") or []:
        if not isinstance(link, (list, tuple)) or len(link) != 6 or link[5] != -1:
            continue
        parents.setdefault(link[3], set()).add(link[1])
    locations: set[str] = set()
    pending = list(parents.get(node_id, ()))
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        node = nodes.get(current) or {}
        kind = node.get("type")
        if kind == "tips/TipsOn":
            locations.update(_expanded_locations((node.get("properties") or {}).get("location")))
        elif kind not in {"tips/TipsOff", "flow/Start"}:
            pending.extend(parents.get(current, ()))
    return locations


def _expanded_locations(value: Any) -> list[str]:
    """Expand only explicit Bravo slots and literal iter: slot lists."""
    raw = str(value or "").strip()
    values = [part.strip() for part in (raw[5:].split(",") if raw.startswith("iter:") else [raw])]
    return ([str(int(slot)) for slot in values]
            if values and all(slot.isdigit() and 1 <= int(slot) <= 9 for slot in values)
            else [])


def _catalog_issues(workflow: dict[str, Any], context: dict[str, Any], instruction: str) -> list[dict[str, str]]:
    catalog = {str(row["id"]): row for row in context.get("labware") or []
               if isinstance(row, dict) and row.get("id")}
    issues = []
    for slot, stack in (workflow.get("deck") or {}).items():
        for index, item in enumerate(stack):
            identity = item.get("labware_id") if isinstance(item, dict) else None
            catalog_item = catalog.get(str(identity))
            if catalog_item is None:
                issues.append({
                    "severity": "error", "code": "UNRESOLVED_LABWARE",
                    "message": f"Labware {identity!r} at Bravo position {slot} is absent from the active catalog.",
                    "path": f"/deck/{slot}/{index}/labware_id",
                })
                continue
            for field in ("kind", "base_class", "wells"):
                claimed, actual = item.get(field), catalog_item.get(field)
                if claimed not in (None, "", 0) and actual not in (None, "", 0) and claimed != actual:
                    issues.append({
                        "severity": "error", "code": "CATALOG_LABWARE_METADATA_MISMATCH",
                        "message": f"Labware {identity!r} declares {field}={claimed!r}, but the active catalog records {actual!r}.",
                        "path": f"/deck/{slot}/{index}/{field}",
                    })
            if item.get("tip_definition_id") and catalog_item.get("base_class") != "tip_box":
                issues.append({
                    "severity": "error", "code": "TIP_ID_ON_NON_TIPBOX",
                    "message": f"Labware {identity!r} is not a catalog tip box; remove its proposed tip definition.",
                    "path": f"/deck/{slot}/{index}/tip_definition_id",
                })
    issues.extend(_node_issues(workflow))
    issues.extend(_hardware_issues(workflow, context, source_instruction=instruction))
    # Head-footprint checks must use catalog well counts, not an omitted or
    # model-claimed deck-item count. This copy is only for validation.
    catalog_view = deepcopy(workflow)
    for stack in (catalog_view.get("deck") or {}).values():
        for item in stack:
            actual = catalog.get(str(item.get("labware_id")))
            if actual is not None and actual.get("wells"):
                item["wells"] = actual["wells"]
    issues.extend(_scientific_pattern_issues(catalog_view, source_instruction=instruction))
    issues.extend(audit_scientific_patterns(
        catalog_view, source_instruction=instruction, head_type=context.get("head_type"),
    ))
    classes: dict[str, list[dict[str, Any]]] = {}
    for row in context.get("liquid_classes") or []:
        if isinstance(row, dict):
            for identifier in (row.get("name"), row.get("liquid_class_id")):
                if identifier:
                    matches = classes.setdefault(str(identifier), [])
                    if row not in matches:
                        matches.append(row)
    tip_pairs = {(str(row.get("labware_id")), str(row.get("tip_definition_id"))): row
                 for row in context.get("tipbox_choices") or []
                 if isinstance(row, dict) and row.get("labware_id") and row.get("tip_definition_id")}
    deck = workflow.get("deck") or {}
    move_targets = {
        target
        for move in (workflow.get("graph") or {}).get("nodes") or [] if isinstance(move, dict)
        for props in [move.get("properties") or {}]
        if move.get("type", "").startswith("plate/")
        for key in ("place_location", "destination_location", "base_location")
        for target in _expanded_locations(props.get(key))
    }
    for index, node in enumerate((workflow.get("graph") or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        kind = node.get("type", "")
        props = node.get("properties") or {}
        location = str(props.get("location", ""))
        if kind == "tips/TipsOn":
            for tip_location in _expanded_locations(location) or [location]:
                stack = deck.get(tip_location) or []
                rack = stack[-1] if stack and isinstance(stack[-1], dict) else None
                rack_id = str(rack.get("labware_id")) if rack else ""
                if not rack or (catalog.get(rack_id) or {}).get("base_class") != "tip_box":
                    issues.append({
                        "severity": "error", "code": "TIP_SUPPLY_UNPROVEN",
                        "message": f"Tips On at position {tip_location} has no catalog tip box at the top of its proposed starting deck stack. A planned move alone does not prove which tips are present.",
                        "path": f"/graph/nodes/{index}/properties/location",
                    })
                    continue
                tip_id = str(rack.get("tip_definition_id") or "")
                pair = tip_pairs.get((rack_id, tip_id))
                if not tip_id:
                    issues.append({
                        "severity": "error", "code": "TIP_DEFINITION_UNSELECTED",
                        "message": f"Propose an exact catalog tip ID for the tip box at position {tip_location}; the rack may support several independent tips. Physical loading still requires review.",
                        "path": f"/deck/{tip_location}/{len(stack) - 1}/tip_definition_id",
                    })
                elif pair is None:
                    issues.append({
                        "severity": "error", "code": "INCOMPATIBLE_TIPBOX_TIP_PAIR",
                        "message": f"The active head has no catalog pairing for rack {rack_id!r} with tip {tip_id!r}.",
                        "path": f"/deck/{tip_location}/{len(stack) - 1}/tip_definition_id",
                    })
                elif pair.get("execution_ready") is not True:
                    issues.append({
                        "severity": "error", "code": "TIP_PAIR_METADATA_INCOMPLETE",
                        "message": f"Rack {rack_id!r} with tip {tip_id!r} lacks metadata required for hardware execution; it remains a planning option only.",
                        "path": f"/deck/{tip_location}/{len(stack) - 1}/tip_definition_id",
                    })
        if kind not in {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}:
            continue
        for liquid_location in _expanded_locations(location) or [location]:
            liquid_stack = deck.get(liquid_location) or []
            if not liquid_stack:
                planned_move = liquid_location in move_targets
                issues.append({
                    "severity": "warning" if planned_move else "error",
                    "code": "LIQUID_LABWARE_MOVE_REVIEW" if planned_move else "LIQUID_LABWARE_UNPROVEN",
                    "message": (
                        f"Liquid action at position {liquid_location} depends on a planned plate move; review its order and occupancy."
                        if planned_move else f"Liquid action at position {liquid_location} has no proposed starting labware or plate-move target."
                    ),
                    "path": f"/graph/nodes/{index}/properties/location",
                })
            elif isinstance(liquid_stack[-1], dict):
                top_id = str(liquid_stack[-1].get("labware_id"))
                if (catalog.get(top_id) or {}).get("base_class") == "tip_box" and liquid_location not in move_targets:
                    issues.append({
                        "severity": "error", "code": "LIQUID_TARGET_IS_TIPBOX",
                        "message": f"Liquid action at position {liquid_location} targets a catalog tip box instead of source or destination labware.",
                        "path": f"/graph/nodes/{index}/properties/location",
                    })
        matching_classes = classes.get(str(props.get("liquid_class"))) or []
        if not matching_classes:
            continue  # _hardware_issues reports an unavailable class.
        if len(matching_classes) != 1:
            issues.append({
                "severity": "error", "code": "AMBIGUOUS_LIQUID_CLASS",
                "message": "Several active catalog liquid classes share this label; select an exact liquid_class_id.",
                "path": f"/graph/nodes/{index}/properties/liquid_class",
            })
            continue
        liquid_class = matching_classes[0]
        if ((context.get("machine_id") and liquid_class.get("machine_id")
             and liquid_class["machine_id"] != context["machine_id"])
                or (context.get("head_type") and liquid_class.get("head_type")
                    and liquid_class["head_type"] != context["head_type"])):
            issues.append({
                "severity": "error", "code": "LIQUID_CLASS_PROFILE_MISMATCH",
                "message": "The selected liquid class belongs to another machine or head profile.",
                "path": f"/graph/nodes/{index}/properties/liquid_class",
            })
        try:
            volume = float(props.get("volume"))
        except (TypeError, ValueError):
            continue  # The graph validator checks missing volume separately.
        capacity = liquid_class.get("tip_capacity_ul")
        points = (liquid_class.get("equation") or {}).get("control_points") or []
        limits = [float(capacity)] if isinstance(capacity, (int, float)) else []
        calibrated = [float(point["desired_ul"]) for point in points
                      if isinstance(point, dict) and isinstance(point.get("desired_ul"), (int, float))]
        if calibrated:
            limits.append(max(calibrated))
        if limits and volume > min(limits) + 1e-6:
            issues.append({
                "severity": "error", "code": "LIQUID_CLASS_VOLUME_OUT_OF_RANGE",
                "message": f"{volume:g} µL exceeds the selected class's tip or calibration range; split the transfer or review a compatible method.",
                "path": f"/graph/nodes/{index}/properties/volume",
            })
        tip_id = liquid_class.get("tip_id")
        if tip_id and not any(
            row.get("tip_definition_id") == tip_id and row.get("execution_ready") is True
            for row in tip_pairs.values()
        ):
            issues.append({
                "severity": "error", "code": "LIQUID_CLASS_TIP_NOT_READY",
                "message": f"The selected class requires {tip_id}, but this head has no execution-ready catalog tip-box pairing for it.",
                "path": f"/graph/nodes/{index}/properties/liquid_class",
            })
        for tip_location in _upstream_tip_locations(workflow, node.get("id")):
            stack = deck.get(tip_location) or []
            rack = stack[-1] if stack and isinstance(stack[-1], dict) else None
            if rack is None:
                continue  # TIP_SUPPLY_UNPROVEN is reported at Tips On.
            selected_tip = str(rack.get("tip_definition_id") or "")
            if tip_id and selected_tip and selected_tip != str(tip_id):
                issues.append({
                    "severity": "error", "code": "LIQUID_CLASS_TIP_MISMATCH",
                    "message": f"The class requires {tip_id!r}, but the upstream rack at position {tip_location} proposes {selected_tip!r}.",
                    "path": f"/graph/nodes/{index}/properties/liquid_class",
                })
            pair = tip_pairs.get((str(rack.get("labware_id")), selected_tip))
            pair_capacity = pair.get("tip_capacity_ul") if pair else None
            if isinstance(pair_capacity, (int, float)) and volume > pair_capacity + 1e-6:
                issues.append({
                    "severity": "error", "code": "TIP_VOLUME_OUT_OF_RANGE",
                    "message": f"{volume:g} µL exceeds the {pair_capacity:g} µL tip proposed at position {tip_location}.",
                    "path": f"/graph/nodes/{index}/properties/volume",
                })
    # OT-2 slots have no automatic Bravo equivalent. This is a required
    # scientist review, but cannot be resolved by graph authoring alone.
    for issue in issues:
        if issue["code"] == "OT2_DECK_REMAP_REVIEW":
            issue["severity"] = "warning"
    return issues


def _hardware_option_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Join class/tip/rack records before presenting hardware choices to Qwen.

    A catalog pairing is only a geometric/capacity possibility. It never
    certifies the reagent method, deck contents, teachpoints, or a physical run.
    """
    head_name = context.get("head_type")
    head = HeadType.__members__.get(head_name) if isinstance(head_name, str) else None
    geometry = head_geometry_for_type(head) if head else None
    ready_pairs = [item for item in context.get("tipbox_choices") or []
                   if isinstance(item, dict) and item.get("execution_ready") is True]
    planning_pairs = [item for item in context.get("tipbox_choices") or []
                      if isinstance(item, dict) and item.get("execution_ready") is not True]

    def positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if 0 < number < float("inf") else None

    combinations: list[dict[str, Any]] = []
    class_limitations: list[dict[str, str]] = []
    for liquid_class in context.get("liquid_classes") or []:
        if not isinstance(liquid_class, dict):
            continue
        identifier = str(liquid_class.get("liquid_class_id") or liquid_class.get("name") or "")
        tip_id = str(liquid_class.get("tip_id") or "")
        if not identifier:
            continue
        if ((context.get("machine_id") and liquid_class.get("machine_id")
             and liquid_class["machine_id"] != context["machine_id"])
                or (head_name and liquid_class.get("head_type")
                    and liquid_class["head_type"] != head_name)):
            class_limitations.append({"liquid_class_id": identifier,
                                      "reason": "class belongs to another machine or head"})
            continue
        matches = [pair for pair in ready_pairs if pair.get("tip_definition_id") == tip_id]
        if not matches:
            class_limitations.append({"liquid_class_id": identifier,
                                      "reason": "no execution-ready catalog tip-box pairing for this class's tip"})
            continue
        points = (liquid_class.get("equation") or {}).get("control_points") or []
        calibrated = [positive(point.get("desired_ul")) for point in points
                      if isinstance(point, dict)]
        calibrated = [value for value in calibrated if value is not None]
        for pair in matches:
            limits = [positive(liquid_class.get("tip_capacity_ul")),
                      positive(pair.get("tip_capacity_ul")),
                      max(calibrated) if calibrated else None]
            limits = [value for value in limits if value is not None]
            if not limits:
                class_limitations.append({"liquid_class_id": identifier,
                                          "reason": "tip or calibration volume limit is unknown"})
                continue
            combinations.append({
                "liquid_class_id": identifier,
                "liquid_class_name": liquid_class.get("name"),
                "tip_definition_id": tip_id,
                "tip_box_labware_id": pair.get("labware_id"),
                "max_single_stroke_ul": min(limits),
                "required_head_mode": pair.get("required_head_mode"),
            })
    return {
        "machine_id": context.get("machine_id"), "head_type": head_name,
        "head_footprint": ({"rows": geometry.rows, "columns": geometry.columns,
                            "channels": geometry.rows * geometry.columns,
                            "pitch_x_mm": geometry.pitch_x_mm,
                            "pitch_y_mm": geometry.pitch_y_mm} if geometry else None),
        "catalog_supported_liquid_pairings": combinations,
        "planning_only_liquid_classes": class_limitations,
        "planning_only_tip_boxes": [
            {key: item.get(key) for key in ("labware_id", "tip_definition_id", "missing_metadata")}
            for item in planning_pairs
        ],
        "labware_options": [
            {key: item.get(key) for key in ("id", "name", "kind", "base_class", "wells")}
            for item in context.get("labware") or [] if isinstance(item, dict)
        ],
    }


def _catalog_brief(context: dict[str, Any]) -> str:
    """Small joined active-profile choices, never a claim of physical loading."""
    choices = _hardware_option_summary(context)
    return (
        "\n\nACTIVE BRAVO PROFILE OPTIONS (read-only catalog facts; no labware or tips "
        "are confirmed loaded and no liquid method is declared qualified):\n"
        + json.dumps(choices, ensure_ascii=False, separators=(",", ":"))
        + "\nFor a liquid primitive, choose a complete entry from "
        "catalog_supported_liquid_pairings: use its exact class ID, tip ID, "
        "rack ID, and no more than max_single_stroke_ul per aspiration or "
        "dispense. A catalog pairing still requires reagent-method and physical "
        "loading review. Planning-only entries are not executable options. "
        "A full-head mode cannot address labware with fewer wells than the "
        "head_footprint.channels count; use an explicitly supported partial "
        "mode only after checking pitch and geometry. For each proposed tip "
        "box, set tip_definition_id to the paired independent tip ID. If no "
        "catalog-supported pairing or geometry can perform a stage, preserve "
        "its scientific intent in an explicit manual handoff and a visible "
        "review issue. Never manufacture instrument settings, consumables, "
        "or hardware readiness."
    )


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({(item.get("code"), item.get("path"), item.get("message")): item
                 for item in issues}.values())


def _review_issues(workflow: dict[str, Any], *, context: dict[str, Any],
                   instruction: str) -> list[dict[str, Any]]:
    def extra(candidate: dict[str, Any]) -> list[dict[str, str]]:
        return _catalog_issues(candidate, context, instruction)

    return _dedupe_issues(validate_scientific_repair_candidate(
        workflow, instruction=instruction, head_type=context.get("head_type"),
        extra_validator=extra,
    ))


def _error_count(issues: list[dict[str, Any]]) -> int:
    return sum(issue.get("severity") == "error" for issue in issues)


def _scientific_actions_preserved(original: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """An incomplete repair cannot improve merely by deleting pipetting."""
    action_types = {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}
    def present(workflow: dict[str, Any]) -> set[str]:
        return {node.get("type") for node in (workflow.get("graph") or {}).get("nodes") or []
                if isinstance(node, dict) and node.get("type") in action_types}
    return present(original) <= present(candidate)


def _submitted_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain a visible truncation notice within the API's 100-issue limit."""
    if len(issues) <= 100:
        return issues
    return issues[:99] + [{
        "severity": "warning", "code": "REVIEW_ISSUES_TRUNCATED",
        "message": f"{len(issues) - 99} additional review issues were omitted from this Designer display; see the local repair record before review.",
        "path": "/graph",
    }]


def _select_workflows(storage: WorkflowStorage, *, tasks: list[str] | None,
                      workflow_id: str | None) -> list[dict[str, Any]]:
    if workflow_id:
        selected = storage.get_workflow(workflow_id)
        if selected is None:
            raise ValueError(f"Saved workflow {workflow_id!r} was not found.")
        if selected.get("protocol_generated_draft") is not True:
            raise ValueError(f"Saved workflow {workflow_id!r} is not a generated draft.")
        return [selected]
    newest: dict[str, dict[str, Any]] = {}
    for row in storage.list_workflows():
        if row.get("protocol_generated_draft") is not True:
            continue
        saved = storage.get_workflow(row["id"])
        if saved is None:
            continue
        provenance = saved.get("protocol_generated_provenance") or {}
        task = provenance.get("source_id")
        if task not in TASKS or (tasks and task not in tasks):
            continue
        prior = newest.get(task)
        if prior is None or saved.get("modified", "") > prior.get("modified", ""):
            newest[task] = saved
    selected = [newest[task] for task in (tasks or TASKS) if task in newest]
    missing = set(tasks or TASKS) - set(newest)
    if missing:
        raise ValueError(f"No saved generated draft for: {', '.join(sorted(missing))}.")
    return selected


async def repair_saved_draft(
    saved: dict[str, Any], *, context: dict[str, Any], output_dir: Path,
    api_url: str, dataset_root: Path | None = None, ecoli_paper: Path | None = None,
    execute: bool = False, max_attempts: int = 2, max_tokens: int = 6500,
    timeout_s: int = 300, save_improving_candidate: bool = False,
    drafter=None,
) -> dict[str, Any]:
    """Audit or repair one stored Qwen draft without modifying the original."""
    if saved.get("protocol_generated_draft") is not True:
        raise ValueError("Scientific repair requires an unreviewed generated draft.")
    provenance = saved.get("protocol_generated_provenance") or {}
    task = provenance.get("source_id")
    if task not in TASKS:
        raise ValueError("The draft is not a recognized pinned Text2WetLab task.")
    instruction, excerpt = _pinned_source(
        task, provenance, dataset_root=dataset_root, ecoli_paper=ecoli_paper,
    )
    original = _authoring_workflow(saved)
    initial_issues = _review_issues(original, context=context, instruction=instruction)
    record: dict[str, Any] = {
        "task": task, "source_workflow_id": saved["id"], "source_name": saved.get("name"),
        "source_sha256": provenance["source_sha256"],
        "initial_issue_count": len(initial_issues), "initial_error_count": _error_count(initial_issues),
        "initial_issue_codes": sorted({issue["code"] for issue in initial_issues}),
        "status": "audit_only",
    }
    if not execute or not _error_count(initial_issues):
        if execute:
            record["status"] = "no_repair_needed"
        return record
    if not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5.")
    os.environ["PYBRAVO_DRAFTER_BASE_URL"] = MODEL_URL
    os.environ["PYBRAVO_DRAFTER_TIMEOUT"] = str(timeout_s)
    os.environ["PYBRAVO_DRAFTER_HTTP_RETRIES"] = "0"
    def extra(candidate: dict[str, Any]) -> list[dict[str, str]]:
        return _catalog_issues(candidate, context, instruction)

    config = DrafterConfig(
        provider="local", model="qwen", max_tokens=max_tokens,
        temperature=0, max_repair_attempts=0,
    )
    catalog_brief = _catalog_brief(context)

    async def catalog_grounded_drafter(prompt: str, **kwargs):
        return await (drafter or draft_workflow)(prompt + catalog_brief, **kwargs)

    repaired = await repair_scientific_workflow(
        instruction=instruction, source_excerpt=excerpt,
        current_workflow=original, validator_issues=initial_issues,
        config=config, provenance=provenance, head_type=context.get("head_type"),
        max_attempts=max_attempts, extra_validator=extra, drafter=catalog_grounded_drafter,
    )
    candidate = repaired.workflow if repaired.passed_checks else repaired.candidate_workflow
    candidate_issues = (_review_issues(candidate, context=context, instruction=instruction)
                        if candidate is not None else repaired.issues)
    record.update({
        "status": "repair_failed", "attempts": repaired.attempts,
        "passed_checks": repaired.passed_checks,
        "candidate_issue_count": len(candidate_issues),
        "candidate_error_count": _error_count(candidate_issues),
        "candidate_issue_codes": sorted({issue["code"] for issue in candidate_issues}),
        "candidate_action_types_preserved": (candidate is not None
                                              and _scientific_actions_preserved(original, candidate)),
        "candidate_sha256": (_digest(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode("utf-8"))
                             if candidate is not None else None),
    })
    task_dir = output_dir / task
    task_dir.mkdir(parents=True, exist_ok=True)
    if candidate is not None:
        (task_dir / "repair_candidate.json").write_text(
            json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
    (task_dir / "repair_record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    improving = (candidate is not None
                 and _error_count(candidate_issues) < _error_count(initial_issues)
                 and _scientific_actions_preserved(original, candidate))
    if candidate is None or not (repaired.passed_checks or save_improving_candidate and improving):
        return record
    to_save = _authoring_workflow(candidate)
    if not str(to_save.get("name", "")).startswith("Text2WetLab · "):
        to_save["name"] = f"Text2WetLab · {to_save['name']}"
    # Lineage and model-output digest stay in a local trace. The persisted
    # provenance remains the original pinned source plus the trace digest.
    repair_trace = {**record, "model": repaired.model, "model_url": MODEL_URL,
                    "original_provenance": repaired.provenance}
    trace_bytes = (json.dumps(repair_trace, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    (task_dir / "repair_trace.json").write_bytes(trace_bytes)
    saved_provenance = deepcopy(provenance)
    saved_provenance["generation_trace_sha256"] = _digest(trace_bytes)
    posted = _post_draft(api_url, to_save, saved_provenance, _submitted_issues(candidate_issues))
    record.update({
        "status": "saved_unreviewed" if repaired.passed_checks else "saved_unreviewed_incomplete",
        "workflow_id": posted["workflow_id"], "designer_url": posted["url"],
        "generation_trace_sha256": saved_provenance["generation_trace_sha256"],
    })
    (task_dir / "repair_record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


async def _main(args: argparse.Namespace) -> int:
    storage = WorkflowStorage(args.workflows_dir)
    context = _active_context(args.api_url)
    selected = _select_workflows(storage, tasks=args.task, workflow_id=args.workflow_id)
    results = []
    for saved in selected:
        try:
            result = await repair_saved_draft(
                saved, context=context, output_dir=args.output_dir,
                api_url=args.api_url, dataset_root=args.dataset_root,
                ecoli_paper=args.ecoli_paper, execute=args.run,
                max_attempts=args.max_attempts, max_tokens=args.max_tokens,
                timeout_s=args.timeout,
                save_improving_candidate=args.save_improving_candidate,
            )
        except Exception as exc:
            result = {"task": (saved.get("protocol_generated_provenance") or {}).get("source_id"),
                      "source_workflow_id": saved.get("id"), "status": "failed",
                      "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(json.dumps(result), flush=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0 if all(item["status"] in {"audit_only", "no_repair_needed", "saved_unreviewed", "saved_unreviewed_incomplete"}
                    for item in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=TASKS, action="append", help="Repeat to select tasks; default is all seven")
    parser.add_argument("--workflow-id", help="Repair this exact saved generated draft")
    parser.add_argument("--workflows-dir", type=Path)
    parser.add_argument("--dataset-root", type=Path, help="Local checkout of the pinned Hugging Face dataset")
    parser.add_argument("--ecoli-paper", type=Path, help="Exact paper used for the heat-shock draft")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/pybravo-text2wetlab-designer-repair"))
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--run", action="store_true", help="Call local Qwen and save a passing unreviewed repair")
    parser.add_argument("--save-improving-candidate", action="store_true",
                        help="With --run, save an incomplete model candidate only if deterministic error count improves")
    parser.add_argument("--max-attempts", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--max-tokens", type=int, default=6500)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.save_improving_candidate and not args.run:
        parser.error("--save-improving-candidate requires --run")
    if args.workflow_id and args.task:
        parser.error("--workflow-id and --task cannot be combined")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
