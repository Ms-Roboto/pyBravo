"""Explainable setup proposals derived from a capability manifest and a draft.

This module never edits a protocol or records a scientist decision. It applies
the active manifest's setup rules and a conservative deck-slot policy derived
from its catalog and machine slots; validation and scientist review remain
separate gates.
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


def _runtime_advisory(snapshot: Mapping[str, Any] | None, slots: set[int]) -> dict[str, Any]:
    """Expose only deck/tip evidence from get_state, without certifying a load.

    Bravo's deck and occupancy tables are software state. They can be stale or
    preconfigured, so neither their identities nor their occupied-tip counts
    are run authorization or a substitute for physical inspection.
    """
    result: dict[str, Any] = {
        "status": "software_known_unverified" if isinstance(snapshot, Mapping) else "unavailable",
        "physically_verified": False,
        "occupied_slots": [],
        "tipbox_inventory": [],
    }
    if not isinstance(snapshot, Mapping):
        return result

    deck = _data(snapshot.get("deck"))
    details = _data(snapshot.get("deck_details"))
    inventory = _data(snapshot.get("tipbox_inventory"))
    for slot in sorted(slots):
        names = deck.get(str(slot))
        entries = details.get(str(slot))
        tipbox = _data(inventory.get(str(slot)))
        if not isinstance(names, list):
            names = []
        if not isinstance(entries, list):
            entries = []
        names = [name for name in names if isinstance(name, str) and name]
        if not names:
            names = [name for item in entries if isinstance(name := _data(item).get("name"), str) and name]
        if not names and isinstance(tipbox.get("labware_name"), str) and tipbox["labware_name"]:
            names = [tipbox["labware_name"]]
        if names or entries or tipbox:
            result["occupied_slots"].append({"slot": slot, "labware_names": names,
                                               "source": ("software_deck_state" if deck.get(str(slot)) or entries
                                                          else "software_tipbox_inventory")})

    for slot in sorted(slots):
        row = _data(inventory.get(str(slot)))
        if not row:
            continue
        occupied = row.get("occupied")
        occupied = [value for value in occupied if isinstance(value, str)] if isinstance(occupied, list) else []
        nrows, ncols = row.get("rows"), row.get("cols")
        well_count = (nrows * ncols if isinstance(nrows, int) and not isinstance(nrows, bool)
                      and isinstance(ncols, int) and not isinstance(ncols, bool)
                      and nrows > 0 and ncols > 0 else None)
        result["tipbox_inventory"].append({
            "slot": slot,
            "labware_name": row.get("labware_name") if isinstance(row.get("labware_name"), str) else None,
            "tip_id": row.get("tip_id") if isinstance(row.get("tip_id"), str) else None,
            "software_occupied_count": len(occupied),
            "well_count": well_count,
            "occupied_wells": occupied,
            "source": "software_tipbox_inventory",
        })
    return result


def _deck_proposals(plan: Mapping[str, Any], manifest: Mapping[str, Any],
                    runtime_advisory: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Suggest only collision-free positions for catalog-known materials.

    An existing stack order may establish a single source stack, but this
    routine never assigns an order or asserts that plates/tips are present.
    Plate-move destinations are reserved even when currently empty. Software
    occupancy is advisory and can reserve other slots, never certify a load.
    """
    rows = _rows(plan.get("materials"))
    slots = sorted({slot for slot in (_data(manifest.get("machine")).get("deck_slots") or [])
                    if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0})
    if not slots:
        return [], []
    catalog = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    tip_pairs = {(row.get("labware_id"), row.get("tip_definition_id")) for row in
                 _rows(manifest.get("tipbox_choices")) if row.get("execution_ready") is True}
    assigned = {row.get("deck_slot") for row in rows if isinstance(row.get("deck_slot"), int)
                and not isinstance(row.get("deck_slot"), bool)}
    runtime_reserved = {row["slot"] for row in _rows(runtime_advisory.get("occupied_slots"))
                        if isinstance(row.get("slot"), int) and row["slot"] not in assigned}
    reserved_move_slots: set[int] = set()
    source_ids: set[str] = set()
    destination_ids: set[str] = set()

    def inspect_steps(steps: Any) -> None:
        for step in _rows(steps):
            if step.get("kind") in {"move_plate", "destack_plate", "stack_plate"}:
                slot = step.get("destination_slot")
                if isinstance(slot, int) and not isinstance(slot, bool):
                    reserved_move_slots.add(slot)
            if step.get("kind") in {"transfer", "distribute"} and isinstance(step.get("source"), str):
                source_ids.add(step["source"])
            if step.get("kind") == "transfer" and isinstance(step.get("destination"), str):
                destination_ids.add(step["destination"])
            if step.get("kind") == "distribute":
                destination_ids.update(item["destination"] for item in _rows(step.get("dispenses"))
                                       if isinstance(item.get("destination"), str))
            if step.get("kind") == "repeat":
                inspect_steps(step.get("steps"))

    inspect_steps(plan.get("steps"))
    unplaced = [(index, row) for index, row in enumerate(rows) if row.get("deck_slot") is None]
    if not unplaced:
        return [], []
    blocked: list[dict[str, Any]] = []
    for index, row in unplaced:
        definition = catalog.get(row.get("labware_id"))
        if definition is None or definition.get("provisional"):
            blocked.append({"path": f"/materials/{index}/deck_slot",
                            "reason": "Choose a verified catalog labware ID before proposing a deck position.",
                            "missing": ["Verified labware identity."]})
        elif row.get("role") == "tips" and (row.get("labware_id"), row.get("tip_definition_id")) not in tip_pairs:
            blocked.append({"path": f"/materials/{index}/deck_slot",
                            "reason": "Select an exact head-compatible rack and tip pair before placing tips.",
                            "missing": ["Verified rack–tip pair for the active head."]})
    if blocked:
        return [], blocked

    # There is no stack-group identifier in ProtocolMaterial. A shared slot is
    # safe to propose only for the one unambiguous, already ordered source
    # stack in this plan. Partial/duplicate orders remain scientist decisions.
    ordered = [(index, row) for index, row in enumerate(rows) if row.get("stack_order") is not None]
    stack_group: list[tuple[int, dict[str, Any]]] = []
    if ordered:
        orders = [row.get("stack_order") for _, row in ordered]
        same_definition = len({row.get("labware_id") for _, row in ordered}) == 1
        candidate_slots = {row.get("deck_slot") for _, row in ordered if row.get("deck_slot") is not None}
        stack_ok = (
            len(ordered) > 1
            and all(isinstance(order, int) and not isinstance(order, bool) and order >= 0 for order in orders)
            and sorted(orders) == list(range(len(ordered)))
            and same_definition and len(candidate_slots) <= 1
            and all(row.get("role") == "liquid" and row.get("id") in source_ids for _, row in ordered)
            and all(row.get("labware_id") in catalog for _, row in ordered)
            and all(_positive(catalog[row["labware_id"]].get("stack_height_mm")) for _, row in ordered)
            and _data(manifest.get("machine")).get("has_gripper") is True
        )
        if stack_ok and candidate_slots:
            anchor = next(iter(candidate_slots))
            stack_ok = anchor in slots and all(
                row.get("deck_slot") != anchor or (index, row) in ordered
                for index, row in enumerate(rows)
            )
        if stack_ok:
            stack_group = ordered
        elif any(row.get("deck_slot") is None for _, row in ordered):
            return [], [{"path": f"/materials/{index}/deck_slot",
                         "reason": "Stack levels are present, but they do not identify one verified, contiguous source stack.",
                         "missing": ["Confirm one stack location and the bottom-to-top order."]}
                        for index, row in ordered if row.get("deck_slot") is None]

    free = [slot for slot in slots if slot not in assigned and slot not in reserved_move_slots
            and slot not in runtime_reserved]
    grouped_indexes = {index for index, _ in stack_group}
    units: list[list[tuple[int, dict[str, Any]]]] = []
    if stack_group and any(index in grouped_indexes for index, _ in unplaced):
        units.append([(index, row) for index, row in stack_group if row.get("deck_slot") is None])
    units.extend([[(index, row)] for index, row in unplaced if index not in grouped_indexes])
    # Supply racks first, then destinations, then source plates and waste.
    units.sort(key=lambda unit: (
        0 if unit[0][1].get("role") == "tips" else
        1 if unit[0][1].get("id") in destination_ids else
        2 if unit[0][1].get("id") in source_ids else 3,
        unit[0][0],
    ))
    needs_free = sum(1 for unit in units if not (
        stack_group and unit[0][0] in grouped_indexes
        and any(row.get("deck_slot") is not None for _, row in stack_group)
    ))
    if needs_free > len(free):
        return [], [{"path": f"/materials/{index}/deck_slot",
                     "reason": "No collision-free position remains after reserving draft materials, plate-move destinations, and software-known occupied slots.",
                     "missing": ["Confirm an explicit stack or revise the deck layout."]}
                    for index, _ in unplaced]

    proposed: list[dict[str, Any]] = []
    for unit in units:
        in_stack = bool(stack_group and unit[0][0] in grouped_indexes)
        anchor = next((row["deck_slot"] for _, row in stack_group if row.get("deck_slot") is not None), None) if in_stack else None
        slot = anchor if anchor is not None else free.pop(0)
        for index, row in unit:
            proposed.append({
                "path": f"/materials/{index}/deck_slot", "value": slot,
                "rule_id": "explicit_source_stack_slot" if in_stack else "free_deck_slot",
                "rationale": ("This source belongs to the plan's explicitly ordered stack."
                              if in_stack else "This position is unassigned and not reserved by a plate move or software-known occupancy."),
                "evidence_level": "heuristic", "requires_confirmation": True,
                "provenance": ["active_machine_deck_slots", "catalog_labware", "plan_materials"]
                + (["software_known_runtime_snapshot"] if runtime_advisory.get("status") == "software_known_unverified" else []),
                "required_evidence": ["Scientist confirmation of physical deck placement and clearance."],
                "evidence": [{"source": "plan_and_catalog", "material_id": row.get("id"),
                              "labware_id": row.get("labware_id"), "proposed_slot": slot,
                              "reserved_move_slots": sorted(reserved_move_slots),
                              "software_occupied_unassigned_slots": sorted(runtime_reserved),
                              "runtime_snapshot_status": runtime_advisory.get("status"),
                              "software_state_physically_verified": False}],
            })
    return sorted(proposed, key=lambda item: int(item["path"].split("/")[2])), []


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
            elif step.get("kind") in {"transfer", "distribute", "mix"}:
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
        if pair is None or pair.get("execution_ready") is not True:
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
        path = step["_path"]
        if step.get("kind") == "transfer":
            targets = [(step.get("source"), step.get("source_anchor"), f"{path}/source", f"{path}/source_anchor"),
                       (step.get("destination"), step.get("destination_anchor"),
                        f"{path}/destination", f"{path}/destination_anchor")]
        elif step.get("kind") == "distribute":
            targets = [(step.get("source"), step.get("source_anchor"), f"{path}/source", f"{path}/source_anchor")]
            targets.extend((item.get("destination"), item.get("destination_anchor"),
                            f"{path}/dispenses/{index}/destination",
                            f"{path}/dispenses/{index}/destination_anchor")
                           for index, item in enumerate(_rows(step.get("dispenses"))))
        else:
            targets = [(step.get("material"), step.get("anchor"), f"{path}/material", f"{path}/anchor")]
        for material_id, anchor, material_path, anchor_path in targets:
            material = materials.get(material_id)
            definition = labware.get(material.get("labware_id")) if material else None
            if material is None or material.get("role") != "liquid" or definition is None or definition.get("provisional"):
                blocks.append(f"{material_path} needs a verified liquid plate catalog ID.")
                missing_definition = True
                continue
            if any((definition["id"], rack.get("tip_definition_id")) in incompatible for rack in tips):
                blocks.append(f"{material_path} has an explicitly incompatible selected tip/plate pair.")
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
                blocks.append(f"{anchor_path} needs a valid, explicit well anchor and plate grid.")
                missing_definition = True
                continue
            if len(cells) != geometry.rows * geometry.columns:
                blocks.append(f"{anchor_path} does not admit the full active-head footprint on {material_id}.")
                partial = True
                continue
            evidence.append({"source": "plan_and_catalog", "path": anchor_path,
                             "material_id": material_id, "labware_id": definition["id"],
                             "anchor": anchor, "addressed_wells": len(cells)})
    return (None if missing_definition else False if partial else True), evidence, blocks


def _destinations_empty_for_each_source(
    liquid_steps: list[dict[str, Any]], materials: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any], head: HeadType | None, full_head: bool | None,
) -> tuple[bool | None, bool | None, list[dict[str, Any]]]:
    """An empty plate can still hold earlier sources when quadrants overlap."""
    if full_head is not True or head is None:
        return None, None, []
    transfers = [step for step in liquid_steps if step.get("kind") == "transfer"]
    for step in liquid_steps:
        if step.get("kind") == "distribute":
            transfers.extend({"source": step.get("source"), "destination": item.get("destination"),
                              "destination_anchor": item.get("destination_anchor"),
                              "_path": f"{step['_path']}/dispenses/{index}"}
                             for index, item in enumerate(_rows(step.get("dispenses"))))
    if not transfers:
        return None, None, []
    labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    mode = normalize_head_mode(head, "all_barrels", "back_left")
    occupied: dict[str, list[tuple[str, set[tuple[int, int]]]]] = {}
    evidence: list[dict[str, Any]] = []
    recorded_empty = True
    missing_initial_volume = False
    for step in transfers:
        destination_id = step.get("destination")
        material = materials.get(destination_id)
        definition = labware.get(material.get("labware_id")) if material else None
        if material is None or definition is None:
            return None, None, []
        initial_volume = material.get("initial_volume_ul")
        if initial_volume is None:
            missing_initial_volume = True
        elif initial_volume != 0:
            recorded_empty = False
        if any(value != 0 for value in _data(material.get("well_volumes_ul")).values()):
            recorded_empty = False
        try:
            anchor = well_cell(step.get("destination_anchor") or "")
            cells = set(plate_footprint_wells(
                head, mode, int(definition.get("rows") or 0), int(definition.get("cols") or 0),
                float(definition.get("spacing_x_mm") or 0), float(definition.get("spacing_y_mm") or 0),
                *anchor,
            ))
        except (ValueError, TypeError):
            return None, None, []
        if not cells:
            return None, None, []
        for prior_source, prior_cells in occupied.get(destination_id, []):
            if prior_source != step.get("source") and prior_cells & cells:
                return False, False, [{"source": "destination_footprint_overlap", "destination": destination_id,
                                       "first_source": prior_source, "later_source": step.get("source"),
                                       "overlapping_wells": len(prior_cells & cells)}]
        occupied.setdefault(destination_id, []).append((step.get("source"), cells))
        evidence.append({"source": "plan_and_catalog", "path": step["_path"] + "/destination_anchor",
                         "destination": destination_id, "source_material_id": step.get("source"),
                         "addressed_wells": len(cells), "initial_volume_ul": initial_volume,
                         "initial_volume_recorded": initial_volume is not None})
    if not recorded_empty:
        evidence.append({"source": "plan", "reason": "At least one destination records nonzero starting liquid."})
        return False, True, evidence
    if missing_initial_volume:
        evidence.append({"source": "plan", "reason": "Destination starting volumes are not yet recorded; physical emptiness is unconfirmed."})
        return None, True, evidence
    return True, True, evidence


def _authorized_reuse(plan: Mapping[str, Any], setup: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    """Only structured scientist decisions establish permission to reuse tips."""
    del setup
    authorized = _scientist_bool(plan, "/setup/same_source_reuse_authorized")
    if authorized is None:
        return None, None
    return authorized, {"source": "scientist_decision", "path": "/setup/same_source_reuse_authorized",
                        "value": authorized, "plan_fingerprint": setup_plan_fingerprint(dict(plan))}


def _cited_quadrant_scope(
    liquid_steps: list[dict[str, Any]], materials: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any], source: Mapping[str, Any],
) -> tuple[bool, list[str], str]:
    """Recognize a recipe only from text actually cited by the liquid steps."""
    paragraphs = {row.get("id") or row.get("paragraph_id"): str(row.get("text") or "")
                  for row in _rows(source.get("paragraphs"))}
    paragraph_ids = list(dict.fromkeys(identity for step in liquid_steps
                                       for identity in step.get("source_paragraph_ids") or []
                                       if isinstance(identity, str) and identity in paragraphs))
    cited_text = " ".join(paragraphs[identity] for identity in paragraph_ids)
    labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    return _four_source_quadrant_scope(liquid_steps, materials, labware, cited_text), paragraph_ids, cited_text


def _explicit_fresh_tip_requirement(cited_text: str) -> bool:
    """An explicit per-transfer tip request overrides a reuse recipe candidate."""
    return bool(re.search(
        r"\b(?:do\s+not|don't|never|no)\s+(?:\w+\s+){0,3}reuse\b"
        r"|\b(?:fresh|new|separate)\s+tips?\s+(?:for|before|between)\s+"
        r"(?:each|every|both|the\s+two)\s+(?:destination|transfer)"
        r"|\b(?:change|replace|discard)\s+tips?\s+(?:between|after)\s+"
        r"(?:each|every|both|the\s+two)\s+(?:destination|transfer)",
        cited_text, re.IGNORECASE,
    ))


def _rack_order(
    liquid_steps: list[dict[str, Any]], materials: dict[str, dict[str, Any]],
    manifest: Mapping[str, Any], has_repeat: bool, plan: Mapping[str, Any],
) -> tuple[list[str] | None, list[str], list[dict[str, Any]]]:
    """Pair distinct catalog racks to first source use, in plan rack order."""
    transfers = [step for step in liquid_steps if step.get("kind") in {"transfer", "distribute"}]
    if has_repeat or len(transfers) != len(liquid_steps) or not transfers:
        return None, ["Tip-rack order needs explicit review when mixing or repeating liquid steps."], []
    sources = list(dict.fromkeys(step.get("source") for step in transfers))
    if any(source not in materials for source in sources):
        return None, ["Each transfer or distribute source must identify a plan material."], []
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
                     if step.get("kind") in {"transfer", "distribute"} and step.get("source") == source]
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
        if pair is None or pair.get("execution_ready") is not True:
            blocks.append(f"{rack.get('id') or '(unnamed rack)'} has no exact verified rack/tip pair for this head.")
            continue
        source_steps = [step for step in transfers if step.get("source") == source_id]
        for step in source_steps:
            if step.get("kind") == "distribute":
                dispenses = _rows(step.get("dispenses"))
                volumes = [item.get("volume_ul") for item in dispenses]
                valid_volume = bool(volumes) and all(_positive(volume) for volume in volumes)
                # A distribute intent may compile as paired transfers when a
                # shared aspiration cannot fit. Rack suitability therefore
                # checks each dispense, not the sum of the sequence.
                required_tip_volume = max(volumes) if valid_volume else None
                volume_path = f"{step['_path']}/dispenses"
                addressed_materials = [source_id, *(item.get("destination") for item in dispenses)]
            else:
                required_tip_volume = step.get("volume_ul")
                valid_volume = _positive(required_tip_volume)
                volume_path = f"{step['_path']}/volume_ul"
                addressed_materials = [source_id, step.get("destination")]
            if not valid_volume or (
                _positive(pair.get("tip_capacity_ul")) and required_tip_volume > pair["tip_capacity_ul"]
            ):
                blocks.append(f"{volume_path} needs confirmed volumes within {rack.get('tip_definition_id')} capacity.")
            for material_id in addressed_materials:
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


def recommend_setup(plan: dict, setup: dict, manifest: dict, *, source: dict | None = None,
                    runtime_snapshot: dict | None = None) -> dict:
    """Return rule-backed setup proposals and remaining scientist inputs.

    Source paragraphs can establish an explicit all-wells instruction when
    cited by a step. Conversational text cannot certify tip reuse or pipetting
    height. The returned proposals are review drafts, never applied changes.
    """
    plan, setup, manifest = _data(plan), _data(setup), _data(manifest)
    source = _data(source)
    deck_slots = {slot for slot in (_data(manifest.get("machine")).get("deck_slots") or [])
                  if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0}
    runtime_advisory = _runtime_advisory(runtime_snapshot, deck_slots)
    material_rows = _rows(plan.get("materials"))
    materials = {row["id"]: row for row in material_rows if isinstance(row.get("id"), str) and row["id"]}
    liquid_steps, has_repeat, expanded_count = _steps(plan)
    legacy_liquid_steps = any(step.get("method_ref") is None for step in liquid_steps)
    head = _head(manifest)
    full_head, head_evidence, head_blocks = _full_head_footprint(liquid_steps, materials, manifest, head, source, plan)
    rack_ids, rack_blocks, rack_evidence = _rack_order(liquid_steps, materials, manifest, has_repeat, plan)
    tip_choices = {(row.get("labware_id"), row.get("tip_definition_id")): row
                   for row in _rows(manifest.get("tipbox_choices"))}
    st10_384_racks = rack_ids is not None and all(
        (rack := materials.get(rack_id, {})).get("tip_definition_id") == "st_10ul"
        and (choice := tip_choices.get((rack.get("labware_id"), rack.get("tip_definition_id")), {})).get("rows") == 16
        and choice.get("cols") == 24
        and choice.get("execution_ready") is True
        for rack_id in rack_ids
    )
    authorized, authorization_evidence = _authorized_reuse(plan, setup)
    empty_destinations, destination_disjoint, destination_evidence = _destinations_empty_for_each_source(
        liquid_steps, materials, manifest, head, full_head,
    )
    four_source_scope, scope_paragraph_ids, cited_scope_text = _cited_quadrant_scope(
        liquid_steps, materials, manifest, source,
    )
    five_ul_pairings = four_source_scope and all(
        len(source_steps := [step for step in liquid_steps if step.get("source") == source_id]) == 2
        and all(_positive(step.get("volume_ul")) and math.isclose(step["volume_ul"], 5.0, abs_tol=1e-9)
                for step in source_steps)
        for source_id in {step.get("source") for step in liquid_steps}
    )
    explicit_fresh_tips = _explicit_fresh_tip_requirement(cited_scope_text)
    source_ids = list(dict.fromkeys(step.get("source") for step in liquid_steps
                                    if step.get("kind") in {"transfer", "distribute"}))
    catalog_labware = {row.get("id"): row for row in _rows(manifest.get("labware")) if row.get("id")}
    def addressed_materials(step: Mapping[str, Any]) -> list[Any]:
        if step.get("kind") == "transfer":
            return [step.get("source"), step.get("destination")]
        if step.get("kind") == "distribute":
            return [step.get("source"), *(item.get("destination") for item in _rows(step.get("dispenses")))]
        return [step.get("material")]

    addressed_ids = list(dict.fromkeys(material_id for step in liquid_steps
                                       for material_id in addressed_materials(step)))
    destination_ids = list(dict.fromkeys(
        destination for step in liquid_steps if step.get("kind") in {"transfer", "distribute"}
        for destination in ([step.get("destination")] if step.get("kind") == "transfer" else
                            [item.get("destination") for item in _rows(step.get("dispenses"))])
    ))
    no_recorded_destination_liquid = bool(destination_ids) and all(
        destination in materials
        and materials[destination].get("initial_volume_ul") in (None, 0)
        and all(value == 0 for value in _data(materials[destination].get("well_volumes_ul")).values())
        for destination in destination_ids
    )
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
        "st10_384_dedicated_racks": st10_384_racks,
        "destination_initially_empty": empty_destinations,
        "destination_footprints_disjoint": destination_disjoint,
        "no_recorded_destination_liquid": no_recorded_destination_liquid,
        "four_source_quadrant_recipe": four_source_scope,
        "four_source_five_ul_pairings": five_ul_pairings,
        "same_source_reuse_prohibited": authorized is False or explicit_fresh_tips,
        "same_source_reuse_authorized": authorized,
        "tip_strategy": setup.get("tip_strategy"),
        "waste_material_present": waste_present,
        "minimum_addressed_well_depth_mm": minimum_depth,
    }
    evidence_by_fact = {
        "full_head_footprint": head_evidence,
        "dedicated_tip_rack_per_source": rack_evidence,
        "st10_384_dedicated_racks": rack_evidence,
        "same_source_reuse_authorized": [authorization_evidence] if authorization_evidence else [],
        "destination_initially_empty": destination_evidence,
        "destination_footprints_disjoint": destination_evidence,
        "no_recorded_destination_liquid": [
            {"source": "plan", "destination_material_id": destination,
             "recorded_initial_volume_ul": materials[destination].get("initial_volume_ul"),
             "missing_starting_volume_is_not_empty_confirmation": True}
            for destination in destination_ids if destination in materials
        ],
        "four_source_quadrant_recipe": ([{
            "source": "cited_protocol_text", "paragraph_ids": scope_paragraph_ids,
            "recipe_id": "four_384_to_1536_quadrants", "recipe_version": "1.0.0",
            "source_count": 4, "destination_count": 2,
        }] if four_source_scope else []),
        "four_source_five_ul_pairings": ([{
            "source": "plan_transfer_volumes", "per_source_transfer_count": 2,
            "per_transfer_volume_ul": 5,
        }] if five_ul_pairings else []),
    }
    recommendations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    suggested_paths: set[str] = set()
    unresolved_paths: set[str] = set()

    rules = _rows(manifest.get("setup_decision_rules"))

    def eligible(rule: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]]] | None:
        path = rule.get("decision")
        conditions = _data(rule.get("when")).get("all")
        if not isinstance(path, str) or not path.startswith("/setup/") or not isinstance(conditions, list):
            return None
        # A pinned method supplies phase-specific classes and heights. Keep
        # protocol-wide fields only for drafts that still contain legacy
        # liquid steps, so recommendations do not ask for redundant settings.
        if not legacy_liquid_steps and path in {"/setup/liquid_class", "/setup/distance_from_bottom_mm"}:
            return None
        if not all(isinstance(condition, Mapping) and _condition_matches(condition, facts)
                   for condition in conditions):
            return None
        if not _missing(_set_up_value(setup, path)):
            return None
        return path, conditions

    def rule_value(rule: Mapping[str, Any]) -> Any:
        raw_value = rule.get("recommendation")
        if isinstance(raw_value, Mapping) and raw_value.get("resolver") == "source_ordered_verified_tip_rack_ids":
            return rack_ids
        return raw_value

    def rule_result(rule: Mapping[str, Any], path: str,
                    conditions: list[Mapping[str, Any]]) -> dict[str, Any]:
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
        return result

    # Evaluate rules to a fixed point. A tip strategy proposed for review is
    # allowed to unlock its matching rack order and disposal proposal in this
    # *same response*; none of these values become saved setup or a scientist
    # decision here. Unresolved questions are evaluated only after closure.
    for _ in range(len(rules) + 1):
        changed = False
        for rule in rules:
            match = eligible(rule)
            if match is None:
                continue
            path, conditions = match
            if path in suggested_paths:
                continue
            value = rule_value(rule)
            if value is None:
                continue
            recommendations.append({**rule_result(rule, path, conditions), "value": value})
            suggested_paths.add(path)
            if path == "/setup/tip_strategy":
                facts["tip_strategy"] = value
            changed = True
        if not changed:
            break

    for rule in rules:
        match = eligible(rule)
        if match is None:
            continue
        path, conditions = match
        if path in suggested_paths or path in unresolved_paths or rule_value(rule) is not None:
            continue
        result = rule_result(rule, path, conditions)
        reason = result["rationale"]
        if path == "/setup/distance_from_bottom_mm":
            bounds = {"minimum_inclusive_mm": 0, "maximum_exclusive_mm": minimum_depth}
            if minimum_depth is not None:
                reason += f" Catalog geometry requires 0 ≤ height < {minimum_depth:g} mm."
            result = {**result, "bounds": bounds,
                      "evidence": result["evidence"] + [
                          {"source": "catalog", "material_id": mid,
                           "labware_id": materials.get(mid, {}).get("labware_id"),
                           "well_depth_mm": depth}
                          for mid, depth in zip(addressed_ids, addressed_depths)
                      ]}
        unresolved.append({**result, "reason": reason})
        unresolved_paths.add(path)

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
    if four_source_scope:
        # The recipe determines consumed volume, but the well's starting and
        # dead volumes are measured experimental facts. State the computable
        # lower bound without filling either field or certifying a source load.
        for source_id in source_ids:
            source_steps = [step for step in liquid_steps if step.get("source") == source_id]
            volumes = [step.get("volume_ul") for step in source_steps]
            if len(volumes) != 2 or not all(_positive(volume) for volume in volumes):
                continue
            source_material = materials.get(source_id, {})
            if source_material.get("initial_volume_ul") is not None and source_material.get("dead_volume_ul") is not None:
                continue
            index = next((position for position, row in enumerate(material_rows)
                          if row.get("id") == source_id), None)
            if index is None:
                continue
            transferable = float(sum(volumes))
            dead = source_material.get("dead_volume_ul")
            minimum_initial = transferable + dead if isinstance(dead, (int, float)) and not isinstance(dead, bool) else None
            catalog_row = catalog_labware.get(source_material.get("labware_id"), {})
            unresolved.append({
                "path": f"/materials/{index}/initial_volume_ul", "rule_id": "quadrant_source_volume_budget",
                "reason": (f"Each well of {source_id} supplies {volumes[0]:g} + {volumes[1]:g} = "
                           f"{transferable:g} µL. Confirm its measured starting volume and dead volume; "
                           f"starting volume must be at least {transferable:g} µL above dead volume."),
                "minimum_transferable_volume_ul": transferable,
                "minimum_initial_volume_ul_if_dead_known": minimum_initial,
                "catalog_well_capacity_ul": catalog_row.get("well_volume_ul"),
                "required_evidence": ["Measured or scientist-confirmed per-well starting volume.",
                                      "Dead volume for the selected source plate, tip, and aspiration method."],
                "evidence_level": "derived", "requires_confirmation": True,
                "provenance": ["config/protocol_recipes.yaml#four_384_to_1536_quadrants",
                               "cited_protocol_text", "plan_transfer_volumes", "catalog_labware"],
                "evidence": [{"source": "plan", "source_material_id": source_id,
                              "step_paths": [step["_path"] for step in source_steps],
                              "per_well_dispenses_ul": volumes,
                              "source_paragraph_ids": scope_paragraph_ids,
                              "catalog_labware_id": source_material.get("labware_id")}],
            })
    # Inventory is physical run state, never a catalog or model inference.
    tipbox_by_slot = {row["slot"]: row for row in runtime_advisory["tipbox_inventory"]}
    for index, row in enumerate(material_rows):
        if row.get("role") == "tips" and row.get("available_tips") is None:
            runtime_tipbox = tipbox_by_slot.get(row.get("deck_slot"))
            unresolved.append({"path": f"/materials/{index}/available_tips", "rule_id": None,
                               "reason": "Inspect and record which fresh tips are physically present before execution.",
                               "required_evidence": ["Scientist-inspected fresh-tip wells or a confirmed full rack."],
                               "evidence_level": "scientist_input", "requires_confirmation": True,
                               "provenance": ["physical_tip_inventory"],
                               "evidence": ([{**runtime_tipbox, "matched_by": "draft_deck_slot_only",
                                             "physically_verified": False}]
                                            if runtime_tipbox else [])})
    deck_recommendations, deck_blocked = _deck_proposals(plan, manifest, runtime_advisory)
    # Place deck choices before reuse decisions in the review UI. Accepting a
    # deck change updates the plan fingerprint, so reuse must be decided on the
    # final layout rather than an earlier draft.
    recommendations[:0] = deck_recommendations
    blocked.extend(deck_blocked)
    return {
        "context_hash": manifest.get("context_hash"),
        "plan_fingerprint": setup_plan_fingerprint(plan),
        "recommendations": recommendations,
        "unresolved": unresolved,
        "blocked": blocked,
        "runtime_snapshot": runtime_advisory,
    }
