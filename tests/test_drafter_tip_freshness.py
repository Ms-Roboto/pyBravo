"""Static drafter checks for repeated tip inventory roles, without hardware."""

from __future__ import annotations

from pybravo.workflow.drafter.prompt import build_system_prompt
from pybravo.workflow.drafter.schema import DraftedWorkflow
from pybravo.workflow.drafter.validator import validate_drafted_workflow


def _loop_draft(*, pickup_location: int | str = 1,
                return_location: int | str = 1,
                pickup_anchor: tuple[int, int] | None = (0, 0),
                return_anchor: tuple[int, int] | None = (0, 0),
                count: int = 3) -> DraftedWorkflow:
    on = {"location": pickup_location}
    off = {"location": return_location}
    if pickup_anchor is not None:
        on.update({"tip_anchor_row": pickup_anchor[0], "tip_anchor_col": pickup_anchor[1]})
    if return_anchor is not None:
        off.update({"tip_anchor_row": return_anchor[0], "tip_anchor_col": return_anchor[1]})
    nodes = [
        {"id": 1, "type": "flow/Start"},
        {"id": 2, "type": "flow/Loop", "properties": {"count": count}},
        {"id": 3, "type": "tips/TipsOn", "properties": on},
        {"id": 4, "type": "liquid/Aspirate", "properties": {
            "location": 2, "volume": 1, "liquid_class": "Synthetic Test Method",
        }},
        {"id": 5, "type": "tips/TipsOff", "properties": off},
        {"id": 6, "type": "flow/End"},
    ]
    links = [
        {"id": 1, "origin_id": 1, "origin_slot": 0, "target_id": 2, "target_slot": 0},
        {"id": 2, "origin_id": 2, "origin_slot": 0, "target_id": 3, "target_slot": 0},
        {"id": 3, "origin_id": 3, "origin_slot": 0, "target_id": 4, "target_slot": 0},
        {"id": 4, "origin_id": 4, "origin_slot": 0, "target_id": 5, "target_slot": 0},
        {"id": 5, "origin_id": 2, "origin_slot": 1, "target_id": 6, "target_slot": 0},
    ]
    return DraftedWorkflow.model_validate({
        "name": "Synthetic repeated-tip graph",
        "graph": {"nodes": nodes, "links": links},
    })


def _codes(draft: DraftedWorkflow) -> set[str]:
    return {issue.code for issue in validate_drafted_workflow(draft)}


def test_repeated_same_rack_fixed_anchor_flags_returned_tip_pickup():
    assert "LOOP_FIXED_SPENT_TIP_ANCHOR" in _codes(_loop_draft())


def test_repeated_same_rack_different_anchors_still_mixes_inventory():
    codes = _codes(_loop_draft(return_anchor=(0, 1)))
    assert "LOOP_MIXES_CLEAN_SPENT_TIPS" in codes
    assert "LOOP_FIXED_SPENT_TIP_ANCHOR" not in codes


def test_distinct_clean_and_spent_racks_do_not_trigger_static_mix_check():
    codes = _codes(_loop_draft(return_location=3))
    assert not {"LOOP_FIXED_SPENT_TIP_ANCHOR", "LOOP_MIXES_CLEAN_SPENT_TIPS"} & codes


def test_single_iteration_or_dynamic_rack_reference_is_not_guessed():
    assert "LOOP_FIXED_SPENT_TIP_ANCHOR" not in _codes(_loop_draft(count=1))
    dynamic = _loop_draft(pickup_location="iter:1,3", return_location="iter:1,3")
    assert "LOOP_FIXED_SPENT_TIP_ANCHOR" not in _codes(dynamic)


def test_tip_lifecycle_outside_repeated_liquid_body_is_reviewed():
    draft = _loop_draft(return_location=3)
    # Move the pickup before the loop and the ejection after it, leaving a
    # liquid action in the repeated body. Reusing that mounted set may be
    # intentional, but the drafter cannot infer its suitability.
    draft.graph.links[0].target_id = 3
    draft.graph.links[1].origin_id = 3
    draft.graph.links[1].target_id = 2
    draft.graph.links[2].origin_id = 2
    draft.graph.links[2].target_id = 4
    draft.graph.links[3].origin_id = 2
    draft.graph.links[3].origin_slot = 1
    draft.graph.links[3].target_id = 5
    draft.graph.links[4].origin_id = 5
    draft.graph.links[4].origin_slot = 0
    draft.graph.links[4].target_id = 6
    assert "LOOP_TIP_REUSE_UNREVIEWED" in _codes(draft)


def test_prompt_distinguishes_fresh_supply_from_spent_return():
    prompt = build_system_prompt(include_exemplars=False)
    assert "Returned tips remain spent" in prompt
    assert "clean pickup supply distinct from a spent-tip return" in prompt
    assert "fresh-well count" in prompt
    assert 'tipbox_fill_state' in prompt


def test_proposed_empty_return_box_survives_model_schema_roundtrip():
    draft = _loop_draft(return_location=3).model_dump()
    draft['deck'] = {
        '1': [{'labware_id': 'same-compatible-rack', 'tipbox_fill_state': 'full'}],
        '3': [{'labware_id': 'same-compatible-rack', 'tipbox_fill_state': 'empty'}],
    }
    parsed = DraftedWorkflow.model_validate(draft).model_dump()
    assert parsed['deck']['1'][0]['tipbox_fill_state'] == 'full'
    assert parsed['deck']['3'][0]['tipbox_fill_state'] == 'empty'


def test_loop_rejects_full_dedicated_return_and_empty_clean_supply():
    payload = _loop_draft(return_location=3).model_dump()
    payload['deck'] = {
        '1': [{'labware_id': 'compatible-rack', 'tipbox_fill_state': 'empty'}],
        '3': [{'labware_id': 'compatible-rack', 'tipbox_fill_state': 'full'}],
    }
    assert {'LOOP_OCCUPIED_RETURN_BOX', 'LOOP_EMPTY_CLEAN_SUPPLY'} <= _codes(
        DraftedWorkflow.model_validate(payload)
    )
    payload['deck']['1'][0]['tipbox_fill_state'] = 'full'
    payload['deck']['3'][0]['tipbox_fill_state'] = 'empty'
    assert not {'LOOP_OCCUPIED_RETURN_BOX', 'LOOP_EMPTY_CLEAN_SUPPLY'} & _codes(
        DraftedWorkflow.model_validate(payload)
    )
