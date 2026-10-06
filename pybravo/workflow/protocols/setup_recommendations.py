"""Explainable setup proposals derived from a capability manifest and a draft.

This module never edits a protocol or records a scientist decision. It applies
only the rules published by the active capability manifest; validation and
scientist review remain separate gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from pybravo.head_mode import head_geometry_for_type, normalize_head_mode, plate_footprint_wells
from pybravo.types import HeadType

from .validation import well_cell


def _data(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _set_up_value(setup: Mapping[str, Any], path: str) -> Any:
    node: Any = setup
    for part in path.split("/")[2:]:
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == []


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _steps(plan: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Return liquid steps; a repeat keeps consumable ordering unresolved."""
    found: list[dict[str, Any]] = []
    has_repeat = False
    expanded_count: int | None = 0

    def visit(items: Any, prefix: str, multiplier: int) -> None:
        nonlocal has_repeat, expanded_count
        for index, step in enumerate(_rows(items)):
            path = f"{prefix}/{index}"
            if step.get("kind") == "repeat":
                has_repeat = True
                count = step.get("repeat")
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    expanded_count = None
                    count = 1
                visit(step.get("steps"), path + "/steps", multiplier * count)
            elif step.get("kind") in {"transfer", "mix"}:
                found.append({**step, "_path": path})
                if expanded_count is not None:
                    expanded_count += multiplier

    visit(plan.get("steps"), "/steps", 1)
    return found, has_repeat, expanded_count


def _head(manifest: Mapping[str, Any]) -> HeadType | None:
    name = _data(manifest.get("machine")).get("head_type")
    try:
        head = HeadType[name] if isinstance(name, str) else HeadType(name)
    except (KeyError, TypeError, ValueError):
        return None
    return head if head.is_disposable else None


def setup_plan_fingerprint(plan: dict) -> str:
    """Bind setup authorization to the current materials and ordered steps."""
    data = _data(plan)
    relevant = {"materials": _rows(data.get("materials")), "steps": _rows(data.get("steps"))}
    payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scientist_bool(plan: Mapping[str, Any], path: str) -> bool | None:
    fingerprint = setup_plan_fingerprint(dict(plan))
    for decision in reversed(_rows(plan.get("decisions"))):
        if decision.get("path") == path and decision.get("actor") == "scientist":
            value = _data(decision.get("value"))
            if value.get("plan_fingerprint") == fingerprint and isinstance(value.get("authorized"), bool):
                return value["authorized"]
            return None
    return None


def _four_source_quadrant_scope(
    steps: list[dict[str, Any]], materials: Mapping[str, dict[str, Any]],
    labware: Mapping[str, dict[str, Any]], cited_text: str,
) -> bool:
    """Recognize only the exact eight-transfer request as a scope heuristic."""
    if len(steps) != 8 or any(step.get("kind") != "transfer" or
                              not re.fullmatch(r"/steps/\d+", step.get("_path", "")) for step in steps):
        return False
    sources = {step.get("source") for step in steps}
    destinations = {step.get("destination") for step in steps}
    if len(sources) != 4 or len(destinations) != 2 or None in sources | destinations:
        return False
    if ({(step.get("source"), step.get("destination")) for step in steps}
            != {(source_id, destination_id) for source_id in sources for destination_id in destinations}):
        return False
    source_grids = [labware.get(materials.get(mid, {}).get("labware_id"), {}) for mid in sources]
    destination_grids = [labware.get(materials.get(mid, {}).get("labware_id"), {}) for mid in destinations]
    if not all(row.get("wells") == 384 and row.get("rows") == 16 and row.get("cols") == 24
               for row in source_grids):
        return False
    if not all(row.get("wells") == 1536 and row.get("rows") == 32 and row.get("cols") == 48
               for row in destination_grids):
        return False
    if any(step.get("source_anchor") != "A1" for step in steps):
        return False
    quadrant_by_source: dict[str, str] = {}
    for step in steps:
        source_id, anchor = step["source"], step.get("destination_anchor")
        if anchor not in {"A1", "A2", "B1", "B2"}:
            return False
        if source_id in quadrant_by_source and quadrant_by_source[source_id] != anchor:
            return False
        quadrant_by_source[source_id] = anchor
    if set(quadrant_by_source.values()) != {"A1", "A2", "B1", "B2"}:
        return False
    # Matching plate counts and quadrant geometry do not establish intent when
    # the cited procedure explicitly rejects or only discusses that transfer.
    if re.search(
        r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|not\s+to|no)\s+"
        r"(?:\w+\s+){0,5}transfer\b",
        cited_text, re.IGNORECASE,
    ):
        return False
    if any(
        re.search(r"\b(?:transfer|quadrants?|layout)\b", sentence, re.IGNORECASE)
        and re.search(r"\b(?:hypothetical|possible\s+future|future\s+layout|only\s+a\s+possible)\b",
                      sentence, re.IGNORECASE)
        for sentence in re.split(r"[.!?;]", cited_text)
    ):
        return False
    return bool(
        re.search(r"\b(?:4|four)\b[^.]*\b384\b", cited_text, re.IGNORECASE)
        and re.search(r"\b(?:2|two)\b[^.]*\b1536\b", cited_text, re.IGNORECASE)
        and re.search(r"\bquadrants?\b", cited_text, re.IGNORECASE)
        and re.search(r"\btransfer\b", cited_text, re.IGNORECASE)
        and re.search(r"\b(?:each|every)\s+plate\b", cited_text, re.IGNORECASE)
    )


def _full_head_footprint(
    liquid_steps: list[dict[str, Any]], materials: dict[str, dict[str, Any]],
    manifest: Mapping[str, Any], head: HeadType | None, source: Mapping[str, Any], plan: Mapping[str, Any],
) -> tuple[bool | None, list[dict[str, Any]], list[str]]:
    if not liquid_steps or head is None:
        return None, [], []
    geometry = head_geometry_for_type(head)
    mode = normalize_head_mode(head, "all_barrels", "back_left")
    labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    incompatible = {(row.get("target_labware_id"), row.get("tip_definition_id"))
                    for row in _rows(manifest.get("tip_plate_compatibility"))
                    if row.get("compatible") is False}
    paragraphs = {row.get("id") or row.get("paragraph_id"): str(row.get("text") or "")
                  for row in _rows(source.get("paragraphs"))}
    pairs = {(row.get("labware_id"), row.get("tip_definition_id")): row
             for row in _rows(manifest.get("tipbox_choices"))}
    tips = [row for row in materials.values() if row.get("role") == "tips"]
    blocks: list[str] = []
    evidence: list[dict[str, Any]] = []
    if not tips:
        blocks.append("Select a tip-rack material with an exact catalog rack/tip pair.")
    for rack in tips:
        pair = pairs.get((rack.get("labware_id"), rack.get("tip_definition_id")))
        if pair is None:
            blocks.append(f"Tip rack {rack.get('id') or '(unnamed)'} needs an exact active-head catalog rack/tip pair.")
        elif pair.get("required_head_mode") not in (None, "all_barrels"):
            blocks.append(f"Tip rack {rack.get('id') or '(unnamed)'} requires {pair['required_head_mode']} head mode.")
    if blocks:
        return None, evidence, blocks

    missing_definition = False
    partial = False
    scope_decision = _scientist_bool(plan, "/setup/full_head_footprint_authorized")
    cited_scope_text = " ".join(paragraphs.get(identity, "") for step in liquid_steps
                                for identity in step.get("source_paragraph_ids") or [])
    four_source_scope = _four_source_quadrant_scope(liquid_steps, materials, labware, cited_scope_text)
    for step in liquid_steps:
        cited_text = " ".join(paragraphs.get(identity, "") for identity in step.get("source_paragraph_ids") or [])
        full_scope_matches = list(re.finditer(
            r"\b(?:every|all)\s+(?:source\s+)?wells?\b|\b(?:full|entire)[ -]plate\b",
            cited_text, re.IGNORECASE,
        ))
        explicit_full_scope = four_source_scope or any(
            not re.search(r"\b(?:not|never|except|don't|do not)\b", cited_text[max(0, match.start() - 30):match.start()], re.IGNORECASE)
            for match in full_scope_matches
        )
        negated_full_scope = any(
            re.search(r"\b(?:not|never|except|don't|do not)\b", cited_text[max(0, match.start() - 30):match.start()], re.IGNORECASE)
            for match in full_scope_matches
        )
        excluded_wells = bool(re.search(r"\b(?:except|excluding|but not|apart from|other than)\b", cited_text, re.IGNORECASE))
        source_material = materials.get(step.get("source"))
        destination_material = materials.get(step.get("destination"))
        source_plate = labware.get(source_material.get("labware_id")) if source_material else None
        destination_plate = labware.get(destination_material.get("labware_id")) if destination_material else None
        verified_pattern = step.get("kind") == "transfer" and any(
            source_plate and destination_plate
            and source_plate.get("wells") == pattern.get("source_labware_wells")
            and destination_plate.get("wells") == pattern.get("destination_labware_wells")
            and step.get("source_anchor") == pattern.get("source_anchor")
            and step.get("destination_anchor") in (pattern.get("destination_anchors") or [])
            for pattern in _rows(manifest.get("transfer_patterns"))
        )
        explicitly_single = bool(re.search(
            r"\b(?:one|single|only one|just one)\s+(?:source\s+)?well\b|\bonly\s+A1\b|\bA1\s+only\b",
            cited_text, re.IGNORECASE,
        ))
        if scope_decision is False or (scope_decision is not True and (
            not explicit_full_scope or explicitly_single or negated_full_scope or excluded_wells
        )):
            blocks.append(f"{step['_path']} needs affirmative, cited all-wells source text or a scientist's full-head decision; an A1 anchor and catalog pattern alone are ambiguous.")
            missing_definition = True
        elif verified_pattern:
            evidence.append({"source": "manifest_transfer_pattern", "path": step["_path"],
                             "pattern_id": "384_to_1536_quadrant"})
        if scope_decision is True:
            evidence.append({"source": "scientist_decision", "path": "/setup/full_head_footprint_authorized",
                             "value": True})
        elif explicit_full_scope:
            evidence.append({"source": "cited_protocol_text", "path": step["_path"],
                             "paragraph_ids": list(step.get("source_paragraph_ids") or []),
                             "scope_pattern": "four_source_384_to_two_1536_quadrants" if four_source_scope else "all_wells"})
        targets = (("source", "source_anchor"), ("destination", "destination_anchor")) if step.get("kind") == "transfer" else (("material", "anchor"),)
        for material_field, anchor_field in targets:
            material_id, anchor = step.get(material_field), step.get(anchor_field)
            material = materials.get(material_id)
            definition = labware.get(material.get("labware_id")) if material else None
            if material is None or material.get("role") != "liquid" or definition is None or definition.get("provisional"):
                blocks.append(f"{step['_path']}/{material_field} needs a verified liquid plate catalog ID.")
                missing_definition = True
                continue
            if any((definition["id"], rack.get("tip_definition_id")) in incompatible for rack in tips):
                blocks.append(f"{step['_path']}/{material_field} has an explicitly incompatible selected tip/plate pair.")
                missing_definition = True
                continue
            try:
                anchor_cell = well_cell(anchor or "")
                cells = plate_footprint_wells(
                    head, mode, int(definition.get("rows") or 0), int(definition.get("cols") or 0),
                    float(definition.get("spacing_x_mm") or 0), float(definition.get("spacing_y_mm") or 0),
                    *anchor_cell,
                )
            except (ValueError, TypeError):
                blocks.append(f"{step['_path']}/{anchor_field} needs a valid, explicit well anchor and plate grid.")
                missing_definition = True
                continue
            if len(cells) != geometry.rows * geometry.columns:
                blocks.append(f"{step['_path']}/{anchor_field} does not admit the full active-head footprint on {material_id}.")
                partial = True
                continue
            evidence.append({"source": "plan_and_catalog", "path": f"{step['_path']}/{anchor_field}",
                             "material_id": material_id, "labware_id": definition["id"],
                             "anchor": anchor, "addressed_wells": len(cells)})
    return (None if missing_definition else False if partial else True), evidence, blocks


def _destinations_empty_for_each_source(
    liquid_steps: list[dict[str, Any]], materials: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any], head: HeadType | None, full_head: bool | None,
) -> tuple[bool | None, list[dict[str, Any]]]:
    """An empty plate can still hold earlier sources when quadrants overlap."""
    if full_head is not True or head is None:
        return None, []
    transfers = [step for step in liquid_steps if step.get("kind") == "transfer"]
    if not transfers:
        return None, []
    labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    mode = normalize_head_mode(head, "all_barrels", "back_left")
    occupied: dict[str, list[tuple[str, set[tuple[int, int]]]]] = {}
    evidence: list[dict[str, Any]] = []
    for step in transfers:
        destination_id = step.get("destination")
        material = materials.get(destination_id)
        definition = labware.get(material.get("labware_id")) if material else None
        if (material is None or definition is None or material.get("initial_volume_ul") != 0
                or any(value != 0 for value in _data(material.get("well_volumes_ul")).values())):
            return False, [{"source": "plan", "destination": destination_id,
                            "reason": "Destination initial volumes are not all recorded as zero."}]
        try:
            anchor = well_cell(step.get("destination_anchor") or "")
            cells = set(plate_footprint_wells(
                head, mode, int(definition.get("rows") or 0), int(definition.get("cols") or 0),
                float(definition.get("spacing_x_mm") or 0), float(definition.get("spacing_y_mm") or 0),
                *anchor,
            ))
        except (ValueError, TypeError):
            return None, []
        if not cells:
            return None, []
        for prior_source, prior_cells in occupied.get(destination_id, []):
            if prior_source != step.get("source") and prior_cells & cells:
                return False, [{"source": "destination_footprint_overlap", "destination": destination_id,
                                "first_source": prior_source, "later_source": step.get("source"),
                                "overlapping_wells": len(prior_cells & cells)}]
        occupied.setdefault(destination_id, []).append((step.get("source"), cells))
        evidence.append({"source": "plan_and_catalog", "path": step["_path"] + "/destination_anchor",
                         "destination": destination_id, "source_material_id": step.get("source"),
                         "addressed_wells": len(cells), "initial_volume_ul": 0})
    return True, evidence


def _authorized_reuse(plan: Mapping[str, Any], setup: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    """Only structured scientist decisions establish permission to reuse tips."""
    del setup
    authorized = _scientist_bool(plan, "/setup/same_source_reuse_authorized")
    if authorized is None:
        return None, None
    return authorized, {"source": "scientist_decision", "path": "/setup/same_source_reuse_authorized",
                        "value": authorized, "plan_fingerprint": setup_plan_fingerprint(dict(plan))}


def _rack_order(
    liquid_steps: list[dict[str, Any]], materials: dict[str, dict[str, Any]],
    manifest: Mapping[str, Any], has_repeat: bool, plan: Mapping[str, Any],
) -> tuple[list[str] | None, list[str], list[dict[str, Any]]]:
    """Pair distinct catalog racks to first source use, in plan rack order."""
    transfers = [step for step in liquid_steps if step.get("kind") == "transfer"]
    if has_repeat or len(transfers) != len(liquid_steps) or not transfers:
        return None, ["Tip-rack order needs explicit review when mixing or repeating liquid steps."], []
    sources = list(dict.fromkeys(step.get("source") for step in transfers))
    if any(source not in materials for source in sources):
        return None, ["Each transfer source must identify a plan material."], []
    # A rack used for one source cannot be silently reused after an intervening
    # source or handoff. The compiler discards tips on these transitions.
    seen: set[str] = set()
    previous: str | None = None
    for step in transfers:
        source = step.get("source")
        if source != previous:
            if source in seen:
                return None, ["A source reappears after another source; one dedicated rack per source is not established."], []
            seen.add(source)
            previous = source
    top_level_steps = _rows(plan.get("steps"))
    for source in sources:
        positions = [index for index, step in enumerate(top_level_steps)
                     if step.get("kind") == "transfer" and step.get("source") == source]
        if positions and positions != list(range(positions[0], positions[-1] + 1)):
            return None, [f"Source {source} has a handoff or plate move between transfers; the compiler discards loaded tips at that point."], []
    racks = [row for row in materials.values() if row.get("role") == "tips"]
    if len(sources) < 2 or len(racks) != len(sources):
        return None, ["Select one distinct tip-rack material for each source plate."], []
    rack_by_id = {rack.get("id"): rack for rack in racks}
    pairing_decision = next((decision for decision in reversed(_rows(plan.get("decisions")))
                             if decision.get("path") == "/setup/source_tip_rack_pairs"
                             and decision.get("actor") == "scientist" and isinstance(decision.get("value"), Mapping)), None)
    pairing_value = _data(pairing_decision.get("value")) if pairing_decision else {}
    explicit_pairs = (_data(pairing_value.get("pairs"))
                      if pairing_value.get("plan_fingerprint") == setup_plan_fingerprint(dict(plan)) else {})
    if explicit_pairs:
        if set(explicit_pairs) != set(sources) or len(set(explicit_pairs.values())) != len(sources) or any(
            rack_id not in rack_by_id for rack_id in explicit_pairs.values()
        ):
            return None, ["The scientist's source-to-rack mapping must name each source and each distinct rack exactly once."], []
        ordered_racks = [rack_by_id[explicit_pairs[source]] for source in sources]
        pairing_basis = "scientist_decision"
    else:
        ordered_racks = []
        for source in sources:
            token = re.compile(r"(?<![A-Za-z0-9])" + re.escape(source) + r"(?![A-Za-z0-9])", re.IGNORECASE)
            matches = [rack for rack in racks if token.search(str(rack.get("id") or ""))]
            if len(matches) != 1:
                return None, [f"Rack IDs must each contain one exact source ID, including {source}, or the scientist must record /setup/source_tip_rack_pairs."], []
            ordered_racks.append(matches[0])
        if len({rack["id"] for rack in ordered_racks}) != len(sources):
            return None, ["The source-coded rack IDs do not establish one distinct rack per source."], []
        pairing_basis = "exact_source_id_in_rack_id"
    pairs = {(row.get("labware_id"), row.get("tip_definition_id")): row
             for row in _rows(manifest.get("tipbox_choices"))}
    relations = {(row.get("target_labware_id"), row.get("tip_definition_id")): row
                 for row in _rows(manifest.get("tip_plate_compatibility"))}
    blocks: list[str] = []
    evidence: list[dict[str, Any]] = []
    for source_id, rack in zip(sources, ordered_racks):
        pair = pairs.get((rack.get("labware_id"), rack.get("tip_definition_id")))
        if pair is None:
            blocks.append(f"{rack.get('id') or '(unnamed rack)'} has no exact verified rack/tip pair for this head.")
            continue
        source_steps = [step for step in transfers if step.get("source") == source_id]
        for step in source_steps:
            if not _positive(step.get("volume_ul")) or (
                _positive(pair.get("tip_capacity_ul")) and step["volume_ul"] > pair["tip_capacity_ul"]
            ):
                blocks.append(f"{step['_path']}/volume_ul needs a confirmed volume within {rack.get('tip_definition_id')} capacity.")
            for material_id in (source_id, step.get("destination")):
                material = materials.get(material_id)
                relation = relations.get((material.get("labware_id"), rack.get("tip_definition_id"))) if material else None
                if relation and relation.get("compatible") is False:
                    blocks.append(f"{rack.get('tip_definition_id')} is incompatible with {material_id}.")
        evidence.append({"source": "plan_and_catalog", "source_material_id": source_id,
                         "tip_rack_id": rack.get("id"), "labware_id": rack.get("labware_id"),
                         "tip_definition_id": rack.get("tip_definition_id"),
                         "execution_ready": pair.get("execution_ready") is True,
                         "pairing_basis": pairing_basis})
    if blocks:
        return None, blocks, evidence
    return [rack["id"] for rack in ordered_racks], [], evidence


def _condition_matches(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    name = condition.get("fact")
    if name not in facts or facts[name] is None:
        return False
    actual, expected = facts[name], condition.get("value")
    op = condition.get("op")
    if op == "eq":
        return type(actual) is type(expected) and actual == expected
    if op == "gte":
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual >= expected
    if op == "in":
        return isinstance(expected, list) and actual in expected
    return False


def recommend_setup(plan: dict, setup: dict, manifest: dict, *, source: dict | None = None) -> dict:
    """Return rule-backed setup proposals and remaining scientist inputs.

    Source paragraphs can establish an explicit all-wells instruction when
    cited by a step. Conversational text cannot certify tip reuse or pipetting
    height. The returned proposals are review drafts, never applied changes.
    """
    plan, setup, manifest = _data(plan), _data(setup), _data(manifest)
    source = _data(source)
    material_rows = _rows(plan.get("materials"))
    materials = {row["id"]: row for row in material_rows if isinstance(row.get("id"), str) and row["id"]}
    liquid_steps, has_repeat, expanded_count = _steps(plan)
    head = _head(manifest)
    full_head, head_evidence, head_blocks = _full_head_footprint(liquid_steps, materials, manifest, head, source, plan)
    rack_ids, rack_blocks, rack_evidence = _rack_order(liquid_steps, materials, manifest, has_repeat, plan)
    authorized, authorization_evidence = _authorized_reuse(plan, setup)
    empty_destinations, destination_evidence = _destinations_empty_for_each_source(
        liquid_steps, materials, manifest, head, full_head,
    )
    source_ids = list(dict.fromkeys(step.get("source") for step in liquid_steps if step.get("kind") == "transfer"))
    catalog_labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    addressed_ids = list(dict.fromkeys(
        material_id for step in liquid_steps
        for material_id in ((step.get("source"), step.get("destination")) if step.get("kind") == "transfer"
                            else (step.get("material"),))
    ))
    addressed_depths = [catalog_labware.get(materials.get(mid, {}).get("labware_id"), {}).get("well_depth_mm")
                        for mid in addressed_ids]
    minimum_depth = min(addressed_depths) if addressed_depths and all(_positive(depth) for depth in addressed_depths) else None
    waste_rows = [row for row in material_rows if row.get("role") == "waste"]
    waste_present: bool | None = False
    if waste_rows:
        waste_present = True if all(
            isinstance(row.get("deck_slot"), int) and not isinstance(row.get("deck_slot"), bool)
            and 1 <= row["deck_slot"] <= 9 and row.get("labware_id")
            for row in waste_rows
        ) else None
    facts: dict[str, Any] = {
        "liquid_step_count": expanded_count,
        "full_head_footprint": full_head,
        "source_count": len(source_ids),
        "dedicated_tip_rack_per_source": rack_ids is not None,
        "destination_initially_empty": empty_destinations,
        "same_source_reuse_authorized": authorized,
        "tip_strategy": setup.get("tip_strategy"),
        "waste_material_present": waste_present,
        "minimum_addressed_well_depth_mm": minimum_depth,
    }
    evidence_by_fact = {
        "full_head_footprint": head_evidence,
        "dedicated_tip_rack_per_source": rack_evidence,
        "same_source_reuse_authorized": [authorization_evidence] if authorization_evidence else [],
        "destination_initially_empty": destination_evidence,
    }
    recommendations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    suggested_paths: set[str] = set()
    unresolved_paths: set[str] = set()

    for rule in _rows(manifest.get("setup_decision_rules")):
        path = rule.get("decision")
        conditions = _data(rule.get("when")).get("all")
        if not isinstance(path, str) or not path.startswith("/setup/") or not isinstance(conditions, list):
            continue
        if not all(isinstance(condition, Mapping) and _condition_matches(condition, facts)
                   for condition in conditions):
            continue
        if not _missing(_set_up_value(setup, path)):
            continue
        raw_value = rule.get("recommendation")
        if isinstance(raw_value, Mapping) and raw_value.get("resolver") == "source_ordered_verified_tip_rack_ids":
            value = rack_ids
        else:
            value = raw_value
        result = {
            "path": path, "rule_id": rule.get("id"),
            "rationale": rule.get("rationale", ""),
            "evidence_level": rule.get("evidence_level"),
            "provenance": list(rule.get("provenance") or []),
            "required_evidence": list(rule.get("required_evidence") or []),
            "requires_confirmation": rule.get("requires_confirmation") is True,
            "evidence": [item for condition in conditions
                         for item in evidence_by_fact.get(condition.get("fact"), [])],
        }
        if rule.get("id") == "tip_rack_order_by_source" and rack_evidence:
            basis = rack_evidence[0]["pairing_basis"]
            result["evidence_level"] = "derived" if basis == "scientist_decision" else "heuristic"
            result["provenance"] = result["provenance"] + [basis]
        if rule.get("id") == "head_mode_full_footprint":
            structured = _scientist_bool(plan, "/setup/full_head_footprint_authorized") is True
            result["evidence_level"] = "derived" if structured else "heuristic"
            result["provenance"] = result["provenance"] + [
                "scientist_decision" if structured else "affirmative_cited_protocol_text"
            ]
        if value is None:
            if path not in unresolved_paths:
                if path == "/setup/distance_from_bottom_mm":
                    bounds = {"minimum_inclusive_mm": 0,
                              "maximum_exclusive_mm": minimum_depth}
                    reason = result["rationale"]
                    if minimum_depth is not None:
                        reason += f" Catalog geometry requires 0 ≤ height < {minimum_depth:g} mm."
                    result = {**result, "bounds": bounds,
                              "evidence": result["evidence"] + [
                                  {"source": "catalog", "material_id": mid,
                                   "labware_id": materials.get(mid, {}).get("labware_id"),
                                   "well_depth_mm": depth}
                                  for mid, depth in zip(addressed_ids, addressed_depths)
                              ]}
                else:
                    reason = result["rationale"]
                unresolved.append({**result, "reason": reason})
                unresolved_paths.add(path)
            continue
        if path in suggested_paths:
            continue
        recommendations.append({**result, "value": value})
        suggested_paths.add(path)

    blocked: list[dict[str, Any]] = []
    if liquid_steps and head_blocks:
        blocked.append({"path": "/setup/head_mode", "reason": "A full-head recommendation needs complete verified geometry and tip pairing.", "missing": head_blocks})
    if liquid_steps and rack_ids is None and rack_blocks:
        blocked.append({"path": "/setup/tip_rack_ids", "reason": "One reviewed rack per source is not established.", "missing": rack_blocks})
    if (liquid_steps and _missing(setup.get("tip_strategy"))
            and "/setup/tip_strategy" not in suggested_paths
            and "/setup/tip_strategy" not in unresolved_paths):
        overlap = any(item.get("source") == "destination_footprint_overlap"
                      for item in destination_evidence)
        if overlap:
            reason = "Different sources address overlapping destination wells; resolve the mapping before selecting a source-isolated tip strategy."
        elif authorized is True and empty_destinations is False:
            reason = "Record 0 µL starting volume for each destination plate and verify its addressed wells are initially empty."
        elif authorized is True and rack_ids is None:
            reason = "Confirm one compatible, distinct tip rack for each source and its exact source-to-rack pairing."
        else:
            reason = "Confirm whether the same source's tip set may dispense into all its destinations; do not infer permission from an empty plate alone."
        unresolved.append({"path": "/setup/tip_strategy", "rule_id": None,
                           "reason": reason,
                           "required_evidence": ["Scientist decision on same-source tip reuse and carryover."],
                           "evidence_level": "scientist_input", "requires_confirmation": True,
                           "provenance": ["scientist_decision"], "evidence": destination_evidence})
    if any(item.get("source") == "destination_footprint_overlap" for item in destination_evidence):
        blocked.append({"path": "/setup/tip_strategy",
                        "reason": "A later source would dispense into wells already filled by another source.",
                        "missing": ["Resolve overlapping source-to-destination footprints or record a qualified alternative contamination strategy."]})
    if (liquid_steps and _missing(setup.get("tip_rack_ids"))
            and "/setup/tip_rack_ids" not in suggested_paths
            and "/setup/tip_rack_ids" not in unresolved_paths):
        unresolved.append({"path": "/setup/tip_rack_ids", "rule_id": None,
                           "reason": ("Select or confirm fresh_each_source before applying the proposed source-to-rack order."
                                      if rack_ids is not None else
                                      "Select and confirm compatible racks in first-source-use order."),
                           "required_evidence": ["One distinct verified rack/tip pair per source and inspected fresh-tip inventory."],
                           "evidence_level": "scientist_input", "requires_confirmation": True,
                           "provenance": ["active_head_catalog_pair"], "evidence": rack_evidence})
    if (liquid_steps and _missing(setup.get("tip_disposal_id"))
            and "/setup/tip_disposal_id" not in suggested_paths
            and "/setup/tip_disposal_id" not in unresolved_paths):
        unresolved.append({"path": "/setup/tip_disposal_id", "rule_id": None,
                           "reason": "Confirm where used tips will go; an incomplete waste material is not an on-deck receptacle.",
                           "required_evidence": ["Configured waste material with deck position, or approved return to each source rack."],
                           "evidence_level": "scientist_input", "requires_confirmation": True,
                           "provenance": ["pybravo.workflow.protocols.validation"], "evidence": []})
    # Inventory is physical run state, never a catalog or model inference.
    for index, row in enumerate(material_rows):
        if row.get("role") == "tips" and row.get("available_tips") is None:
            unresolved.append({"path": f"/materials/{index}/available_tips", "rule_id": None,
                               "reason": "Inspect and record which fresh tips are physically present before execution.",
                               "required_evidence": ["Scientist-inspected fresh-tip wells or a confirmed full rack."],
                               "evidence_level": "scientist_input", "requires_confirmation": True,
                               "provenance": ["physical_tip_inventory"], "evidence": []})
    return {
        "context_hash": manifest.get("context_hash"),
        "plan_fingerprint": setup_plan_fingerprint(plan),
        "recommendations": recommendations,
        "unresolved": unresolved,
        "blocked": blocked,
    }
