"""The reviewed protocol compiler never silently repairs scientific intent."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from pybravo.workflow.protocols.compiler import ALLOWED_NODE_TYPES, ProtocolCompilationError, compile_plan
from pybravo.workflow.protocols.validation import validate_plan


def protocol_fixture():
    plate = {"id": "plate", "name": "96 well plate", "kind": "sbs_plate", "base_class": "plate", "rows": 8, "cols": 12,
             "wells": 96, "spacing_x_mm": 9.0, "spacing_y_mm": 9.0, "well_volume_ul": 200.0, "well_depth_mm": 10.0}
    rack = {**plate, "id": "rack", "name": "96 tips", "kind": "tip_box", "base_class": "tip_box", "tip_definition_id": "tips-200", "supported_tip_ids": ["tips-200"]}
    context = {"head_type": "HT_96_D_200", "has_gripper": True, "labware": [plate, rack],
               "tip_definitions": [{"id": "tips-200", "capacity_ul": 200.0, "length_mm": 50.0, "compatible_heads": ["HT_96_D_200"]}],
               "liquid_classes": [{"id": "water", "name": "Water"}]}
    plan = {"name": "Reviewed buffer transfer", "materials": [
        {"id": "buffer", "name": "Buffer", "labware_id": "plate", "deck_slot": 1, "initial_volume_ul": 100.0, "dead_volume_ul": 10.0},
        {"id": "samples", "name": "Samples", "labware_id": "plate", "deck_slot": 2, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0},
        {"id": "tips", "name": "Fresh tips", "role": "tips", "labware_id": "rack", "deck_slot": 3,
         "available_tips": "full"},
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


def test_distribute_uses_one_aspiration_and_ordered_distinct_dispenses():
    plan, setup, context, sources = protocol_fixture()
    plan["materials"].append({"id": "samples-2", "name": "Second empty plate", "labware_id": "plate",
                              "deck_slot": 5, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0})
    plan["steps"] = [{"id": "distribute-1", "kind": "distribute", "source": "buffer", "source_anchor": "A1",
                      "dispenses": [{"destination": "samples", "destination_anchor": "A1", "volume_ul": 5.0},
                                    {"destination": "samples-2", "destination_anchor": "A1", "volume_ul": 5.0}],
                      "source_paragraph_ids": ["p1"],
                      "source_values": [{"field": "volume_ul", "value": 5.0, "unit": "uL", "paragraph_id": "p1"}]}]
    sources[0]["text"] = "Distribute 5 uL of buffer to each of two empty plates."
    workflow = compile_plan(plan, setup, context, sources=sources)
    liquid_nodes = [node for node in workflow["graph"]["nodes"] if node["type"].startswith("liquid/")]
    assert [node["type"] for node in liquid_nodes] == ["liquid/Aspirate", "liquid/Dispense", "liquid/Dispense"]
    assert [node["properties"]["volume"] for node in liquid_nodes] == [10.0, 5.0, 5.0]
    assert [node["properties"].get("empty_tips") for node in liquid_nodes] == [None, False, True]
    summary = workflow["protocol"]["run_sheet"]
    assert summary["tips_required"] == 96
    assert summary["reagent_consumption_ul"] == {"buffer": 960.0}
    assert summary["final_volumes_ul"]["buffer"]["H12"] == 90.0
    assert summary["final_volumes_ul"]["samples"]["H12"] == 5.0
    assert summary["final_volumes_ul"]["samples-2"]["H12"] == 5.0
    assert summary["run_steps"][0]["aspirate_volume_ul"] == 10.0

    context["tip_definitions"][0]["capacity_ul"] = 9.0
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert "distribute_calibration_fallback" in codes(report)
    paired = compile_plan(plan, setup, context, sources=sources)
    assert [node["type"] for node in paired["graph"]["nodes"] if node["type"].startswith("liquid/")] == [
        "liquid/Aspirate", "liquid/Dispense", "liquid/Aspirate", "liquid/Dispense"]
    assert paired["protocol"]["run_sheet"]["run_steps"][0]["strategy"] == "paired_fallback"
    context["tip_definitions"][0]["capacity_ul"] = 4.0
    assert "tip_volume_exceeded" in codes(validate_plan(plan, setup, context, sources=sources))
    plan["materials"][0]["initial_volume_ul"] = 19.0
    assert "insufficient_reagent" in codes(validate_plan(plan, setup, context, sources=sources))


def test_distribute_rejects_overlapping_partial_head_targets_before_paired_fallback():
    plan, setup, context, sources = protocol_fixture()
    setup["head_mode"] = {"subset_type": "rectangle", "subset_config": "back_left",
                          "row_count": 2, "column_count": 2}
    context["tip_definitions"][0]["capacity_ul"] = 9.0
    plan["steps"] = [{"id": "overlap", "kind": "distribute", "source": "buffer", "source_anchor": "A1",
                      "dispenses": [{"destination": "samples", "destination_anchor": "A1", "volume_ul": 5.0},
                                    {"destination": "samples", "destination_anchor": "A2", "volume_ul": 5.0}],
                      "source_paragraph_ids": ["p1"],
                      "source_values": [{"field": "volume_ul", "value": 5.0, "unit": "uL", "paragraph_id": "p1"}]}]
    sources[0]["text"] = "Distribute 5 uL of buffer into both A1 and A2 regions of an empty plate."
    report = validate_plan(plan, setup, context, sources=sources)
    assert "distribute_calibration_fallback" in codes(report)
    assert "overlapping_dispense" in codes(report)
    assert not report["valid"]
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, context, sources=sources)


def test_pinned_step_method_overrides_legacy_class_and_reports_adaptations(monkeypatch):
    from pybravo.workflow.protocols import methods
    from pybravo.workflow.protocols.store import digest

    plan, setup, context, sources = protocol_fixture()
    context["machine_id"] = "machine-1"
    liquid_class = {"id": "water", "name": "Water", "machine_id": "machine-1",
                    "head_type": "HT_96_D_200", "tip_id": "tips-200"}
    context["liquid_classes"] = [liquid_class]
    plan["materials"][0].update(reagent_id="reagent-buffer", reagent_family="buffer")
    method = {"method_id": "buffer-20", "version": "1.0.0", "status": "reviewed", "title": "Reviewed buffer transfer",
              "applicability": {"machine_ids": ["machine-1"], "head_types": ["HT_96_D_200"],
                                "tip_ids": ["tips-200"], "tipbox_ids": ["rack"],
                                "source_labware_ids": ["plate"], "destination_labware_ids": ["plate"],
                                "reagent_families": ["buffer"], "operations": ["transfer"],
                                "min_volume_ul": 1.0, "max_volume_ul": 20.0},
              "aspirate": {"liquid_class_ref": {"id": "water", "name": "Water", "digest": digest(liquid_class)},
                           "distance_from_bottom_mm": 2.0, "pre_air_ul": 0.0, "post_air_ul": 0.0,
                           "dynamic_tip_extension": 0.0, "tip_touch": False},
              "dispense": {"liquid_class_ref": {"id": "water", "name": "Water", "digest": digest(liquid_class)},
                           "distance_from_bottom_mm": 1.5, "blowout_ul": 0.0,
                           "dynamic_tip_retraction": 0.0, "tip_touch": False},
              "tip_policy": "fresh_per_transfer", "evidence": [{"source_type": "synthetic_example"}]}
    record = {**method, "revision": "a" * 64, "execution_ready": True, "missing_fields": []}
    monkeypatch.setattr(methods, "get_method", lambda _context, identity, revision=None:
                        record if identity == record["method_id"] and revision in (None, record["revision"]) else None)
    plan["steps"][0]["method_ref"] = {"method_id": record["method_id"], "revision": record["revision"]}
    setup["liquid_class"] = None
    setup["distance_from_bottom_mm"] = None
    workflow = compile_plan(plan, setup, context, sources=sources)
    nodes = [node for node in workflow["graph"]["nodes"] if node["type"].startswith("liquid/")]
    assert [node["properties"]["distance_from_bottom"] for node in nodes] == [2.0, 1.5]
    assert all(node["properties"]["_method_ref"]["revision"] == record["revision"] for node in nodes)
    summary = workflow["protocol"]["run_sheet"]
    assert summary["methods_used"][0]["method_id"] == "buffer-20"
    assert summary["run_steps"][0]["method_ref"]["digest"] == record["revision"]

    plan["steps"][0].update(reagent_id="step-reagent", reagent_family="buffer")
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert report["summary"]["run_steps"][0]["reagent_id"] == "step-reagent"
    assert not any(issue["code"] == "method_mismatch" for issue in report["issues"])

    plan["materials"][0]["reagent_family"] = "viscous_buffer"
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert not any(issue["code"] == "method_mismatch" for issue in report["issues"])
    plan["steps"][0]["reagent_family"] = "viscous_buffer"
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert any(issue["code"] == "method_mismatch" and issue["severity"] == "warning"
               for issue in report["issues"])
    record["applicability"]["max_volume_ul"] = 8.0
    liquid_class["equation"] = {"control_points": [
        {"desired_ul": 0.0, "commanded_ul": 0.0},
        {"desired_ul": 25.0, "commanded_ul": 25.0},
    ]}
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert any(difference.get("phase") == "aspirate" and difference["actual"] == 20.0
               for issue in report["issues"] if issue["code"] == "method_mismatch"
               for difference in issue["differences"])
    liquid_class["equation"]["control_points"][-1] = {"desired_ul": 8.0, "commanded_ul": 8.0}
    report = validate_plan(plan, setup, context, sources=sources)
    assert not report["valid"]
    assert "method_calibration_limit" in codes(report)
    record["applicability"]["max_volume_ul"] = 20.0
    liquid_class["equation"]["control_points"][-1] = {"desired_ul": 20.0, "commanded_ul": 205.0}
    assert "tip_volume_exceeded" in codes(validate_plan(plan, setup, context, sources=sources))
    liquid_class["equation"]["control_points"][-1] = {"desired_ul": 20.0, "commanded_ul": 20.0}
    record["aspirate"]["pre_air_ul"] = 190.0
    assert "method_calibration_limit" in codes(validate_plan(plan, setup, context, sources=sources))
    liquid_class.pop("equation")
    assert "tip_volume_exceeded" in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["pre_air_ul"] = 0.0
    record["dispense"]["blowout_ul"] = 1.0
    assert "method_blowout_unavailable" in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["pre_air_ul"] = 2.0
    assert "method_blowout_unavailable" not in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["pre_air_ul"] = 0.0
    original_step = plan["steps"][0]
    original_text = sources[0]["text"]
    record["dispense"]["distance_from_bottom_mm"] = 2.0
    plan["steps"][0] = {"id": "mix-1", "kind": "mix", "material": "buffer", "anchor": "A1",
                        "volume_ul": 20.0, "cycles": 3,
                        "method_ref": {"method_id": record["method_id"], "revision": record["revision"]},
                        "source_paragraph_ids": ["p1"],
                        "source_values": [{"field": "volume_ul", "value": 20.0, "unit": "uL", "paragraph_id": "p1"},
                                          {"field": "cycles", "value": 3.0, "unit": "count", "paragraph_id": "p1"}]}
    sources[0]["text"] = "Mix 20 uL of buffer 3 times."
    assert "method_blowout_unavailable" in codes(validate_plan(plan, setup, context, sources=sources))
    record["dispense"]["blowout_ul"] = 0.0
    record["aspirate"]["dynamic_tip_extension"] = 1.0
    assert "method_mix_unsupported" in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["dynamic_tip_extension"] = 0.0
    plan["steps"][0] = original_step
    sources[0]["text"] = original_text
    record["dispense"]["distance_from_bottom_mm"] = 1.5
    record["dispense"]["blowout_ul"] = 0.0
    plan["steps"][0]["method_ref"]["revision"] = "stale-revision"
    assert "method_reference" in codes(validate_plan(plan, setup, context, sources=sources))
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, context, sources=sources)


def test_distribute_with_pinned_method_audits_one_aspiration_and_two_dispenses(monkeypatch):
    from pybravo.workflow.protocols import methods

    plan, setup, context, sources = protocol_fixture()
    context["machine_id"] = "machine-1"
    context["liquid_classes"] = [{"id": "water", "name": "Water", "machine_id": "machine-1",
                                  "head_type": "HT_96_D_200", "tip_id": "tips-200"}]
    plan["materials"][0]["reagent_family"] = "buffer"
    plan["materials"].append({"id": "samples-2", "name": "Second empty plate", "labware_id": "plate",
                              "deck_slot": 5, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0})
    plan["steps"] = [{"id": "distribute-1", "kind": "distribute", "source": "buffer", "source_anchor": "A1",
                      "dispenses": [{"destination": "samples", "destination_anchor": "A1", "volume_ul": 5.0},
                                    {"destination": "samples-2", "destination_anchor": "A1", "volume_ul": 5.0}],
                      "method_ref": {"method_id": "buffer-multi", "revision": "b" * 64},
                      "source_paragraph_ids": ["p1"],
                      "source_values": [{"field": "volume_ul", "value": 5.0, "unit": "uL", "paragraph_id": "p1"}]}]
    sources[0]["text"] = "Distribute 5 uL of buffer to each of two empty plates."
    setup.update(tip_strategy="fresh_each_source", tip_reuse_reason="One source and two initially empty destinations",
                 liquid_class=None, distance_from_bottom_mm=None)
    record = {"method_id": "buffer-multi", "version": "1.0.0", "revision": "b" * 64,
              "status": "reviewed", "execution_ready": True, "missing_fields": [],
              "applicability": {"machine_ids": ["machine-1"], "head_types": ["HT_96_D_200"],
                                "tip_ids": ["tips-200"], "tipbox_ids": ["rack"],
                                "source_labware_ids": ["plate"], "destination_labware_ids": ["plate"],
                                "reagent_families": ["buffer"], "operations": ["distribute"],
                                "min_volume_ul": 1.0, "max_volume_ul": 10.0},
              "aspirate": {"liquid_class_ref": {"id": "water"}, "distance_from_bottom_mm": 2.0,
                           "pre_air_ul": 0.0, "post_air_ul": 0.0, "dynamic_tip_extension": 0.0, "tip_touch": False},
              "dispense": {"liquid_class_ref": {"id": "water"}, "distance_from_bottom_mm": 1.5,
                           "blowout_ul": 0.0, "dynamic_tip_retraction": 0.0, "tip_touch": False},
              "tip_policy": "reuse_within_source"}
    monkeypatch.setattr(methods, "get_method", lambda _context, identity, revision=None:
                        record if identity == record["method_id"] and revision == record["revision"] else None)
    workflow = compile_plan(plan, setup, context, sources=sources)
    liquid_nodes = [node for node in workflow["graph"]["nodes"] if node["type"].startswith("liquid/")]
    assert [node["properties"]["volume"] for node in liquid_nodes] == [10.0, 5.0, 5.0]
    assert [node["properties"]["distance_from_bottom"] for node in liquid_nodes] == [2.0, 1.5, 1.5]
    assert all(node["properties"]["_method_ref"]["method_id"] == "buffer-multi" for node in liquid_nodes)
    assert not any(issue["code"] == "method_mismatch" for issue in validate_plan(plan, setup, context, sources=sources)["issues"])
    record["applicability"]["min_volume_ul"] = 8.0
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert sum(difference.get("phase") == "dispense" for issue in report["issues"]
               if issue["code"] == "method_mismatch" for difference in issue["differences"]) == 2
    record["applicability"]["min_volume_ul"] = 1.0
    record["dispense"]["blowout_ul"] = 1.0
    assert "method_distribute_unsupported" in codes(validate_plan(plan, setup, context, sources=sources))
    original_targets = plan["steps"][0]["dispenses"]
    plan["steps"][0]["dispenses"] = original_targets[:1]
    assert "method_distribute_unsupported" in codes(validate_plan(plan, setup, context, sources=sources))
    plan["steps"][0]["dispenses"] = original_targets
    record["dispense"]["blowout_ul"] = 0.0
    record["aspirate"]["pre_air_ul"] = 1.0
    assert "method_distribute_air_gap" in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["pre_air_ul"] = 0.0
    record["aspirate"]["post_air_ul"] = 1.0
    assert "method_distribute_air_gap" in codes(validate_plan(plan, setup, context, sources=sources))
    record["aspirate"]["post_air_ul"] = 0.0
    context["tip_definitions"][0]["capacity_ul"] = 9.0
    context["liquid_classes"].append({"id": "water-dispense", "name": "Water dispense",
                                      "machine_id": "machine-1", "head_type": "HT_96_D_200",
                                      "tip_id": "tips-200", "equation": {"control_points": [
                                          {"desired_ul": 0.0, "commanded_ul": 0.0},
                                          {"desired_ul": 10.0, "commanded_ul": 10.04}]}})
    record["dispense"]["liquid_class_ref"] = {"id": "water-dispense"}
    assert "distribute_calibration" in codes(validate_plan(plan, setup, context, sources=sources))


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
    (lambda p: p["materials"][2].update(available_tips=None), "tip_inventory_unconfirmed"),
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


def test_provisional_labware_and_wrong_head_rack_cannot_compile():
    plan, setup, context, sources = protocol_fixture()
    rack = context["labware"][1]
    rack["provisional"] = True
    assert "provisional_labware" in codes(validate_plan(plan, setup, context, sources=sources))
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, context, sources=sources)
    rack["provisional"] = False
    rack["compatible_head_types"] = ["HT_384_D_70"]
    assert "tip_rack_head_compatibility" in codes(validate_plan(plan, setup, context, sources=sources))


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


def _four_source_quadrant_fixture():
    """Reviewed synthetic run using the checked-in physical labware geometry."""
    snapshot = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/labware_catalog.snapshot.yaml").read_text())
    by_name = {item["name"]: item for item in snapshot["labware"]}
    source_plate = by_name["384 Labcyte PP0200 PP sq flt"]
    destination_plate = by_name["1536 Labcyte LP-0400 LDV"]
    rack = by_name["384 V11 ST10 Tip Box 10734.102"]
    assert (destination_plate["rows"], destination_plate["cols"], destination_plate["spacing_x_mm"]) == (32, 48, 2.25)
    assert (source_plate["rows"], source_plate["cols"], source_plate["spacing_x_mm"]) == (16, 24, 4.5)
    assert (rack["rows"], rack["cols"], rack["spacing_x_mm"]) == (16, 24, 4.5)
    materials = [
        {"id": f"source-{n}", "name": f"Source {n}", "labware_id": source_plate["id"],
         "deck_slot": 9, "stack_order": n - 1, "initial_volume_ul": 15.0, "dead_volume_ul": 2.0}
        for n in (2, 4, 1, 3)  # Deliberately not bottom-to-top model order.
    ]
    materials += [
        {"id": f"rack-{n}", "name": f"ST10 rack {n}", "role": "tips", "labware_id": rack["id"],
         "deck_slot": n, "tip_definition_id": "st_10ul", "available_tips": "full"}
        for n in range(1, 5)
    ]
    materials += [
        {"id": "dest-a", "name": "1536 destination A", "labware_id": destination_plate["id"],
         "deck_slot": 5, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0},
        {"id": "dest-b", "name": "1536 destination B", "labware_id": destination_plate["id"],
         "deck_slot": 8, "initial_volume_ul": 0.0, "dead_volume_ul": 0.0},
    ]
    steps = []
    for index, (source_id, quadrant) in enumerate(zip(
        ("source-4", "source-3", "source-2", "source-1"), ("A1", "A2", "B1", "B2")
    )):
        steps.append({"id": f"stage-{source_id}", "kind": "destack_plate" if index < 3 else "move_plate",
                      "material": source_id, "destination_slot": 6, "source_paragraph_ids": ["p1"]})
        for destination_id in ("dest-a", "dest-b"):
            steps.append({"id": f"transfer-{source_id}-{destination_id}", "kind": "transfer",
                          "source": source_id, "destination": destination_id,
                          "source_anchor": "A1", "destination_anchor": quadrant, "volume_ul": 5.0,
                          "source_paragraph_ids": ["p1"],
                          "source_values": [{"field": "volume_ul", "value": 5.0, "unit": "uL", "paragraph_id": "p1"}]})
        steps.append({"id": f"file-{source_id}", "kind": "move_plate" if index == 0 else "stack_plate",
                      "material": source_id, "destination_slot": 7, "source_paragraph_ids": ["p1"]})
    plan = {"name": "Four sources into two 1536 plates", "materials": materials, "steps": steps}
    setup = {"head_mode": {"subset_type": "all_barrels", "subset_config": "back_left"},
             "tip_strategy": "fresh_each_source", "tip_rack_ids": [f"rack-{n}" for n in range(1, 5)],
             "tip_disposal_id": "return_to_source_rack", "liquid_class": "reviewed-st10-water",
             "tip_reuse_reason": "Both destinations start empty; each source has its own tip set and tips are changed before the next source.",
             "distance_from_bottom_mm": 1.0}
    context = {"head_type": "HT_384_D_70", "has_gripper": True,
               "labware": [source_plate, destination_plate, rack],
               "tip_definitions": [{"id": "st_10ul", "kind": "tip", "capacity_ul": 10.0,
                                    "length_mm": 19.9, "compatible_heads": ["HT_384_D_70"]}],
               "liquid_classes": [{"id": "reviewed-st10-water", "name": "Reviewed ST10 water",
                                   "tip_id": "st_10ul", "head_type": "HT_384_D_70"}]}
    sources = [{"id": "p1", "text": "I have 4 384 well plates stacked in one deck position. Transfer 5 uL "
                "from each plate into its quadrant of two empty 1536 plates with no cross contamination."}]
    return plan, setup, context, sources


def test_four_stacked_384_sources_fill_both_1536_plates_with_four_isolated_st10_racks():
    plan, setup, context, sources = _four_source_quadrant_fixture()
    workflow = compile_plan(plan, setup, context, sources=sources)
    summary = workflow["protocol"]["run_sheet"]
    assert [item["name"] for item in workflow["deck"]["9"]] == [f"Source {n}" for n in (1, 2, 3, 4)]
    assert [item["tip_definition_id"] for slot in range(1, 5) for item in workflow["deck"][str(slot)]] == ["st_10ul"] * 4
    assert summary["tips_required"] == 4 * 384
    assert summary["final_deck_stacks"]["7"] == [f"source-{n}" for n in (4, 3, 2, 1)]
    assert "9" not in summary["final_deck_stacks"]
    assert [s["tip_rack_id"] for s in summary["run_steps"] if s["kind"] == "transfer"] == [
        rack_id for rack_id in ("rack-1", "rack-2", "rack-3", "rack-4") for _ in range(2)]
    for destination_id in ("dest-a", "dest-b"):
        transfers = [step for step in summary["run_steps"] if step["kind"] == "transfer" and step["destination"] == destination_id]
        footprints = [set(step["destination_wells"]) for step in transfers]
        assert len(transfers) == 4
        assert all(len(footprint) == 384 for footprint in footprints)
        assert len(set.union(*footprints)) == 1536
        assert all(volume == 5.0 for volume in summary["final_volumes_ul"][destination_id].values())
    nodes = workflow["graph"]["nodes"]
    assert len([node for node in nodes if node["type"] == "tips/TipsOn"]) == 4
    returns = [node["properties"]["location"] for node in nodes if node["type"] == "tips/TipsOff"]
    assert returns == [1, 2, 3, 4]
    assert len([node for node in nodes if node["type"] == "plate/Destack"]) == 3
    assert len([node for node in nodes if node["type"] == "plate/Stack"]) == 3
    checkpoints = [node["properties"]["message"] for node in nodes if node["type"] == "system/Manual"]
    assert len(checkpoints) == 2
    assert "Before every run" in checkpoints[0]
    assert "returned spent tips" in checkpoints[1]
    assert summary["spent_tip_rack_ids"] == [f"rack-{n}" for n in range(1, 5)]


def test_nonlinear_st10_distribute_uses_paired_fallback_without_extra_tipboxes():
    plan, setup, context, sources = _four_source_quadrant_fixture()
    context["liquid_classes"][0]["equation"] = {"control_points": [
        {"desired_ul": 0.0, "commanded_ul": 0.0},
        {"desired_ul": 5.0, "commanded_ul": 5.5},
        {"desired_ul": 10.0, "commanded_ul": 10.0},
    ]}
    revised = []
    for index, step in enumerate(plan["steps"]):
        if step["kind"] == "transfer" and step["destination"] == "dest-a":
            other = plan["steps"][index + 1]
            revised.append({"id": f"distribute-{step['source']}", "kind": "distribute",
                            "source": step["source"], "source_anchor": "A1",
                            "dispenses": [
                                {"destination": transfer["destination"],
                                 "destination_anchor": transfer["destination_anchor"], "volume_ul": 5.0}
                                for transfer in (step, other)],
                            "source_paragraph_ids": ["p1"], "source_values": step["source_values"]})
        elif step["kind"] != "transfer":
            revised.append(step)
    plan["steps"] = revised
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"], report["issues"]
    assert sum(issue["code"] == "distribute_calibration_fallback" for issue in report["issues"]) == 4
    workflow = compile_plan(plan, setup, context, sources=sources)
    liquid_nodes = [node for node in workflow["graph"]["nodes"] if node["type"].startswith("liquid/")]
    assert [node["type"] for node in liquid_nodes[:4]] == [
        "liquid/Aspirate", "liquid/Dispense", "liquid/Aspirate", "liquid/Dispense"]
    assert [node["properties"]["volume"] for node in liquid_nodes[:4]] == [5.0] * 4
    summary = workflow["protocol"]["run_sheet"]
    assert summary["tips_required"] == 4 * 384
    assert all(step["strategy"] == "paired_fallback" and step["aspiration_sequence_ul"] == [5.0, 5.0]
               for step in summary["run_steps"] if step["kind"] == "distribute")
    assert all(volume == 5.0 for destination in ("dest-a", "dest-b")
               for volume in summary["final_volumes_ul"][destination].values())
    context["liquid_classes"][0]["equation"]["control_points"][-1]["commanded_ul"] = 11.0
    report = validate_plan(plan, setup, context, sources=sources)
    assert report["valid"]
    assert any("commanded_tip_capacity" in issue["reasons"] for issue in report["issues"]
               if issue["code"] == "distribute_calibration_fallback")
    assert compile_plan(plan, setup, context, sources=sources)["protocol"]["run_sheet"]["tips_required"] == 4 * 384
    next(material for material in plan["materials"] if material["id"] == "dest-a")["initial_volume_ul"] = 1.0
    assert "distribute_calibration" in codes(validate_plan(plan, setup, context, sources=sources))
    with pytest.raises(ProtocolCompilationError):
        compile_plan(plan, setup, context, sources=sources)


def test_stacked_sources_require_explicit_top_first_moves_and_unshared_racks():
    plan, setup, context, sources = _four_source_quadrant_fixture()
    plan["steps"][0]["material"] = "source-2"
    assert "buried_plate" in codes(validate_plan(plan, setup, context, sources=sources))
    plan, setup, context, sources = _four_source_quadrant_fixture()
    plan["materials"][0]["stack_order"] = 3
    assert "stack_order" in codes(validate_plan(plan, setup, context, sources=sources))
    plan, setup, context, sources = _four_source_quadrant_fixture()
    setup["tip_rack_ids"] = ["rack-1"]
    assert "tip_inventory_exhausted" in codes(validate_plan(plan, setup, context, sources=sources))
    plan, setup, context, sources = _four_source_quadrant_fixture()
    setup["tip_strategy"] = "reuse_all"
    assert "tip_disposal" in codes(validate_plan(plan, setup, context, sources=sources))
    plan, setup, context, sources = _four_source_quadrant_fixture()
    setup["tip_reuse_reason"] = None
    assert "tip_reuse_reason" in codes(validate_plan(plan, setup, context, sources=sources))


def test_st70_is_rejected_for_1536_destinations_even_when_the_rack_supports_it():
    plan, setup, context, sources = _four_source_quadrant_fixture()
    context["tip_definitions"] = [{"id": "st_70ul", "kind": "tip", "capacity_ul": 70.0,
                                   "length_mm": 30.0, "compatible_heads": ["HT_384_D_70"]}]
    context["labware"][2]["supported_tip_ids"].append("st_70ul")
    context["liquid_classes"][0]["tip_id"] = "st_70ul"
    for material in plan["materials"]:
        if material["id"].startswith("rack-"):
            material["tip_definition_id"] = "st_70ul"
    assert "tip_plate_compatibility" in codes(validate_plan(plan, setup, context, sources=sources))
