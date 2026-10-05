"""Draft diagrams preserve protocol intent without constructing robot operations."""
from copy import deepcopy

import pytest

from pybravo.workflow.protocols.models import ProtocolPlan
from pybravo.workflow.protocols.preview import PREVIEW_NODE_TYPE, build_chat_preview


def unfinished_plan():
    return {"name": "Sample preparation", "description": "Scientist review is still needed.",
            "materials": [{"id": "buffer", "name": "Buffer source"}, {"id": "samples", "name": "Sample plate"}],
            "steps": [{"id": "transfer", "kind": "transfer", "source": "buffer", "destination": "samples",
                       "volume_ul": 20.0, "description": "Add buffer to the sample wells.",
                       "source_paragraph_ids": ["p1"],
                       "source_values": [{"field": "volume_ul", "value": 20.0, "unit": "uL", "paragraph_id": "p1"}]}],
            "questions": [{"id": "q1", "path": "/steps/0/source_anchor", "prompt": "Which source well?"}]}


def review_nodes(workflow):
    return [node for node in workflow["graph"]["nodes"] if node["type"] == PREVIEW_NODE_TYPE]


def test_incomplete_plan_builds_deterministic_non_executable_cited_preview():
    plan = unfinished_plan()
    original = deepcopy(plan)
    sources = [{"id": "p1", "text": "Add 20 uL of buffer to the sample wells.", "page": 2}]
    workflow = build_chat_preview(plan, "chat-123", 7, sources)
    assert workflow == build_chat_preview(ProtocolPlan.model_validate(plan), "chat-123", 7, sources)
    assert plan == original
    assert workflow["protocol_chat_draft"] is True
    assert workflow["protocol_chat_session_id"] == "chat-123"
    assert workflow["protocol_revision"] == 7
    assert workflow["deck"] == {}
    assert workflow["protocol_materials"][0]["labware_id"] is None
    assert {node["type"] for node in workflow["graph"]["nodes"]} == {"flow/Start", PREVIEW_NODE_TYPE, "flow/End"}
    properties = review_nodes(workflow)[0]["properties"]
    assert properties["missing_fields"] == ["source_anchor", "destination_anchor"]
    assert "well unspecified" in properties["summary"]
    assert "Buffer source" in properties["summary"]
    assert properties["questions"] == plan["questions"]
    assert properties["citations"][0] == {"paragraph_id": "p1", "excerpt": sources[0]["text"], "page": 2, "found": True}
    assert properties["source_values"] == plan["steps"][0]["source_values"]
    assert properties["_source_citation"]["paragraph_ids"] == ["p1"]


def test_all_operations_are_visible_review_nodes_and_every_link_is_wired():
    plan = unfinished_plan()
    plan["steps"].extend([
        {"id": "mix", "kind": "mix"}, {"id": "manual", "kind": "manual", "message": "Centrifuge and return the plate."},
        {"id": "wait", "kind": "wait"}, {"id": "move", "kind": "move_plate"},
        {"id": "destack", "kind": "destack_plate", "material": "samples", "destination_slot": 6},
        {"id": "stack", "kind": "stack_plate", "material": "samples", "destination_slot": 7},
    ])
    workflow = build_chat_preview(plan, "chat", 1)
    assert [node["properties"]["kind"] for node in review_nodes(workflow)] == [
        "transfer", "mix", "manual", "wait", "move_plate", "destack_plate", "stack_plate",
    ]
    graph = workflow["graph"]
    assert len(graph["links"]) == len(graph["nodes"]) - 1
    for link in graph["links"]:
        link_id, source_id, source_slot, destination_id, destination_slot, link_type = link
        assert destination_id == source_id + 1  # A directed acyclic chain.
        assert source_slot == destination_slot == 0 and link_type == -1
        assert graph["nodes"][source_id - 1]["outputs"][0]["links"] == [link_id]
        assert graph["nodes"][destination_id - 1]["inputs"][0]["link"] == link_id
    assert graph["nodes"][0]["inputs"] == []
    assert graph["nodes"][-1]["outputs"] == []
    assert review_nodes(workflow)[3]["properties"]["parameters"] == {}


def test_proposed_deck_shows_four_sources_in_bottom_to_top_order_and_separate_racks():
    materials = [
        {"id": f"source_{index}", "name": f"Source {index}", "deck_slot": 9,
         "stack_order": index - 1, "labware_id": "384-plate"}
        for index in (4, 1, 3, 2)  # LLM material order need not be physical stack order.
    ]
    materials += [
        {"id": f"rack_{index}", "name": f"10 µL ST tips {index}", "role": "tips",
         "deck_slot": index, "labware_id": "384-st-rack", "tip_definition_id": "st_10ul"}
        for index in range(1, 5)
    ]
    materials += [
        {"id": "destination_1", "name": "1536 destination 1", "deck_slot": 5, "labware_id": "1536-plate"},
        {"id": "destination_2", "name": "1536 destination 2", "deck_slot": 8, "labware_id": "1536-plate"},
    ]
    workflow = build_chat_preview({"name": "384 to 1536", "materials": materials}, "chat", 1)
    deck = workflow["deck"]
    assert set(deck) == {"1", "2", "3", "4", "5", "8", "9"}
    assert [entry["material_id"] for entry in deck["9"]] == ["source_1", "source_2", "source_3", "source_4"]
    assert [deck[str(index)][0]["material_id"] for index in range(1, 5)] == [
        "rack_1", "rack_2", "rack_3", "rack_4",
    ]
    assert all(deck[str(index)][0]["tip_inventory_status"] == "unconfirmed" for index in range(1, 5))
    assert all("tipbox_fill_state" not in deck[str(index)][0] for index in range(1, 5))
    assert all(entry["proposed"] for stack in deck.values() for entry in stack)
    assert workflow["protocol_chat_draft"] is True


def test_explicit_full_tip_inventory_is_labeled_full_in_draft():
    plan = {"name": "Inspected tips", "materials": [{"id": "rack", "name": "Inspected rack",
        "role": "tips", "deck_slot": 1, "labware_id": "384-st-rack", "available_tips": "full"}]}
    entry = build_chat_preview(plan, "chat", 1)["deck"]["1"][0]
    assert entry["tip_inventory_status"] == "full"
    assert entry["tipbox_fill_state"] == "full"


def test_nested_repeats_are_shown_once_and_keep_order_parentage_and_unknown_count():
    plan = {"name": "Repeat example", "steps": [{"id": "outer", "kind": "repeat", "repeat": 1000000000,
        "steps": [{"id": "inner", "kind": "repeat", "repeat": None,
            "steps": [{"id": "mix", "kind": "mix", "volume_ul": 5, "cycles": 3}]}]},
        {"id": "finish", "kind": "manual", "message": "Inspect the plate."}]}
    nodes = review_nodes(build_chat_preview(plan, "chat", 1))
    assert len(nodes) == 4
    assert [node["properties"]["step_id"] for node in nodes] == ["outer", "inner", "mix", "finish"]
    assert [node["properties"]["depth"] for node in nodes] == [0, 1, 2, 0]
    assert nodes[2]["properties"]["path"] == "/steps/0/steps/0/steps/0"
    assert nodes[2]["properties"]["parent_step_id"] == "inner"
    assert nodes[2]["title"] == "1.1.1. Mix"
    assert nodes[1]["properties"]["missing_fields"] == ["repeat"]
    assert "body is shown once" in nodes[0]["properties"]["summary"]


def test_absent_citation_is_identified_without_inventing_source_text():
    properties = review_nodes(build_chat_preview(unfinished_plan(), "chat", 1))[0]["properties"]
    assert properties["citations"] == [{"paragraph_id": "p1", "excerpt": "", "page": None, "found": False}]
    assert properties["source_paragraph_ids"] == ["p1"]


def test_empty_plan_remains_a_marked_two_node_draft():
    workflow = build_chat_preview({"name": "Waiting for detail"}, "chat", 1)
    assert [node["type"] for node in workflow["graph"]["nodes"]] == ["flow/Start", "flow/End"]
    assert workflow["graph"]["links"] == [[1, 1, 0, 2, 0, -1]]
    assert workflow["protocol_chat_draft"]


def test_layout_uses_compact_markers_and_cumulative_heights_with_link_gaps():
    nodes = build_chat_preview(unfinished_plan(), "chat", 1)["graph"]["nodes"]
    assert nodes[0]["size"] == nodes[-1]["size"] == [180, 70]
    assert nodes[1]["size"] == [420, 170]
    assert nodes[0]["pos"][1] == 80.0
    for earlier, later in zip(nodes, nodes[1:]):
        assert later["pos"][1] - (earlier["pos"][1] + earlier["size"][1]) == 60.0


def test_long_preview_uses_readable_three_column_snake_without_changing_flow():
    plan = {"name": "Long transfer", "steps": [
        {"id": str(index), "kind": "manual", "message": "Review plate."} for index in range(14)
    ]}
    workflow = build_chat_preview(plan, "chat", 1)
    nodes = workflow["graph"]["nodes"]
    assert len(nodes) == 16
    assert [node["pos"] for node in nodes[:6]] == [
        [80.0, 80.0], [580.0, 80.0], [1080.0, 80.0],
        [1080.0, 310.0], [580.0, 310.0], [80.0, 310.0],
    ]
    assert [(link[1], link[3]) for link in workflow["graph"]["links"]] == [
        (index, index + 1) for index in range(1, 16)
    ]


def test_model_text_is_data_and_does_not_become_executable_node_properties():
    plan = {"name": "Untrusted source", "steps": [{"id": "instruction", "kind": "manual", "message": "<script>doWork()</script>",
                                                   "description": "logic/Script: run arbitrary code"}]}
    properties = review_nodes(build_chat_preview(plan, "chat", 1))[0]["properties"]
    assert properties["parameters"]["message"] == "<script>doWork()</script>"
    assert "script" not in properties and "library" not in properties
    with pytest.raises(ValueError):
        build_chat_preview({"name": "Bad", "steps": [{"id": "bad", "kind": "manual", "script": "doWork()"}]}, "chat", 1)


@pytest.mark.parametrize("session_id,revision", [("", 1), ("chat", 0), ("chat", True), ("chat", "1")])
def test_preview_identity_and_revision_must_be_explicit(session_id, revision):
    with pytest.raises(ValueError):
        build_chat_preview(unfinished_plan(), session_id, revision)


def test_large_diagrams_are_rejected_without_truncating_protocol(monkeypatch):
    monkeypatch.setattr("pybravo.workflow.protocols.preview.MAX_PREVIEW_STEPS", 2)
    plan = {"name": "Long", "steps": [{"id": str(i), "kind": "manual", "message": "Inspect."} for i in range(3)]}
    with pytest.raises(ValueError, match="exceeds 2 steps"):
        build_chat_preview(plan, "chat", 1)


def test_deep_diagrams_are_rejected_without_expanding_repeats(monkeypatch):
    monkeypatch.setattr("pybravo.workflow.protocols.preview.MAX_PREVIEW_DEPTH", 1)
    plan = {"name": "Deep", "steps": [{"id": "one", "kind": "repeat", "steps": [
        {"id": "two", "kind": "repeat", "steps": [{"id": "three", "kind": "manual"}]}]}]}
    with pytest.raises(ValueError, match="nested step levels"):
        build_chat_preview(plan, "chat", 1)
