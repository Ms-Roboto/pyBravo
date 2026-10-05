"""The reviewed protocol compiler never silently repairs scientific intent."""
from copy import deepcopy

import pytest

from pybravo.workflow.protocols.compiler import ALLOWED_NODE_TYPES, ProtocolCompilationError, compile_plan
from pybravo.workflow.protocols.validation import validate_plan


def protocol_fixture():
    plate = {"id": "plate", "name": "96 well plate", "kind": "sbs_plate", "base_class": "plate", "rows": 8, "cols": 12,
             "wells": 96, "spacing_x_mm": 9.0, "spacing_y_mm": 9.0, "well_volume_ul": 200.0, "well_depth_mm": 10.0}
    rack = {**plate, "id": "rack", "name": "96 tips", "kind": "tip_box", "base_class": "tip_box", "tip_definition_id": "tips-200", "supported_tip_ids": ["tips-200"]}
    context = {"head_type": "HT_96_D_200", "has_gripper": True, "labware": [plate, rack],
               "tip_definitions": [{"id": "tips-200", "capacity_ul": 200.0, "compatible_heads": ["HT_96_D_200"]}],
               "liquid_classes": [{"id": "water", "name": "Water"}]}
    plan = {"name": "Reviewed buffer transfer", "materials": [
        {"id": "buffer", "name": "Buffer", "labware_id": "plate", "deck_slot": 1, "initial_volume_ul": 100.0, "dead_volume_ul": 10.0},
        {"id": "samples", "name": "Samples", "labware_id": "plate", "deck_slot": 2, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0},
        {"id": "tips", "name": "Fresh tips", "role": "tips", "labware_id": "rack", "deck_slot": 3},
        {"id": "waste", "name": "Empty tip rack", "role": "waste", "labware_id": "rack", "deck_slot": 4, "available_tips": []}],
        "steps": [{"id": "transfer-1", "kind": "transfer", "source": "buffer", "destination": "samples", "source_anchor": "A1", "destination_anchor": "A1", "volume_ul": 20.0,
                   "source_paragraph_ids": ["p1"], "source_values": [{"field": "volume_ul", "value": 20.0, "unit": "uL", "paragraph_id": "p1"}]}]}
    setup = {"head_mode": {"subset_type": "all_barrels", "subset_config": "back_left"}, "tip_strategy": "fresh_each_step", "tip_rack_ids": ["tips"],
             "tip_disposal_id": "waste", "liquid_class": "water", "distance_from_bottom_mm": 1.0}
    sources = [{"id": "p1", "text": "Transfer 20 uL of buffer to each sample. Mix 5 uL 3 times. Wait 2 min. Repeat 2 times.", "page": 1}]
    return plan, setup, context, sources


def evaluate(plan=None, setup=None, context=None, sources=None):
    original = protocol_fixture()
    return validate_plan(plan or original[0], setup or original[1], context or original[2], sources=original[3] if sources is None else sources)


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_deterministic_compile_has_only_connected_allowlisted_nodes_and_sources():
    plan, setup, context, sources = protocol_fixture()
    first = compile_plan(plan, setup, context, sources=sources)
    assert first == compile_plan(plan, setup, context, sources=sources)
    assert [node["type"] for node in first["graph"]["nodes"]] == ["flow/Start", "tips/TipsOn", "liquid/Aspirate", "liquid/Dispense", "tips/TipsOff", "flow/End"]
    for index, node in enumerate(first["graph"]["nodes"]):
        assert node["type"] in ALLOWED_NODE_TYPES
        if index:
            assert node["inputs"][0]["link"] == index
        if index not in (0, 5):
            assert node["properties"]["_source_citation"]["paragraph_id"] == "p1"
    assert first["deck"]["4"][0]["tipbox_fill_state"] == "empty"
    summary = first["protocol"]["run_sheet"]
    assert summary["tips_required"] == 96
    assert summary["reagent_consumption_ul"] == {"buffer": 1920.0}
    assert summary["final_volumes_ul"]["buffer"]["H12"] == 80.0
    assert summary["final_volumes_ul"]["samples"]["H12"] == 20.0


def test_missing_data_becomes_questions_and_never_executable():
    plan, setup, context, sources = protocol_fixture()
    plan["steps"][0]["volume_ul"] = None
    plan["materials"][0]["labware_id"] = None
    report = validate_plan(plan, setup, context, sources=sources)
    assert not report["valid"]
    assert {q["path"] for q in report["questions"]} >= {"/steps/0/volume_ul", "/materials/0/labware_id"}
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, context, sources=sources)


@pytest.mark.parametrize("change,expected", [
    (lambda p: p["steps"][0].update(volume_ul=float("nan")), "schema"),
    (lambda p: p["steps"][0].update(volume_ul=float("inf")), "schema"),
    (lambda p: p["steps"][0].update(volume_ul=-5), "liquid_volume"),
    (lambda p: p["steps"][0].update(script="print('bad')"), "schema"),
    (lambda p: p["steps"][0].update(message="ignored scientific condition"), "unexpected_parameter"),
    (lambda p: p["steps"][0].update(source_anchor="H12"), "unreachable_wells"),
    (lambda p: p["materials"][1].update(deck_slot=1), "deck_collision"),
    (lambda p: p["materials"][0].update(initial_volume_ul=25), "insufficient_reagent"),
    (lambda p: p["materials"][1].update(initial_volume_ul=190), "destination_capacity"),
    (lambda p: p["materials"][0].update(initial_volume_ul=250), "initial_capacity"),
    (lambda p: p["materials"][0].update(well_volumes_ul={"A1": 11}), "insufficient_reagent"),
    (lambda p: p["materials"][0].update(well_volumes_ul={"A0": 100}), "well_volume"),
    (lambda p: p["materials"][2].update(available_tips=[]), "tip_inventory_exhausted"),
    (lambda p: p["materials"][3].update(available_tips=None), "disposal_not_empty"),
])
def test_fail_closed_parameter_and_inventory_checks(change, expected):
    plan, setup, context, sources = protocol_fixture()
    change(plan)
    assert expected in codes(validate_plan(plan, setup, context, sources=sources))


def test_grounding_verifies_number_unit_citation_and_scientist_override():
    plan, setup, context, sources = protocol_fixture()
    plan["steps"][0]["volume_ul"] = 5.0
    assert "ungrounded_parameter" in codes(evaluate(plan=plan))
    plan["decisions"] = [{"path": "/steps/0/volume_ul", "value": 5.0, "reason": "Scientist requests a lower transfer volume"}]
    assert evaluate(plan=plan)["valid"]
    plan["steps"][0]["source_values"][0]["value"] = 50
    assert "ungrounded_source_value" in codes(evaluate(plan=plan))
    plan["steps"][0]["source_values"][0]["paragraph_id"] = "fake"
    assert "unknown_evidence_source" in codes(evaluate(plan=plan))
    plan, setup, context, sources = protocol_fixture()
    sources[0]["text"] = "Transfer 20 min."
    assert "ungrounded_source_value" in codes(validate_plan(plan, setup, context, sources=sources))
    plan["steps"][0]["source_values"][0].update(value=.02, unit="mL")
    sources[0]["text"] = "Transfer 0.02 mL."
    assert validate_plan(plan, setup, context, sources=sources)["valid"]


def test_multichannel_dense_plate_footprint_and_unreachable_pitch():
    plan, setup, context, sources = protocol_fixture()
    context["labware"][0].update(rows=16, cols=24, wells=384, spacing_x_mm=4.5, spacing_y_mm=4.5)
    plan["steps"][0].update(source_anchor="B2", destination_anchor="B2")
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"], report
    assert report["summary"]["final_volumes_ul"]["samples"]["P24"] == 20
    assert report["summary"]["final_volumes_ul"]["samples"]["A1"] == 0
    context["labware"][0]["spacing_x_mm"] = 6
    assert "unreachable_wells" in codes(validate_plan(plan, setup, context, sources=sources))


def test_repeat_expansion_consumes_unique_tips_and_accumulates_well_volumes():
    plan, setup, context, sources = protocol_fixture()
    setup["head_mode"] = {"subset_type": "column", "subset_config": "back_left", "column_count": 1}
    plan["steps"][0]["repeat"] = 2
    plan["steps"][0]["source_values"].append({"field": "repeat", "value": 2, "unit": "count", "paragraph_id": "p1"})
    workflow = compile_plan(plan, setup, context, sources=sources)
    pickups = [n for n in workflow["graph"]["nodes"] if n["type"] == "tips/TipsOn"]
    assert [n["properties"]["tip_anchor_col"] for n in pickups] == [11, 10]
    assert workflow["protocol"]["run_sheet"]["tips_required"] == 16
    assert workflow["protocol"]["run_sheet"]["final_volumes_ul"]["samples"]["H1"] == 40
    assert workflow["protocol"]["run_sheet"]["final_volumes_ul"]["samples"]["H2"] == 0


def test_nested_repeat_expands_order_but_respects_operation_bound():
    plan, setup, context, sources = protocol_fixture()
    plan["steps"] = [{"id": "outer", "kind": "repeat", "repeat": 2, "source_paragraph_ids": ["p1"],
        "source_values": [{"field": "repeat", "value": 2, "unit": "count", "paragraph_id": "p1"}],
        "steps": [{"id": "pause", "kind": "wait", "duration_s": 120, "source_paragraph_ids": ["p1"],
                   "source_values": [{"field": "duration_s", "value": 2, "unit": "min", "paragraph_id": "p1"}]}]}]
    assert validate_plan(plan, setup, context, sources=sources)["summary"]["expanded_steps"] == 2
    context["max_operations"] = 3
    assert "operation_limit" in codes(validate_plan(plan, setup, context, sources=sources))


def test_mix_wait_manual_and_evolving_plate_locations_compile():
    plan, setup, context, sources = protocol_fixture()
    setup["tip_strategy"] = "reuse_all"
    setup["tip_reuse_reason"] = "Single buffer and same sample mapping throughout"
    plan["steps"].extend([
        {"id": "mix", "kind": "mix", "material": "samples", "anchor": "A1", "volume_ul": 5, "cycles": 3,
         "source_paragraph_ids": ["p1"], "source_values": [{"field": "volume_ul", "value": 5, "unit": "uL", "paragraph_id": "p1"}, {"field": "cycles", "value": 3, "unit": "count", "paragraph_id": "p1"}]},
        {"id": "wait", "kind": "wait", "duration_s": 120, "source_paragraph_ids": ["p1"], "source_values": [{"field": "duration_s", "value": 2, "unit": "min", "paragraph_id": "p1"}]},
        {"id": "manual", "kind": "manual", "message": "Centrifuge the sample plate and return it to slot 2.", "source_paragraph_ids": ["p1"]},
        {"id": "move", "kind": "move_plate", "material": "samples", "destination_slot": 5, "source_paragraph_ids": ["p1"]},
    ])
    workflow = compile_plan(plan, setup, context, sources=sources)
    types = [n["type"] for n in workflow["graph"]["nodes"]]
    assert types[-6:] == ["liquid/Mix", "tips/TipsOff", "system/Wait", "system/Manual", "plate/PickPlace", "flow/End"]
    assert workflow["protocol"]["run_sheet"]["final_deck"]["5"] == "samples"
    context["has_gripper"] = False
    assert "gripper_unavailable" in codes(validate_plan(plan, setup, context, sources=sources))
    context["has_gripper"] = True
    plan["steps"][-1]["destination_slot"] = 1
    assert "move_collision" in codes(validate_plan(plan, setup, context, sources=sources))


def test_tip_capacity_head_compatibility_catalog_and_setup_choices():
    plan, setup, context, sources = protocol_fixture()
    context["tip_definitions"][0]["capacity_ul"] = 10
    assert "tip_volume_exceeded" in codes(validate_plan(plan, setup, context, sources=sources))
    context["tip_definitions"][0]["compatible_heads"] = ["HT_384_D_70"]
    assert "tip_head_compatibility" in codes(validate_plan(plan, setup, context, sources=sources))
    setup["liquid_class"] = "invented"
    setup["distance_from_bottom_mm"] = 11
    setup["tip_strategy"] = "reuse_all"
    report = validate_plan(plan, setup, context, sources=sources)
    assert {"liquid_class", "pipetting_height", "tip_reuse_reason"} <= codes(report)


def test_imported_sbs_tip_box_is_usable_only_with_explicit_tip_link_and_head_match():
    plan, setup, context, sources = protocol_fixture()
    rack = context["labware"][1]
    rack["kind"] = "sbs_plate"
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"], report["issues"]
    assert compile_plan(plan, setup, context, sources=sources)["deck"]["3"][0]["tipbox_fill_state"] == "full"

    rack["tip_definition_id"] = ""
    rack["supported_tip_ids"] = []
    plan["materials"][2]["tip_definition_id"] = "tips-200"
    assert "tip_rack_compatibility" in codes(validate_plan(plan, setup, context, sources=sources))
    rack["tip_definition_id"] = "tips-200"
    context["tip_definitions"][0]["compatible_heads"] = []
    assert "tip_head_compatibility" in codes(validate_plan(plan, setup, context, sources=sources))


def test_model_tipbox_recommendation_needs_scientist_confirmation_of_exact_pair():
    plan, setup, context, sources = protocol_fixture()
    context["tipbox_choices"] = [{"labware_id": "rack", "tip_definition_id": "tips-200"}]
    plan["materials"][2]["tip_definition_id"] = "tips-200"
    plan["questions"] = [{"id": "catalog-tipbox:tips", "path": "/materials/2/labware_id",
                          "prompt": "Confirm this rack and tip pair."}]
    assert "tipbox_confirmation" in codes(validate_plan(plan, setup, context, sources=sources))
    plan["decisions"] = [
        {"path": "/materials/2/labware_id", "value": "rack", "reason": "Scientist checked rack", "actor": "scientist"},
        {"path": "/materials/2/tip_definition_id", "value": "tips-200", "reason": "Scientist checked tip", "actor": "scientist"},
    ]
    assert validate_plan(plan, setup, context, sources=sources)["valid"]
    plan["decisions"][1]["value"] = "old-tip"
    assert "tipbox_confirmation" in codes(validate_plan(plan, setup, context, sources=sources))
    plan["decisions"][1]["value"] = "tips-200"
    context["tipbox_choices"] = []
    assert "tipbox_confirmation" in codes(validate_plan(plan, setup, context, sources=sources))


def test_corrected_question_is_resolved_from_exact_pointer():
    plan, setup, context, sources = protocol_fixture()
    plan["questions"] = [{"id": "q1", "path": "/steps/0/volume_ul", "prompt": "What volume?"}]
    assert validate_plan(plan, setup, context, sources=sources)["valid"]
    plan["steps"][0]["volume_ul"] = None
    assert "unresolved_question" in codes(validate_plan(plan, setup, context, sources=sources))


def test_liquid_class_id_resolves_name_and_requires_tip_specific_calibration():
    plan, setup, context, sources = protocol_fixture()
    context["liquid_classes"] = [{"liquid_class_id": "class-id", "name": "Calibrated water", "head_type": "HT_96_D_200", "tip_capacity_ul": 200}]
    setup["liquid_class"] = "class-id"
    workflow = compile_plan(plan, setup, context, sources=sources)
    assert next(n for n in workflow["graph"]["nodes"] if n["type"] == "liquid/Aspirate")["properties"]["liquid_class"] == "Calibrated water"
    context["liquid_classes"][0]["tip_capacity_ul"] = 50
    assert "liquid_class_tip" in codes(validate_plan(plan, setup, context, sources=sources))


def test_tip_disposal_pitch_and_explicit_head_counts_are_never_normalized_silently():
    plan, setup, context, sources = protocol_fixture()
    disposal = deepcopy(context["labware"][1])
    disposal.update(id="dense-disposal", spacing_x_mm=4.5, spacing_y_mm=4.5, rows=16, cols=24)
    context["labware"].append(disposal)
    plan["materials"][-1]["labware_id"] = disposal["id"]
    setup["head_mode"]["row_count"] = 2
    report = validate_plan(plan, setup, context, sources=sources)
    assert {"disposal_pitch", "head_count"} <= codes(report)


def test_numeral_in_volume_does_not_ground_repeat_count():
    plan, setup, context, sources = protocol_fixture()
    setup["tip_strategy"] = "reuse_all"
    setup["tip_reuse_reason"] = "Repeat into same samples"
    plan["steps"][0]["repeat"] = 20
    plan["steps"][0]["source_values"].append({"field": "repeat", "value": 20, "unit": "count", "paragraph_id": "p1"})
    assert "ungrounded_source_value" in codes(validate_plan(plan, setup, context, sources=sources))


def test_persisted_engineering_benchmark_acceptance_cases():
    from pybravo.workflow.protocols.evaluate import evaluate_suite
    result = evaluate_suite()
    assert result['total'] == 18
    assert result['passed'] == result['total'], [case for case in result['cases'] if not case['passed']]
    assert result['hardware_qualified'] is False
    assert result['scientist_edit_seconds'] is None
