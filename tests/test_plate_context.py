from copy import deepcopy

import pytest

from pybravo.workflow.plate_context import resolve_plate_context

COLUMN = {"subset_type": "column", "subset_config": "back_left", "column_count": 1}
ROW = {"subset_type": "row", "subset_config": "front_right", "row_count": 1}


def node(node_id, kind, **properties):
    return {"id": node_id, "type": kind, "properties": properties}


def workflow(nodes=None):
    nodes = nodes or [
        node(1, "flow/Start"),
        node(2, "tips/TipsOn", location=1, head_mode=COLUMN),
        node(3, "liquid/Aspirate", location=2, head_mode=ROW),
    ]
    return {
        "deck": {
            "1": [{"labware_id": "rack", "tip_definition_id": "st_10ul", "tipbox_fill_state": "full"}],
            "2": [{"labware_id": "plate", "is_lidded": False}],
        },
        "graph": {"nodes": nodes, "links": [
            [index, before["id"], 0, after["id"], 0, -1]
            for index, (before, after) in enumerate(zip(nodes, nodes[1:]), 1)
        ]},
    }


def test_resolves_mounted_pickup_not_stale_liquid_mode_and_never_mutates_input():
    wf = workflow()
    original = deepcopy(wf)
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "resolved"
    assert result["head_mode"] == COLUMN
    assert result["location"] == 2
    assert result["tip_node_id"] == 2
    assert result["tip_location"] == 1
    assert result["tip_definition_id"] == "st_10ul"
    assert result["tip_labware"] == wf["deck"]["1"][0]
    result["deck"]["1"][0]["labware_id"] = "changed"
    result["tip_labware"]["tip_definition_id"] = "changed"
    result["head_mode"]["subset_type"] = "changed"
    assert wf == original


def test_missing_pickup_mode_remains_explicitly_inherited():
    wf = workflow()
    wf["graph"]["nodes"][1]["properties"].pop("head_mode")
    assert resolve_plate_context(wf, 3)["head_mode"] is None
    assert resolve_plate_context(wf, 3)["status"] == "resolved"


@pytest.mark.parametrize("after_off", [False, True])
def test_no_mounted_pickup_is_unresolved(after_off):
    nodes = [node(1, "flow/Start")]
    if after_off:
        nodes.extend([node(2, "tips/TipsOn", location=1, head_mode=COLUMN), node(4, "tips/TipsOff", location=1)])
    nodes.append(node(3, "liquid/Aspirate", location=2, head_mode=ROW))
    result = resolve_plate_context(workflow(nodes), 3)
    assert result["status"] == "unresolved"
    assert "No preceding Tips On" in result["message"]
    assert result["head_mode"] is None


@pytest.mark.parametrize("off_mode, expected", [(None, COLUMN), (ROW, ROW)])
def test_mode_configuration_survives_tip_cycles(off_mode, expected):
    wf = workflow([
        node(1, "flow/Start"), node(2, "tips/TipsOn", location=1, head_mode=COLUMN),
        node(4, "tips/TipsOff", location=1, head_mode=off_mode),
        node(5, "tips/TipsOn", location=1), node(3, "liquid/Dispense", location=2),
    ])
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "resolved"
    assert result["head_mode"] == expected
    assert result["tip_node_id"] == 5


def test_static_destack_pick_place_and_stack_use_resulting_top_plate():
    wf = workflow([
        node(1, "flow/Start"),
        node(6, "plate/Destack", source_location=9, destination_location=6),
        node(7, "plate/PickPlace", pick_location=6, place_location=7),
        node(8, "plate/Stack", source_location=7, base_location=2),
        node(2, "tips/TipsOn", location=1, head_mode=COLUMN), node(3, "liquid/Aspirate", location=2),
    ])
    wf["deck"]["9"] = [{"labware_id": "bottom"}, {"labware_id": "top"}]
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "resolved"
    assert result["deck"]["2"][-1]["labware_id"] == "top"
    assert result["deck"]["9"] == [{"labware_id": "bottom"}]
    assert "6" not in result["deck"] and "7" not in result["deck"]
    assert len(wf["deck"]["9"]) == 2


def test_loaded_rack_identity_is_captured_before_rack_moves():
    wf = workflow([
        node(1, "flow/Start"), node(2, "tips/TipsOn", location=1, head_mode=COLUMN),
        node(4, "plate/PickPlace", pick_location=1, place_location=8),
        node(3, "liquid/Dispense", location=2),
    ])
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "resolved"
    assert result["tip_labware"]["labware_id"] == "rack"
    assert result["tip_location"] == 1
    assert "1" not in result["deck"]
    assert result["deck"]["8"][-1]["labware_id"] == "rack"


def branched_workflow(different=False):
    wf = workflow([
        node(1, "flow/Start"), node(2, "tips/TipsOn", location=1, head_mode=COLUMN),
        node(4, "flow/IfElse", condition="var:choice"), node(5, "system/Wait", duration_s=1),
        node(6, "plate/PickPlace", pick_location=2, place_location=8) if different else node(6, "system/Wait", duration_s=2),
        node(3, "liquid/Aspirate", location=2),
    ])
    wf["graph"]["links"] = [
        [1, 1, 0, 2, 0, -1], [2, 2, 0, 4, 0, -1], [3, 4, 0, 5, 0, -1],
        [4, 4, 1, 6, 0, -1], [5, 5, 0, 3, 0, -1], [6, 6, 0, 3, 0, -1],
    ]
    return wf


def test_identical_contexts_at_a_branch_join_can_resolve():
    assert resolve_plate_context(branched_workflow(), 3)["status"] == "resolved"


def test_different_decks_at_a_branch_join_do_not_choose_first_predecessor():
    result = resolve_plate_context(branched_workflow(True), 3)
    assert result["status"] == "unresolved"
    assert "different deck or mounted-tip contexts" in result["message"]


def test_different_pickups_at_a_branch_join_are_unresolved():
    wf = branched_workflow()
    wf["graph"]["nodes"][1] = node(2, "system/Wait", duration_s=1)
    wf["graph"]["nodes"][3] = node(5, "tips/TipsOn", location=1, head_mode=COLUMN)
    wf["graph"]["nodes"][4] = node(6, "tips/TipsOn", location=1, head_mode=ROW)
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "different deck or mounted-tip contexts" in result["message"]


def loop_workflow():
    wf = workflow([
        node(1, "flow/Start"), node(4, "flow/Loop", count=12),
        node(2, "tips/TipsOn", location=1, head_mode=COLUMN), node(3, "liquid/Aspirate", location=2),
        node(5, "tips/TipsOff", location=1), node(6, "flow/End"),
    ])
    wf["graph"]["links"] = [
        [1, 1, 0, 4, 0, -1], [2, 4, 0, 2, 0, -1], [3, 2, 0, 3, 0, -1],
        [4, 3, 0, 5, 0, -1], [5, 4, 1, 6, 0, -1],
    ]
    return wf


def test_fixed_deck_loop_repeated_pickup_has_one_stable_mapping_context():
    assert resolve_plate_context(loop_workflow(), 3)["status"] == "resolved"


def test_loop_inherited_mode_must_remain_identical_across_iterations():
    wf = loop_workflow()
    wf["graph"]["nodes"][2]["properties"].pop("head_mode")
    wf["graph"]["nodes"][4]["properties"]["head_mode"] = ROW
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "different deck" in result["message"]


def test_plate_moves_inside_loop_do_not_reuse_first_iteration_context():
    wf = loop_workflow()
    wf["graph"]["nodes"].append(node(7, "plate/PickPlace", pick_location=2, place_location=8))
    wf["graph"]["links"].append([6, 5, 0, 7, 0, -1])
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "Plate moves inside a loop" in result["message"]


@pytest.mark.parametrize("count", ["var:n", 1000, -1, True])
def test_unbounded_or_dynamic_loop_does_not_guess(count):
    wf = loop_workflow()
    wf["graph"]["nodes"][1]["properties"]["count"] = count
    assert resolve_plate_context(wf, 3)["status"] == "unresolved"


@pytest.mark.parametrize("kind", ["system/Manual", "logic/Script", "plate/Delid", "plate/Mount", "sensor/ScanStackHeight", "custom/Unknown"])
def test_unknown_context_changing_tasks_before_target_are_unresolved(kind):
    wf = workflow([node(1, "flow/Start"), node(2, "tips/TipsOn", location=1), node(4, kind), node(3, "liquid/Aspirate", location=2)])
    assert resolve_plate_context(wf, 3)["status"] == "unresolved"


@pytest.mark.parametrize("target_index", [1, 2])
@pytest.mark.parametrize("location", ["iter:1,2", "var:plate", None, True, 0, 10])
def test_dynamic_or_invalid_pickup_and_liquid_locations_are_unresolved(target_index, location):
    wf = workflow()
    wf["graph"]["nodes"][target_index]["properties"]["location"] = location
    assert resolve_plate_context(wf, 3)["status"] == "unresolved"


def test_unconnected_target_does_not_inherit_an_unrelated_pickup():
    wf = workflow()
    wf["graph"]["links"].pop()
    assert resolve_plate_context(wf, 3)["status"] == "unresolved"


def test_library_code_is_never_executed_for_mapping():
    wf = workflow()
    wf["library"] = "raise Exception('must not execute')"
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "library code" in result["message"]


def test_dict_links_and_single_item_deck_entries_are_supported_and_data_links_ignored():
    wf = workflow()
    wf["deck"]["1"] = wf["deck"]["1"][0]
    wf["graph"]["links"] = [dict(zip(("id", "origin_id", "origin_slot", "target_id", "target_slot", "link_type"), link)) for link in wf["graph"]["links"]]
    wf["graph"]["links"].append({"id": 8, "origin_id": 3, "origin_slot": 1, "target_id": 2, "target_slot": 1, "link_type": "string"})
    assert resolve_plate_context(wf, 3)["status"] == "resolved"


def test_cyclic_flow_fails_closed():
    wf = workflow()
    wf["graph"]["links"][1] = [2, 2, 0, 1, 0, -1]
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "cyclic flow" in result["message"]


def test_multiple_links_on_one_flow_output_fail_closed():
    wf = workflow()
    wf["graph"]["links"].append([3, 1, 0, 3, 0, -1])
    assert resolve_plate_context(wf, 3)["status"] == "unresolved"


def test_destack_into_occupied_position_is_not_assumed_to_succeed():
    wf = workflow([
        node(1, "flow/Start"), node(4, "plate/Destack", source_location=9, destination_location=2),
        node(2, "tips/TipsOn", location=1), node(3, "liquid/Aspirate", location=2),
    ])
    wf["deck"]["9"] = [{"labware_id": "source"}]
    result = resolve_plate_context(wf, 3)
    assert result["status"] == "unresolved"
    assert "empty destination" in result["message"]


def test_tasks_after_target_do_not_invalidate_its_context():
    wf = workflow()
    wf["graph"]["nodes"].append(node(4, "system/Manual"))
    wf["graph"]["links"].append([3, 3, 0, 4, 0, -1])
    assert resolve_plate_context(wf, 3)["status"] == "resolved"
