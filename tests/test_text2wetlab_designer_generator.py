"""Deterministic import checks for local-model-authored Designer workflows."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_text2wetlab_designer import (  # noqa: E402
    _hardware_issues,
    _node_issues,
    _normalize_loop_backedges,
    _sanitize_model_deck,
    _scientific_pattern_issues,
)


def test_unknown_model_labware_is_removed_and_reported():
    workflow = {"deck": {"1": [{"labware_id": "invented"}], "2": [{"labware_id": "known"}]}}
    issues = _sanitize_model_deck(workflow, {"known"})
    assert workflow["deck"] == {"2": [{"labware_id": "known"}]}
    assert issues[0]["code"] == "UNRESOLVED_LABWARE"
    assert issues[0]["path"] == "/deck/1/0/labware_id"


def test_fixed_tip_cell_inside_repeated_loop_is_explicit_issue():
    workflow = {"graph": {
        "nodes": [
            {"id": 1, "type": "flow/Loop", "properties": {"count": 12}},
            {"id": 2, "type": "tips/TipsOn", "properties": {"location": 3, "tip_anchor_row": 0, "tip_anchor_col": 0}},
            {"id": 3, "type": "flow/End", "properties": {}},
        ],
        "links": [[1, 1, 0, 2, 0, -1], [2, 1, 1, 3, 0, -1]],
    }}
    assert [issue["code"] for issue in _node_issues(workflow)] == ["REPEATED_TIP_CELL"]


def test_script_node_is_rejected_for_unreviewed_import():
    workflow = {"graph": {"nodes": [{"id": 1, "type": "logic/Script", "properties": {"script": "pass"}}], "links": []}}
    assert _node_issues(workflow)[0]["code"] == "UNSUPPORTED_NODE"


def test_loop_return_wire_is_removed_without_changing_step_nodes():
    workflow = {"graph": {
        "nodes": [
            {"id": 1, "type": "flow/Start"},
            {"id": 2, "type": "flow/Loop"},
            {"id": 3, "type": "system/Manual"},
            {"id": 4, "type": "flow/End"},
        ],
        "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1],
                  [3, 3, 0, 2, 0, -1], [4, 2, 1, 4, 0, -1]],
    }}
    issues = _normalize_loop_backedges(workflow)
    assert [item["code"] for item in issues] == ["LOOP_BACKEDGE_REMOVED"]
    assert [node["id"] for node in workflow["graph"]["nodes"]] == [1, 2, 3, 4]
    assert [link[0] for link in workflow["graph"]["links"]] == [1, 2, 4]


def test_active_head_method_and_rack_mismatches_are_review_issues():
    workflow = {
        "deck": {"1": [{"labware_id": "lt-rack", "base_class": "tip_box"}]},
        "graph": {"nodes": [
            {"id": 1, "type": "tips/TipsOn", "properties": {"location": 1}},
            {"id": 2, "type": "liquid/Aspirate", "properties": {"liquid_class": "invented", "volume": 100}},
        ]},
    }
    context = {"head_type": "HT_384_D_70", "liquid_classes": [{"name": "known", "liquid_class_id": "known-id"}],
               "tipbox_choices": [{"labware_id": "st-rack"}]}
    assert {issue["code"] for issue in _hardware_issues(workflow, context, source_instruction="OT-2 task")} == {
        "INCOMPATIBLE_TIPBOX", "UNAVAILABLE_LIQUID_CLASS", "OT2_DECK_REMAP_REVIEW",
    }


def test_missing_sample_coverage_and_cross_source_tip_lifecycle_are_flagged():
    workflow = {"graph": {"nodes": [
        {"type": "tips/TipsOn", "properties": {}},
        {"type": "liquid/Aspirate", "properties": {"location": 1}},
        {"type": "liquid/Aspirate", "properties": {"location": 2}},
        {"type": "tips/TipsOff", "properties": {}},
    ]}}
    assert {issue["code"] for issue in _scientific_pattern_issues(
        workflow, source_instruction="Screen 96 transformant colonies.",
    )} == {"SAMPLE_COVERAGE_UNPROVEN", "CROSS_SOURCE_TIP_REVIEW"}
