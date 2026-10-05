"""Published capability choices stay within the reviewed compiler's reality."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import get_args

from pybravo.bravo import Bravo
from pybravo.workflow.protocols.capabilities import build_capability_manifest, compact_capability_options
from pybravo.workflow.protocols.compiler import ALLOWED_NODE_TYPES
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.models import ProtocolStep


def _operations(manifest: dict) -> dict[str, dict]:
    return {row["id"]: row for row in manifest["assistant_operations"]}


def test_live_context_manifest_is_sanitized_and_tracks_compiler_contract():
    bravo = Bravo(mode="simulation")
    try:
        context = machine_context(bravo)
        before = copy.deepcopy(context)
        manifest = build_capability_manifest(context)
    finally:
        bravo.disconnect()

    assert context == before
    assert manifest["context_hash"] == context["context_hash"]
    assert manifest["machine"]["head_type"] == context["head_type"]
    assert "profile" not in manifest
    assert "tip_offsets" not in manifest
    assert "connected" not in manifest
    assert {row["id"] for row in manifest["assistant_operations"]} == set(
        get_args(ProtocolStep.model_fields["kind"].annotation)
    )
    assert {node for row in manifest["assistant_operations"] for node in row["lowers_to"]} <= ALLOWED_NODE_TYPES
    assert all(not row["model_selectable"] for row in manifest["robot_operations"])
    assert _operations(manifest)["transfer"]["lowers_to"] == ["liquid/Aspirate", "liquid/Dispense"]
    assert "aspirate" not in _operations(manifest)
    assert "dispense" not in _operations(manifest)
    # Catalog tuples and mappings are returned as JSON-native, detached data.
    json.dumps(manifest, allow_nan=False)
    assert isinstance(manifest["tip_definitions"][0]["compatible_heads"], list)


def test_box_and_tip_options_are_independent_and_provisional_rack_is_not_promoted():
    bravo = Bravo(mode="simulation")
    try:
        context = machine_context(bravo)
    finally:
        bravo.disconnect()
    # A stale caller must not turn a visually provisional box into a verified pair.
    context["tipbox_choices"].append({
        "labware_id": "lw-96st-provisional", "tip_definition_id": "st_10ul",
        "execution_ready": True,
    })
    manifest = build_capability_manifest(context)
    choices = {(row["labware_id"], row["tip_definition_id"]): row for row in manifest["tipbox_choices"]}
    assert ("lw-96st-provisional", "st_10ul") not in choices
    assert choices[("lw-4914769d0af7", "st_10ul")]["execution_ready"] is True
    assert choices[("lw-4914769d0af7", "st_70ul")]["execution_ready"] is False
    assert "tip_length" in choices[("lw-4914769d0af7", "st_70ul")]["missing_metadata"]
    provisional = next(row for row in manifest["tipbox_catalog_candidates"]
                       if row["labware_id"] == "lw-96st-provisional")
    assert provisional["execution_ready"] is False
    assert provisional["status"] == "catalog_candidate"


def test_configured_head_and_gripper_gate_only_relevant_assistant_intents():
    context = {"head_type": "HT_384_PINTOOL", "has_gripper": False, "context_hash": "pin-tool"}
    manifest = build_capability_manifest(context)
    operations = _operations(manifest)
    for identity in ("transfer", "mix", "move_plate", "stack_plate", "destack_plate"):
        assert operations[identity]["selectable"] is False
        assert operations[identity]["availability"] == "unavailable"
    for identity in ("wait", "manual", "repeat"):
        assert operations[identity]["selectable"] is True
    assert manifest["tipbox_choices"] == []
    assert manifest["transfer_patterns"] == []
    assert all(row["availability"] == "unavailable" for row in manifest["robot_operations"]
               if row["id"].startswith(("tips/", "liquid/", "plate/")))


def test_quadrant_option_requires_catalog_geometry_and_384_disposable_head():
    source = {"id": "384-source", "base_class": "microplate", "wells": 384,
              "rows": 16, "cols": 24, "spacing_x_mm": 4.5, "spacing_y_mm": 4.5}
    destination = {"id": "1536-destination", "base_class": "microplate", "wells": 1536,
                   "rows": 32, "cols": 48, "spacing_x_mm": 2.25, "spacing_y_mm": 2.25}
    context = {"head_type": "HT_384_D_70", "has_gripper": True,
               "labware": [source, destination], "context_hash": "384-geometry"}
    manifest = build_capability_manifest(context)
    assert manifest["transfer_patterns"] == [{
        "id": "384_to_1536_quadrant",
        "description": "A full 384-channel source footprint maps to one 1536-well quadrant.",
        "source_labware_wells": 384, "destination_labware_wells": 1536,
        "source_anchor": "A1", "destination_anchors": ["A1", "A2", "B1", "B2"],
        "status": "geometry_option", "review_required": True,
    }]
    assert _operations(manifest)["transfer"]["selectable"] is True
    # The model may propose transfer despite absent class/inventory; these
    # stay explicit review requirements, not fabricated filled tip racks.
    assert manifest["tipbox_choices"] == []
    assert manifest["liquid_classes"] == []
    destination["provisional"] = True
    assert build_capability_manifest(context)["transfer_patterns"] == []


def test_compact_options_fit_small_local_model_budget_without_catalog_duplication():
    bravo = Bravo(mode="simulation")
    try:
        manifest = build_capability_manifest(machine_context(bravo))
    finally:
        bravo.disconnect()
    compact = compact_capability_options(manifest)
    assert len(json.dumps(compact, separators=(",", ":"))) < 3_200
    assert "labware" not in compact
    assert "tip_definitions" not in compact
    assert "tipbox_choices" not in compact
    assert "robot_operations" not in compact
    assert compact["assistant_operations"][0]["lowers_to"] == ["liquid/Aspirate", "liquid/Dispense"]


def test_compact_setup_options_distinguish_literal_disposal_from_material_reference():
    bravo = Bravo(mode="simulation")
    try:
        manifest = build_capability_manifest(machine_context(bravo))
    finally:
        bravo.disconnect()
    full = manifest["setup_options"]
    compact = compact_capability_options(manifest)["setup_options"]

    disposals = {row["id"]: row for row in compact["tip_disposals"]}
    assert "waste_material" not in disposals
    assert disposals["waste_container"] == {
        "id": "waste_container", "value_kind": "material_id",
        "required_material_role": "waste", "setup_field": "tip_disposal_id",
    }
    assert disposals["return_to_source_rack"] == {
        "id": "return_to_source_rack", "value_kind": "literal",
        "setup_value": "return_to_source_rack", "setup_field": "tip_disposal_id",
        "requires_tip_strategy": "fresh_each_source",
    }
    assert "setup_value" not in disposals["waste_container"]
    assert {row["id"]: row.get("requires_reason", False)
            for row in compact["tip_strategies"]} == {
                "fresh_each_step": False, "fresh_each_source": True, "reuse_all": True,
            }

    modes = {row["id"]: row for row in compact["head_modes"]}
    assert set(modes) == {"all_barrels", "single_barrel", "row", "column", "rectangle"}
    for mode in modes.values():
        assert mode["requires_confirmation"] is True
        assert mode["compatible_tip_pair_check_required"] is True
        assert mode["required_fields"][:2] == ["subset_type", "subset_config"]
    assert modes["all_barrels"]["required_fields"] == ["subset_type", "subset_config"]
    assert modes["row"]["required_fields"][-1] == "row_count"
    assert modes["column"]["required_fields"][-1] == "column_count"
    assert modes["rectangle"]["required_fields"][-2:] == ["row_count", "column_count"]
    assert next(row for row in full["head_modes"] if row["id"] == "all_barrels")["subset_config_values"] == ["back_left"]
    assert next(row for row in manifest["tipbox_choices"] if row.get("tip_row_stride") == 2)["required_head_mode"] == "all_barrels"


def test_tip_to_1536_relation_is_typed_and_never_qualifies_execution():
    profile = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
    bravo = Bravo(profile=profile)
    try:
        context = machine_context(bravo)
    finally:
        bravo.disconnect()
    manifest = build_capability_manifest(context)
    relations = {row["tip_definition_id"]: row for row in manifest["tip_plate_compatibility"]}
    assert relations["st_70ul"]["compatible"] is False
    assert relations["st_70ul"]["status"] == "incompatible"
    assert relations["st_10ul"]["compatible"] is True
    assert relations["st_10ul"]["status"] == "planning_compatible"
    assert relations["st_10ul"]["target_labware_id"] == "lw-3918306f45b8"
    assert relations["st_10ul"]["applies_to"] == ["source", "destination", "mix"]
    assert all(row["execution_ready"] is False for row in relations.values())
    compact = compact_capability_options(manifest)
    assert len(json.dumps(compact, separators=(",", ":"))) < 3_200
    assert {row["tip_definition_id"]: row["compatible"]
            for row in compact["tip_plate_compatibility"]} == {"st_10ul": True, "st_70ul": False}

    # A stale supplied choice cannot create a positive relation after the
    # authoritative rack-to-tip link is removed from the catalog snapshot.
    rack = next(row for row in context["labware"] if row["id"] == "lw-4914769d0af7")
    rack["supported_tip_ids"] = ["st_70ul"]
    rack["tip_definition_id"] = "st_70ul"
    assert [row["tip_definition_id"] for row in build_capability_manifest(context)["tip_plate_compatibility"]] == ["st_70ul"]


def test_tip_to_1536_relation_does_not_promote_provisional_target_plate():
    context = {"head_type": "HT_384_D_70", "has_gripper": True,
               "labware": [{"id": "candidate-1536", "base_class": "microplate", "wells": 1536,
                            "rows": 32, "cols": 48, "spacing_x_mm": 2.25, "spacing_y_mm": 2.25,
                            "well_volume_ul": 6.0, "provisional": True}],
               "tip_definitions": [{"id": "st_70ul", "capacity_ul": 70.0,
                                    "compatible_heads": ["HT_384_D_70"]}]}
    assert build_capability_manifest(context)["tip_plate_compatibility"] == []
