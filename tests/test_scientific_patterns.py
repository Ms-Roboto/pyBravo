"""Scientific review checks use executable Designer actions, not draft prose."""

from __future__ import annotations

from pybravo.workflow.drafter.scientific_patterns import (
    StageRequirement,
    audit_scientific_patterns,
    numbered_stage_requirements,
)


def _workflow(*nodes: dict) -> dict:
    links = [[index, nodes[index - 1]["id"], 0, nodes[index]["id"], 0, -1]
             for index in range(1, len(nodes))]
    return {"graph": {"nodes": list(nodes), "links": links}}


def _node(node_id: int, kind: str, **props: object) -> dict:
    return {"id": node_id, "type": kind, "properties": props}


def _codes(workflow: dict, **kwargs: object) -> set[str]:
    return {issue["code"] for issue in audit_scientific_patterns(workflow, **kwargs)}


def test_ignored_multiwell_lists_and_active_head_footprint_are_reported():
    workflow = _workflow(
        _node(1, "flow/Start"),
        _node(2, "tips/TipsOn", head_mode={"subset_type": "all_barrels"}),
        _node(3, "liquid/Aspirate", location=1, volume=1, anchor="A1",
              wells=["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]),
        _node(4, "liquid/Dispense", location=2, volume=1, anchor="A1",
              wells=["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"]),
        _node(5, "tips/TipsOff"),
        _node(6, "flow/End"),
    )
    issues = audit_scientific_patterns(
        workflow, source_instruction="Transform 8 plasmids separately.", head_type="HT_384_D_70",
    )
    assert sum(issue["code"] == "NON_EXECUTABLE_WELL_LIST" for issue in issues) == 2
    assert sum(issue["code"] == "HEAD_FOOTPRINT_MISMATCH" for issue in issues) == 2
    assert "SAMPLE_MAPPING_UNPROVEN" in {issue["code"] for issue in issues}
    assert workflow["graph"]["nodes"][2]["properties"]["wells"][0] == "A1"


def test_ignored_well_range_and_head_larger_than_plate_are_reported():
    workflow = _workflow(
        _node(1, "flow/Start"),
        _node(2, "tips/TipsOn", head_mode={"subset_type": "all_barrels"}),
        _node(3, "liquid/Aspirate", location=1, volume=2, wells="A1:G1"),
        _node(4, "liquid/Dispense", location=2, volume=2, wells="A1:D1"),
        _node(5, "tips/TipsOff"), _node(6, "flow/End"),
    )
    workflow["deck"] = {
        "1": [{"wells": 96, "labware_id": "plate-a"}],
        "2": [{"wells": 96, "labware_id": "plate-b"}],
    }
    issues = audit_scientific_patterns(workflow, head_type="HT_384_D_70")
    assert sum(issue["code"] == "NON_EXECUTABLE_WELL_LIST" for issue in issues) == 2
    assert sum(issue["code"] == "HEAD_FOOTPRINT_MISMATCH" for issue in issues) == 2
    assert sum(issue["code"] == "HEAD_EXCEEDS_LABWARE" for issue in issues) == 2


def test_populated_sample_range_implies_coverage_even_without_numeral():
    workflow = _workflow(
        _node(1, "flow/Start"), _node(2, "tips/TipsOn"),
        _node(3, "liquid/Aspirate", location=1, anchor="A1", volume=1),
        _node(4, "liquid/Dispense", location=2, anchor="A1", volume=1),
        _node(5, "tips/TipsOff"), _node(6, "flow/End"),
    )
    source = "- sample_plate wells A1:H12: PCR products, each 50 µL."
    assert "SAMPLE_MAPPING_UNPROVEN" in _codes(workflow, source_instruction=source)


def test_explicit_eluate_recovery_requires_an_action_beyond_deck_setup():
    workflow = _workflow(
        _node(1, "flow/Start"),
        {"id": 2, "type": "system/Manual", "title": "Setup deck",
         "properties": {"message": "Place the elution plate at location 2."}},
        _node(3, "flow/End"),
    )
    source = "Recover each eluate into its own well of the elution plate."
    assert "ELUTION_STAGE_UNACCOUNTED" in _codes(workflow, source_instruction=source)
    workflow["graph"]["nodes"].insert(2, {
        "id": 4, "type": "system/Manual", "title": "Recover eluate",
        "properties": {"message": "Move cleaned eluate to the collection plate."},
    })
    workflow["graph"]["links"] = [
        [1, 1, 0, 2, 0, -1], [2, 2, 0, 4, 0, -1], [3, 4, 0, 3, 0, -1],
    ]
    assert "ELUTION_STAGE_UNACCOUNTED" not in _codes(workflow, source_instruction=source)


def test_tip_balance_and_source_isolation_follow_flow_links_not_node_array():
    workflow = _workflow(
        _node(1, "flow/Start"), _node(2, "tips/TipsOn"),
        _node(3, "liquid/Aspirate", location=1, anchor="A1", volume=10),
        _node(4, "liquid/Dispense", location=3, anchor="A1", volume=10),
        _node(5, "liquid/Aspirate", location=2, anchor="A1", volume=10),
        _node(6, "liquid/Dispense", location=3, anchor="A1", volume=20),
        _node(7, "tips/TipsOff"), _node(8, "flow/End"),
    )
    workflow["graph"]["nodes"].reverse()
    codes = _codes(workflow)
    assert "CROSS_SOURCE_TIP_REVIEW" in codes
    assert "DISPENSE_EXCEEDS_ASPIRATED" in codes
    assert "LIQUID_WITHOUT_TIPS" not in codes


def test_explicit_two_well_transfer_is_balanced_and_mapped():
    workflow = _workflow(
        _node(1, "flow/Start"), _node(2, "tips/TipsOn"),
        _node(3, "liquid/Aspirate", location=1, anchor="A1", volume=100),
        _node(4, "liquid/Dispense", location=2, anchor="A1", volume=100),
        _node(5, "liquid/Aspirate", location=1, anchor="A1", volume=100),
        _node(6, "liquid/Dispense", location=2, anchor="B1", volume=100),
        _node(7, "tips/TipsOff"), _node(8, "flow/End"),
    )
    assert _codes(workflow, source_instruction="Transfer to two wells.") == set()


def test_iterated_sample_transfer_needs_tip_cycle_inside_loop():
    start = _node(1, "flow/Start")
    loop = _node(2, "flow/Loop", count=3)
    on = _node(3, "tips/TipsOn")
    asp = _node(4, "liquid/Aspirate", location=1, anchor="iter:A1,B1,C1", volume=1)
    dsp = _node(5, "liquid/Dispense", location=2, anchor="iter:A1,B1,C1", volume=1)
    off = _node(6, "tips/TipsOff")
    end = _node(7, "flow/End")
    workflow = {"graph": {"nodes": [start, on, loop, asp, dsp, off, end], "links": [
        [1, 1, 0, 3, 0, -1], [2, 3, 0, 2, 0, -1],
        [3, 2, 0, 4, 0, -1], [4, 4, 0, 5, 0, -1],
        [5, 2, 1, 6, 0, -1], [6, 6, 0, 7, 0, -1],
    ]}}
    codes = _codes(workflow, source_instruction="Process 3 samples in paired wells.")
    assert "SAMPLE_MAPPING_UNPROVEN" not in codes
    assert "REUSED_SAMPLE_TIP_IN_LOOP" in codes
    workflow["graph"]["nodes"].append(_node(8, "tips/TipsOn"))
    workflow["graph"]["nodes"].append(_node(9, "tips/TipsOff"))
    workflow["graph"]["links"] = [
        [1, 1, 0, 2, 0, -1], [2, 2, 0, 8, 0, -1],
        [3, 8, 0, 4, 0, -1], [4, 4, 0, 5, 0, -1], [5, 5, 0, 9, 0, -1],
        [6, 2, 1, 7, 0, -1],
    ]
    assert "REUSED_SAMPLE_TIP_IN_LOOP" not in _codes(
        workflow, source_instruction="Process 3 samples in paired wells.")


def test_module_handoff_and_direct_module_claim_are_distinguished():
    workflow = _workflow(
        _node(1, "flow/Start"),
        {"id": 2, "type": "system/Wait", "title": "Engage magnet", "properties": {"duration_s": 300}},
        _node(3, "flow/End"),
    )
    codes = _codes(workflow, source_instruction="# Magnetic cleanup\nEngage the magnetic module for five minutes.\n## Tools and constraints\nUse a thermocycler only if needed.")
    assert "MODULE_HANDOFF_MISSING" in codes
    assert "UNSUPPORTED_MODULE_ACTION" in codes
    workflow["graph"]["nodes"][1] = {
        "id": 2, "type": "system/Manual", "title": "Magnet handoff",
        "properties": {"message": "Operator engages the magnet for five minutes."},
    }
    assert "MODULE_HANDOFF_MISSING" not in _codes(
        workflow, source_instruction="Engage the magnetic module for five minutes.")


def test_numbered_source_stages_require_mixing_and_ordered_manual_handoff():
    source = """## The protocol to implement
1. Transfer 40 µL beads to the sample plate. Mix 10 times.
2. Engage magnetic module for five minutes.
3. Transfer 90 µL supernatant to waste.
## Tools and constraints
4. These are examples, not steps.
"""
    requirements = numbered_stage_requirements(source)
    assert [stage.kind for stage in requirements] == ["transfer", "mix", "manual", "transfer"]
    assert requirements[1].cycles == 10
    workflow = _workflow(
        _node(1, "flow/Start"), _node(2, "tips/TipsOn"),
        _node(3, "liquid/Aspirate", location=1, volume=40),
        _node(4, "liquid/Dispense", location=2, volume=40),
        {"id": 5, "type": "system/Manual", "title": "Engage magnet", "properties": {"message": "Engage magnet."}},
        _node(6, "liquid/Aspirate", location=2, volume=90),
        _node(7, "liquid/Dispense", location=3, volume=90),
        _node(8, "tips/TipsOff"), _node(9, "flow/End"),
    )
    issues = audit_scientific_patterns(workflow, source_instruction=source)
    assert sum(issue["code"] == "SOURCE_STAGE_UNACCOUNTED" for issue in issues) == 1
    workflow["graph"]["nodes"][4] = _node(5, "liquid/Mix", location=2, volume=10, cycles=10)
    workflow["graph"]["nodes"].insert(5, {
        "id": 10, "type": "system/Manual", "title": "Engage magnet",
        "properties": {"message": "Engage the magnet."},
    })
    workflow["graph"]["links"] = [
        [1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1], [3, 3, 0, 4, 0, -1],
        [4, 4, 0, 5, 0, -1], [5, 5, 0, 10, 0, -1], [6, 10, 0, 6, 0, -1],
        [7, 6, 0, 7, 0, -1], [8, 7, 0, 8, 0, -1], [9, 8, 0, 9, 0, -1],
    ]
    assert "SOURCE_STAGE_UNACCOUNTED" not in _codes(workflow, source_instruction=source)


def test_mix_cycle_count_must_match_numbered_source_stage():
    source = "## The protocol to implement\n1. Mix 10 times after dispensing.\n"
    workflow = _workflow(
        _node(1, "flow/Start"), _node(2, "tips/TipsOn"),
        _node(3, "liquid/Mix", location=2, volume=20, cycles=3),
        _node(4, "tips/TipsOff"), _node(5, "flow/End"),
    )
    assert "SOURCE_STAGE_UNACCOUNTED" in _codes(workflow, source_instruction=source)
    workflow["graph"]["nodes"][2]["properties"]["cycles"] = 10
    assert "SOURCE_STAGE_UNACCOUNTED" not in _codes(workflow, source_instruction=source)


def test_reviewed_stage_contract_detects_out_of_order_module():
    workflow = _workflow(
        _node(1, "flow/Start"),
        {"id": 2, "type": "system/Manual", "title": "Thermocycler", "properties": {"message": "Thermocycle plate."}},
        _node(3, "tips/TipsOn"),
        _node(4, "liquid/Aspirate", location=1, volume=5),
        _node(5, "liquid/Dispense", location=2, volume=5),
        _node(6, "tips/TipsOff"), _node(7, "flow/End"),
    )
    stages = [StageRequirement("transfer", "Assemble reaction", volume_ul=5),
              StageRequirement("manual", "Thermocycle reaction", marker="thermocycl")]
    assert "STAGE_OUT_OF_ORDER" in _codes(workflow, expected_stages=stages)
