"""Setup recommendations explain catalog-backed choices without certifying a run."""

from __future__ import annotations

import copy
from pathlib import Path

from pybravo.bravo import Bravo
from pybravo.workflow.protocols.capabilities import build_capability_manifest
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.setup_recommendations import recommend_setup, setup_plan_fingerprint


def _fixture() -> tuple[dict, dict, dict]:
    profile = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
    bravo = Bravo(profile=profile)
    try:
        manifest = build_capability_manifest(machine_context(bravo))
    finally:
        bravo.disconnect()
    source_plate = next(row for row in manifest["labware"] if row.get("wells") == 384
                        and row.get("base_class") == "microplate" and not row.get("provisional")
                        and row.get("rows") == 16 and row.get("cols") == 24
                        and row.get("spacing_x_mm") == 4.5 and row.get("spacing_y_mm") == 4.5)
    target_plate = next(row for row in manifest["labware"] if row.get("wells") == 1536
                        and row.get("base_class") == "microplate" and not row.get("provisional")
                        and row.get("rows") == 32 and row.get("cols") == 48
                        and row.get("spacing_x_mm") == 2.25 and row.get("spacing_y_mm") == 2.25)
    pair = next(row for row in manifest["tipbox_choices"] if row["tip_definition_id"] == "st_10ul"
                and row["rows"] == 16 and row["cols"] == 24 and row["execution_ready"])
    materials = [
        {"id": f"source_{number}", "role": "liquid", "labware_id": source_plate["id"]}
        for number in (2, 1)
    ] + [
        {"id": f"destination_{number}", "role": "liquid", "labware_id": target_plate["id"],
         "initial_volume_ul": 0, "well_volumes_ul": {}}
        for number in (1, 2)
    ] + [
        {"id": f"tips_source_{number}", "role": "tips", "labware_id": pair["labware_id"],
         "tip_definition_id": pair["tip_definition_id"], "available_tips": None}
        for number in (2, 1)
    ]
    for slot, material in enumerate(materials, start=1):
        material["deck_slot"] = slot
    steps = [
        {"id": f"source_{source}_to_destination_{destination}", "kind": "transfer",
         "description": "Transfer every source well into its destination quadrant.",
         "source": f"source_{source}", "destination": f"destination_{destination}",
         "source_anchor": "A1", "destination_anchor": anchor, "volume_ul": 5.0}
        for source, anchor in ((2, "A2"), (1, "A1")) for destination in (1, 2)
    ]
    plan = {"name": "Two source quadrant transfer", "materials": materials, "steps": steps, "decisions": []}
    fingerprint = setup_plan_fingerprint(plan)
    plan["decisions"] = [
        {"path": "/setup/same_source_reuse_authorized",
         "value": {"authorized": True, "plan_fingerprint": fingerprint},
         "actor": "scientist", "reason": "Each source has dedicated tips; both destinations are empty."},
        {"path": "/setup/full_head_footprint_authorized",
         "value": {"authorized": True, "plan_fingerprint": fingerprint},
         "actor": "scientist", "reason": "Run all wells of each 384 source plate."},
    ]
    return plan, {}, manifest


def _values(result: dict) -> dict[str, object]:
    return {row["path"]: row["value"] for row in result["recommendations"]}


def test_catalog_dead_volume_is_only_proposed_for_sources_and_placeholder_stays_unresolved():
    plan, setup, manifest = _fixture()
    source = plan["materials"][0]
    catalog = next(row for row in manifest["labware"] if row["id"] == source["labware_id"])
    catalog.update(dead_volume_ul=7.5, dead_volume_status="placeholder")
    before = copy.deepcopy(plan)
    result = recommend_setup(plan, setup, manifest)
    proposals = [row for row in result["recommendations"] if row["rule_id"] == "catalog_source_dead_volume"]
    assert {row["path"] for row in proposals} == {"/materials/0/dead_volume_ul", "/materials/1/dead_volume_ul"}
    assert all(row["value"] == 7.5 and row["catalog_dead_volume_status"] == "placeholder"
               and row["requires_confirmation"] for row in proposals)
    assert all("method suitability separately" in row["rationale"] for row in proposals)
    assert plan == before

    source["dead_volume_ul"] = 9.0
    result = recommend_setup(plan, setup, manifest)
    assert any(row["path"] == "/materials/0/dead_volume_ul" and
               row["catalog_dead_volume_status"] == "placeholder" for row in result["unresolved"])

    catalog["dead_volume_status"] = "reviewed"
    source["dead_volume_ul"] = None
    result = recommend_setup(plan, setup, manifest)
    reviewed = next(row for row in result["recommendations"]
                    if row["path"] == "/materials/0/dead_volume_ul")
    assert reviewed["catalog_dead_volume_status"] == "reviewed"


def test_full_quadrant_plan_yields_rule_backed_setup_proposals_and_scientist_inputs():
    plan, setup, manifest = _fixture()
    before = copy.deepcopy((plan, setup, manifest))
    result = recommend_setup(plan, setup, manifest)
    assert result["plan_fingerprint"] == setup_plan_fingerprint(plan)
    values = _values(result)
    catalog_dead = next(row["dead_volume_ul"] for row in manifest["labware"]
                        if row["id"] == plan["materials"][0]["labware_id"])
    assert values == {
        "/materials/0/dead_volume_ul": catalog_dead,
        "/materials/1/dead_volume_ul": catalog_dead,
        "/setup/head_mode": {"subset_type": "all_barrels", "subset_config": "back_left",
                             "row_count": None, "column_count": None},
        "/setup/tip_strategy": "fresh_each_source",
        "/setup/tip_rack_ids": ["tips_source_2", "tips_source_1"],
        "/setup/tip_disposal_id": "return_to_source_rack",
    }
    assert {row["rule_id"] for row in result["recommendations"]} == {
        "head_mode_full_footprint", "tip_strategy_dedicated_source",
        "tip_rack_order_by_source", "tip_disposal_return_to_source",
        "catalog_source_dead_volume",
    }
    assert all(row["requires_confirmation"] and row["provenance"] for row in result["recommendations"])
    assert {row["path"] for row in result["unresolved"]} >= {
        "/setup/liquid_class", "/setup/distance_from_bottom_mm",
        "/materials/4/available_tips", "/materials/5/available_tips",
    }
    assert result["blocked"] == []
    assert (plan, setup, manifest) == before

    # A cascade is review-only; it has not changed setup or confirmed a rack.
    assert setup == {}
    setup["tip_strategy"] = "fresh_each_source"
    next_result = recommend_setup(plan, setup, manifest)
    assert _values(next_result)["/setup/tip_rack_ids"] == ["tips_source_2", "tips_source_1"]
    assert _values(next_result)["/setup/tip_disposal_id"] == "return_to_source_rack"
    assert next(row for row in next_result["recommendations"] if row["path"] == "/setup/tip_rack_ids")["evidence_level"] == "heuristic"
    assert "/setup/tip_reuse_reason" in {row["path"] for row in next_result["unresolved"]}


def test_pinned_methods_do_not_request_protocol_wide_liquid_settings():
    plan, setup, manifest = _fixture()
    for step in plan["steps"]:
        step["method_ref"] = {"method_id": "reviewed:test", "revision": "pinned"}
    result = recommend_setup(plan, setup, manifest)
    unresolved_paths = {row["path"] for row in result["unresolved"]}
    assert "/setup/liquid_class" not in unresolved_paths
    assert "/setup/distance_from_bottom_mm" not in unresolved_paths
    assert "/materials/4/available_tips" in unresolved_paths


def test_empty_destinations_do_not_authorize_tip_reuse_or_invent_assessment():
    plan, setup, manifest = _fixture()
    plan["decisions"] = []
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"text": "The destinations are empty, so do not worry about cross contamination."}
    ]})
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_disposal_id" not in _values(result)
    assert "/setup/tip_strategy" in {row["path"] for row in result["unresolved"]}
    assert "/setup/tip_rack_ids" not in _values(result)


def test_generic_rack_order_is_not_treated_as_a_source_pairing():
    plan, setup, manifest = _fixture()
    for number, material in enumerate(plan["materials"][-2:], start=1):
        material["id"] = f"rack_{number}"
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/tip_rack_ids" not in _values(result)
    assert "/setup/tip_strategy" not in _values(result)
    assert any("exact source ID" in item for row in result["blocked"] for item in row["missing"])


def test_structured_scientist_source_rack_mapping_resolves_generic_ids():
    plan, setup, manifest = _fixture()
    for number, material in enumerate(plan["materials"][-2:], start=1):
        material["id"] = f"rack_{number}"
    for decision in plan["decisions"]:
        decision["value"]["plan_fingerprint"] = setup_plan_fingerprint(plan)
    plan["decisions"].append({"path": "/setup/source_tip_rack_pairs",
                              "value": {"pairs": {"source_2": "rack_2", "source_1": "rack_1"},
                                        "plan_fingerprint": setup_plan_fingerprint(plan)},
                              "actor": "scientist", "reason": "Rack labels verified on deck."})
    setup["tip_strategy"] = "fresh_each_source"
    result = recommend_setup(plan, setup, manifest)
    assert _values(result)["/setup/tip_rack_ids"] == ["rack_2", "rack_1"]
    assert next(row for row in result["recommendations"] if row["path"] == "/setup/tip_rack_ids")["evidence_level"] == "derived"


def test_explicit_no_reuse_selects_fresh_step_without_assuming_tip_inventory():
    plan, setup, manifest = _fixture()
    plan["decisions"][0]["value"]["authorized"] = False
    result = recommend_setup(plan, setup, manifest)
    assert _values(result)["/setup/tip_strategy"] == "fresh_each_step"
    assert "/setup/tip_disposal_id" not in _values(result)
    assert any(row["path"].endswith("/available_tips") for row in result["unresolved"])


def test_missing_catalog_identity_and_mixed_footprints_block_full_head_inference():
    plan, setup, manifest = _fixture()
    plan["materials"][0]["labware_id"] = "missing-plate"
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/head_mode" not in _values(result)
    assert any(row["path"] == "/setup/head_mode" for row in result["blocked"])


def test_a1_on_a_same_pitch_plate_does_not_prove_all_wells_intent():
    plan, setup, manifest = _fixture()
    plan["decisions"] = [decision for decision in plan["decisions"]
                         if decision["path"] != "/setup/full_head_footprint_authorized"]
    source_labware_id = plan["materials"][0]["labware_id"]
    for material in plan["materials"]:
        if material["id"].startswith("destination_"):
            material["labware_id"] = source_labware_id
    for step in plan["steps"]:
        step["destination_anchor"] = "A1"
        step["description"] = "Transfer from A1."
        step["source_paragraph_ids"] = ["p1"]
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Transfer A1 only."}
    ]})
    assert "/setup/head_mode" not in _values(result)
    assert any("cited all-wells" in item for row in result["blocked"] for item in row["missing"])

    for step in plan["steps"]:
        step["description"] = "Transfer every source well into the destination plate."
    # Model-generated description alone is insufficient; cited source text
    # must actually say that all wells are intended.
    assert "/setup/head_mode" not in _values(recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Transfer A1 only."}
    ]}))
    assert _values(recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Transfer every source well to the destination plate."}
    ]}))["/setup/head_mode"]["subset_type"] == "all_barrels"

    plan, setup, manifest = _fixture()
    plan["steps"][1]["destination_anchor"] = "Z48"
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/head_mode" not in _values(result)
    assert any(row["path"] == "/setup/head_mode" for row in result["blocked"])


def test_catalog_quadrant_pattern_does_not_override_single_well_scope():
    plan, setup, manifest = _fixture()
    plan["decisions"] = [decision for decision in plan["decisions"]
                         if decision["path"] != "/setup/full_head_footprint_authorized"]
    for step in plan["steps"]:
        step["description"] = "Transfer from A1 to the destination quadrant."
        step["source_paragraph_ids"] = ["p1"]
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Do not transfer all wells; transfer A1 only."}
    ]})
    assert "/setup/head_mode" not in _values(result)
    assert any("catalog pattern alone" in item for row in result["blocked"] for item in row["missing"])


def test_all_wells_except_a1_is_not_full_head_scope():
    plan, setup, manifest = _fixture()
    plan["decisions"] = [decision for decision in plan["decisions"]
                         if decision["path"] != "/setup/full_head_footprint_authorized"]
    for step in plan["steps"]:
        step["source_paragraph_ids"] = ["p1"]
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Transfer all wells except A1 to each destination."}
    ]})
    assert "/setup/head_mode" not in _values(result)
    assert any(row["path"] == "/setup/head_mode" for row in result["blocked"])


def test_affirmative_cited_full_scope_is_heuristic_until_structured_scientist_decision():
    plan, setup, manifest = _fixture()
    plan["decisions"] = [decision for decision in plan["decisions"]
                         if decision["path"] != "/setup/full_head_footprint_authorized"]
    for step in plan["steps"]:
        step["source_paragraph_ids"] = ["p1"]
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
        {"id": "p1", "text": "Transfer every source well into a quadrant."}
    ]})
    head = next(row for row in result["recommendations"] if row["path"] == "/setup/head_mode")
    assert head["evidence_level"] == "heuristic"
    assert head["requires_confirmation"] is True


def _four_source_fixture() -> tuple[dict, dict, dict]:
    plan, setup, manifest = _fixture()
    source_template = next(row for row in plan["materials"] if row["id"] == "source_2")
    rack_template = next(row for row in plan["materials"] if row["id"] == "tips_source_2")
    for number in (3, 4):
        source_material = copy.deepcopy(source_template)
        source_material["id"] = f"source_{number}"
        rack_material = copy.deepcopy(rack_template)
        rack_material["id"] = f"tips_source_{number}"
        plan["materials"].extend([source_material, rack_material])
    anchors = {1: "A1", 2: "A2", 3: "B1", 4: "B2"}
    plan["steps"] = [
        {"kind": "transfer", "id": f"transfer_{number}_{destination}",
         "description": "Transfer the plate into a destination quadrant.",
         "source": f"source_{number}", "destination": f"destination_{destination}",
         "source_anchor": "A1", "destination_anchor": anchors[number],
         "volume_ul": 5.0, "source_paragraph_ids": ["p1"]}
        for number in (4, 3, 2, 1) for destination in (1, 2)
    ]
    plan["decisions"] = []
    return plan, setup, manifest


def test_exact_four_source_two_destination_quadrant_request_is_reviewable_heuristic():
    plan, setup, manifest = _four_source_fixture()
    for material in plan["materials"]:
        if material["id"].startswith("destination_"):
            material["initial_volume_ul"] = None
    result = recommend_setup(plan, setup, manifest, source={"paragraphs": [{
        "id": "p1", "text": "I have 4 384 well plates and want to transfer 5 ul from each plate "
        "into the 4 quadrants of two 1536 plates."
    }]})
    head = next(row for row in result["recommendations"] if row["path"] == "/setup/head_mode")
    assert head["value"] == {"subset_type": "all_barrels", "subset_config": "back_left",
                             "row_count": None, "column_count": None}
    assert head["evidence_level"] == "heuristic"
    values = _values(result)
    assert values["/setup/tip_strategy"] == "fresh_each_source"
    assert values["/setup/tip_rack_ids"] == [
        "tips_source_4", "tips_source_3", "tips_source_2", "tips_source_1",
    ]
    assert values["/setup/tip_disposal_id"] == "return_to_source_rack"
    strategy = next(row for row in result["recommendations"] if row["path"] == "/setup/tip_strategy")
    assert strategy["rule_id"] == "tip_strategy_quadrant_conditional"
    assert strategy["requires_confirmation"] is True
    assert "conditional" in strategy["rationale"]
    assert "two separate 5 µL aspirations" in strategy["rationale"]
    assert any(item["source"] == "cited_protocol_text" for item in strategy["evidence"])
    assert any(item.get("missing_starting_volume_is_not_empty_confirmation")
               for item in strategy["evidence"])
    assert "/setup/tip_reuse_reason" in {item["path"] for item in result["unresolved"]}
    volume_questions = [item for item in result["unresolved"]
                        if item.get("rule_id") == "quadrant_source_volume_budget"]
    assert len(volume_questions) == 4
    assert all(item["minimum_transferable_volume_ul"] == 10 for item in volume_questions)
    assert all(item["minimum_initial_volume_ul_if_dead_known"] is None for item in volume_questions)
    assert all(material.get("dead_volume_ul") is None for material in plan["materials"]
               if material["id"].startswith("source_"))
    assert setup == {}


def test_quadrant_tip_reuse_candidate_abstains_on_known_destination_liquid_or_fresh_tip_instruction():
    plan, setup, manifest = _four_source_fixture()
    cited = {"paragraphs": [{"id": "p1", "text": "I have 4 384 well plates and transfer 5 ul "
              "from each plate into the 4 quadrants of two 1536 plates."}]}
    plan["materials"][2]["initial_volume_ul"] = 2
    result = recommend_setup(plan, setup, manifest, source=cited)
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_rack_ids" not in _values(result)

    plan["materials"][2]["initial_volume_ul"] = 0
    cited["paragraphs"][0]["text"] += " Use fresh tips for each transfer."
    result = recommend_setup(plan, setup, manifest, source=cited)
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_rack_ids" not in _values(result)


def test_four_source_scope_abstains_on_negated_or_hypothetical_transfer():
    plan, setup, manifest = _four_source_fixture()
    for cited_text in (
        "I have 4 384 plates and 2 1536 plates. Do not transfer each plate into quadrants.",
        "I have 4 384 plates and 2 1536 plates. No transfer from each plate into quadrants.",
        "I have 4 384 plates and 2 1536 plates. A transfer from each plate into "
        "the four quadrants is only a possible future layout.",
    ):
        result = recommend_setup(plan, setup, manifest, source={"paragraphs": [
            {"id": "p1", "text": cited_text},
        ]})
        assert "/setup/head_mode" not in _values(result), cited_text
        assert any(row["path"] == "/setup/head_mode" for row in result["blocked"]), cited_text


def test_plan_edit_invalidates_bound_scope_and_reuse_decisions():
    plan, setup, manifest = _fixture()
    plan["steps"][0]["destination_anchor"] = "B2"
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/head_mode" not in _values(result)
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_strategy" in {row["path"] for row in result["unresolved"]}


def test_overlapping_source_quadrants_are_not_treated_as_empty_per_source():
    plan, setup, manifest = _fixture()
    for step in plan["steps"]:
        step["destination_anchor"] = "A1"
    for decision in plan["decisions"]:
        decision["value"]["plan_fingerprint"] = setup_plan_fingerprint(plan)
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/head_mode" in _values(result)
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_disposal_id" not in _values(result)


def test_tip_plate_exclusion_prevents_dedicated_rack_order_proposal():
    plan, setup, manifest = _fixture()
    pair = next(row for row in manifest["tipbox_choices"] if row["tip_definition_id"] == "st_70ul")
    for material in plan["materials"]:
        if material["role"] == "tips":
            material["labware_id"] = pair["labware_id"]
            material["tip_definition_id"] = pair["tip_definition_id"]
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/tip_rack_ids" not in _values(result)
    assert "/setup/tip_strategy" not in _values(result)
    assert any(row["path"] == "/setup/tip_rack_ids" for row in result["blocked"])


def test_existing_setup_is_preserved_and_manifest_rules_are_authoritative():
    plan, setup, manifest = _fixture()
    setup["head_mode"] = {"subset_type": "single_barrel", "subset_config": "front_left"}
    setup["liquid_class"] = "scientist-selected-class"
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/head_mode" not in _values(result)
    assert "/setup/liquid_class" not in {row["path"] for row in result["unresolved"]}

    plan, setup, manifest = _fixture()
    manifest["setup_decision_rules"] = []
    assert {row["path"] for row in recommend_setup(plan, setup, manifest)["recommendations"]} == {
        "/materials/0/dead_volume_ul", "/materials/1/dead_volume_ul",
    }


def test_unplaced_waste_material_does_not_block_safety_question_or_become_disposal():
    plan, setup, manifest = _fixture()
    plan["materials"].append({"id": "waste", "role": "waste", "deck_slot": None, "labware_id": None})
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/tip_disposal_id" not in _values(result)
    assert "/setup/tip_disposal_id" in {row["path"] for row in result["unresolved"]}


def test_height_bounds_are_geometry_only_and_leave_qualified_value_blank():
    plan, setup, manifest = _fixture()
    result = recommend_setup(plan, setup, manifest)
    height = next(row for row in result["unresolved"] if row["path"] == "/setup/distance_from_bottom_mm")
    depths = [row["well_depth_mm"] for row in manifest["labware"]
              if row["id"] in {m["labware_id"] for m in plan["materials"] if m["role"] == "liquid"}]
    assert height["bounds"] == {"minimum_inclusive_mm": 0, "maximum_exclusive_mm": min(depths)}
    assert "/setup/distance_from_bottom_mm" not in _values(result)


def test_intervening_plate_move_blocks_claim_of_one_tip_set_per_source():
    plan, setup, manifest = _fixture()
    plan["steps"].insert(1, {"id": "move", "kind": "move_plate", "material": "source_2", "destination_slot": 6})
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/tip_strategy" not in _values(result)
    assert "/setup/tip_rack_ids" not in _values(result)
    assert any("handoff or plate move" in item for row in result["blocked"] for item in row["missing"])


def test_deck_slots_are_suggested_only_for_unplaced_catalog_materials_and_reserve_moves():
    plan, setup, manifest = _fixture()
    for index in (0, 2, 4):
        plan["materials"][index]["deck_slot"] = None
    plan["steps"].append({"id": "park", "kind": "move_plate", "material": "source_1",
                          "destination_slot": 7})
    result = recommend_setup(plan, setup, manifest)
    deck = {row["path"]: row for row in result["recommendations"]
            if row["path"].endswith("/deck_slot")}
    assert {path: row["value"] for path, row in deck.items()} == {
        "/materials/0/deck_slot": 5,
        "/materials/2/deck_slot": 3,
        "/materials/4/deck_slot": 1,
    }
    assert all(row["requires_confirmation"] and row["evidence_level"] == "heuristic"
               for row in deck.values())
    assert all(7 in row["evidence"][0]["reserved_move_slots"] for row in deck.values())
    assert plan["materials"][1]["deck_slot"] == 2


def test_software_deck_state_reserves_unassigned_slots_without_confirming_tip_inventory():
    plan, setup, manifest = _fixture()
    plan["materials"][4]["deck_slot"] = None
    original_manifest = copy.deepcopy(manifest)
    snapshot = {
        "deck": {"5": ["Software-assigned rack"], "6": ["Existing rack"]},
        "tipbox_inventory": {
            "5": {"labware_name": "Software-assigned rack", "tip_id": "st_10ul",
                  "rows": 16, "cols": 24, "occupied": ["0:0", "0:1"]},
            "6": {"labware_name": "Existing rack", "tip_id": "st_10ul",
                  "rows": 16, "cols": 24, "occupied": ["0:0"]},
        },
    }
    result = recommend_setup(plan, setup, manifest, runtime_snapshot=snapshot)
    deck = next(row for row in result["recommendations"]
                if row["path"] == "/materials/4/deck_slot")
    assert deck["value"] == 7
    assert deck["evidence"][0]["software_occupied_unassigned_slots"] == [5]
    assert deck["requires_confirmation"] is True
    runtime = result["runtime_snapshot"]
    assert runtime["status"] == "software_known_unverified"
    assert runtime["physically_verified"] is False
    assert {row["slot"] for row in runtime["occupied_slots"]} == {5, 6}
    assert runtime["tipbox_inventory"][0]["software_occupied_count"] == 2
    assert runtime["tipbox_inventory"][0]["occupied_wells"] == ["0:0", "0:1"]
    tip_questions = {row["path"]: row for row in result["unresolved"]
                     if row["path"].endswith("/available_tips")}
    assert set(tip_questions) == {"/materials/4/available_tips", "/materials/5/available_tips"}
    assert tip_questions["/materials/4/available_tips"]["evidence"] == []
    assert tip_questions["/materials/5/available_tips"]["evidence"][0]["matched_by"] == "draft_deck_slot_only"
    assert tip_questions["/materials/5/available_tips"]["evidence"][0]["physically_verified"] is False
    assert all(row["available_tips"] is None for row in plan["materials"] if row["role"] == "tips")
    assert manifest == original_manifest


def test_explicitly_ordered_four_source_stack_shares_one_suggested_slot():
    plan, setup, manifest = _four_source_fixture()
    for material in plan["materials"]:
        material["deck_slot"] = None
        if material["id"].startswith("source_"):
            material["stack_order"] = int(material["id"].split("_")[1]) - 1
    plan["steps"].extend([
        {"id": "reserve_7", "kind": "move_plate", "material": "source_1", "destination_slot": 7},
        {"id": "reserve_8", "kind": "destack_plate", "material": "source_2", "destination_slot": 8},
    ])
    result = recommend_setup(plan, setup, manifest)
    deck = {row["path"]: row["value"] for row in result["recommendations"]
            if row["path"].endswith("/deck_slot")}
    assert len(deck) == 10
    for index, material in enumerate(plan["materials"]):
        if material["id"].startswith("source_"):
            assert deck[f"/materials/{index}/deck_slot"] == 9
    assert {deck[f"/materials/{index}/deck_slot"] for index, material in enumerate(plan["materials"])
            if material["role"] == "tips"} == {1, 2, 3, 4}
    assert {deck[f"/materials/{index}/deck_slot"] for index, material in enumerate(plan["materials"])
            if material["id"].startswith("destination_")} == {5, 6}


def test_deck_recommender_abstains_for_unverified_rack_or_ambiguous_stack():
    plan, setup, manifest = _fixture()
    plan["materials"][4]["deck_slot"] = None
    plan["materials"][4]["tip_definition_id"] = "unknown-tip"
    result = recommend_setup(plan, setup, manifest)
    assert "/materials/4/deck_slot" not in _values(result)
    assert any(row["path"] == "/materials/4/deck_slot" for row in result["blocked"])

    plan, setup, manifest = _fixture()
    plan["materials"][0]["deck_slot"] = None
    plan["materials"][0]["stack_order"] = 1
    result = recommend_setup(plan, setup, manifest)
    assert "/materials/0/deck_slot" not in _values(result)
    assert any("Stack levels" in row["reason"] for row in result["blocked"])


def test_deck_recommender_does_not_partially_fill_overfull_layout():
    plan, setup, manifest = _four_source_fixture()
    for material in plan["materials"]:
        material["deck_slot"] = None
    plan["steps"].append({"id": "park", "kind": "move_plate", "material": "source_1",
                          "destination_slot": 9})
    result = recommend_setup(plan, setup, manifest)
    assert not any(row["path"].endswith("/deck_slot") for row in result["recommendations"])
    assert len([row for row in result["blocked"] if row["path"].endswith("/deck_slot")]) == 10


def test_distribute_sources_count_for_tip_rack_order_and_capacity():
    plan, setup, manifest = _fixture()
    plan["steps"] = [
        {"id": f"distribute_{source}", "kind": "distribute", "source": f"source_{source}",
         "source_anchor": "A1", "dispenses": [
             {"destination": f"destination_{destination}", "destination_anchor": anchor,
              "volume_ul": 5.0}
             for destination in (1, 2)
         ]}
        for source, anchor in ((2, "A2"), (1, "A1"))
    ]
    for decision in plan["decisions"]:
        decision["value"]["plan_fingerprint"] = setup_plan_fingerprint(plan)
    setup["tip_strategy"] = "fresh_each_source"
    result = recommend_setup(plan, setup, manifest)
    assert _values(result)["/setup/tip_rack_ids"] == ["tips_source_2", "tips_source_1"]
    assert "/setup/head_mode" in _values(result)
    assert not any(row["path"] == "/setup/tip_rack_ids" for row in result["blocked"])

    plan["steps"][0]["dispenses"][0]["volume_ul"] = 6.0
    result = recommend_setup(plan, setup, manifest)
    assert _values(result)["/setup/tip_rack_ids"] == ["tips_source_2", "tips_source_1"]

    plan["steps"][0]["dispenses"][0]["volume_ul"] = 11.0
    result = recommend_setup(plan, setup, manifest)
    assert "/setup/tip_rack_ids" not in _values(result)
    assert any("capacity" in item for row in result["blocked"] for item in row["missing"])
