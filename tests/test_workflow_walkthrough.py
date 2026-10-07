"""A Designer walkthrough animates order and safe hovers, never robot tasks."""

from __future__ import annotations

import copy

import pytest

from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis
from pybravo.workflow.walkthrough import WorkflowWalkthrough


def _workflow(node_types: list[str], links: list[list[int]], *, properties=None):
    properties = properties or {}
    return {
        "name": "Saved generated draft",
        "protocol_generated_draft": True,
        "deck": {"1": [{"labware_id": "proposed-rack", "name": "Proposed rack"}]},
        "graph": {
            "nodes": [
                {"id": index, "type": kind, "title": kind,
                 "properties": properties.get(index, {})}
                for index, kind in enumerate(node_types, 1)
            ],
            "links": links,
        },
    }


def _linear(*node_types: str, properties=None):
    kinds = ["flow/Start", *node_types, "flow/End"]
    return _workflow(kinds, [
        [index, index, 0, index + 1, 0, -1]
        for index in range(1, len(kinds))
    ], properties=properties)


@pytest.mark.asyncio
async def test_native_walkthrough_hovers_only_and_keeps_graph_deck_profile_unchanged():
    workflow = _linear("tips/TipsOn", "liquid/Aspirate", "liquid/Dispense",
                       "plate/PickPlace", properties={
                           2: {"location": 1},
                           3: {"location": 1, "volume": 5, "liquid_class": ""},
                           4: {"location": 2, "volume": 5, "liquid_class": "unreviewed"},
                           5: {"pick_location": 1, "place_location": 2},
                       })
    original_workflow = copy.deepcopy(workflow)
    profile = BravoProfile.default()
    original_profile = copy.deepcopy(profile)
    events = []
    walkthrough = WorkflowWalkthrough(workflow, profile, on_event=events.append,
                                      step_delay_s=0, diagnostics=[{
                                          "code": "invalid_liquid_class",
                                          "reason": "Catalog did not recognize the method.",
                                      }])
    result = await walkthrough.execute()

    assert result == {
        "status": "visualized", "visited_nodes": 5,
        "simulation_kind": "visual_walkthrough", "qualification_granted": False,
        "validation_passed": False,
    }
    assert workflow == original_workflow
    assert profile._to_dict() == original_profile._to_dict()
    assert events[0]["type"] == "workflow:start"
    assert events[-1]["type"] == "workflow:complete"
    assert events[-1]["status"] == "visualized"
    assert all(event["simulation_kind"] == "visual_walkthrough"
               and event["qualification_granted"] is False
               and event["validation_passed"] is False for event in events)
    assert [event["node_id"] for event in events if event["type"] == "workflow:node_start"] == [2, 3, 4, 5, 6]
    assert {event["status"] for event in events
            if event["type"] == "workflow:node_complete"} == {"visualized"}
    assert {event["execution_status"] for event in events
            if event["type"] == "workflow:node_complete"} == {"not_executed"}
    positions = [event for event in events if event["type"] == "workflow:positions"]
    assert [event["deck_location"] for event in positions] == [1, 1, 2, 1, 2]
    assert all(event["pose"] == "retracted_hover" and event["positions"]["Z"] == 0
               and event["positions"]["Zg"] == -20
               and event["positions"]["G"] == event["positions"]["W"] == 0
               for event in positions)
    assert positions[0]["positions"]["X"] == profile.teachpoints.get_teachpoint(1, Axis.X)
    warnings = [event for event in events if event["type"] == "workflow:task_warning"]
    assert {event["code"] for event in warnings} >= {
        "invalid_liquid_class", "tips_not_executed", "liquid_method_missing",
        "liquid_method_unverified", "plate_not_moved",
    }
    assert any(event["warning"] == "Catalog did not recognize the method." for event in warnings)
    assert not any(event["type"] in {"workflow:tips_change", "workflow:plate_pick",
                                             "workflow:plate_place"} for event in events)


@pytest.mark.asyncio
async def test_missing_or_out_of_range_location_keeps_head_stationary():
    profile = BravoProfile.default()
    # A profile teachpoint that exceeds both the profile and machine bounds
    # must not be silently clamped into a misleading deck position.
    profile.teachpoints.set_teachpoint(2, Axis.X, 10000)
    workflow = _linear("liquid/Aspirate", "liquid/Dispense", properties={
        2: {"location": "missing", "volume": 5},
        3: {"location": 2, "volume": 5},
    })
    events = []
    await WorkflowWalkthrough(workflow, profile, events.append, step_delay_s=0).execute()
    assert not any(event["type"] == "workflow:positions" for event in events)
    assert {event["code"] for event in events if event["type"] == "workflow:task_warning"} >= {
        "unresolved_location", "unavailable_safe_hover",
    }
    assert events[-1]["validation_passed"] is False


@pytest.mark.asyncio
async def test_static_loop_repeats_visual_body_only_then_continues_done_path():
    workflow = _workflow(
        ["flow/Start", "flow/Loop", "liquid/Mix", "system/Wait", "flow/End"],
        [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1],
         [3, 2, 1, 4, 0, -1], [4, 4, 0, 5, 0, -1]],
        properties={2: {"count": 2}, 3: {"location": 1, "volume": 5}},
    )
    events = []
    result = await WorkflowWalkthrough(workflow, BravoProfile.default(), events.append,
                                       step_delay_s=0).execute()
    assert result["visited_nodes"] == 5  # loop, body twice, wait, end
    assert [event["node_id"] for event in events if event["type"] == "workflow:node_start"] == [2, 3, 3, 4, 5]
    assert [event["step_name"] for event in events
            if event["type"] == "workflow:node_step" and "visual iteration" in event["step_name"]] == [
                "visual iteration 1/2; no tasks executed",
                "visual iteration 2/2; no tasks executed",
            ]
    assert [event["node_id"] for event in events if event["type"] == "workflow:node_complete"].count(2) == 1


@pytest.mark.asyncio
async def test_abort_stops_before_next_task_and_never_qualifies():
    workflow = _linear("liquid/Aspirate", "liquid/Dispense", properties={
        2: {"location": 1}, 3: {"location": 2},
    })
    events = []
    walkthrough = None

    async def on_event(event):
        events.append(event)
        if event["type"] == "workflow:node_complete" and event["node_id"] == 2:
            walkthrough.abort()

    walkthrough = WorkflowWalkthrough(workflow, BravoProfile.default(), on_event,
                                      step_delay_s=0)
    result = await walkthrough.execute()
    assert result["status"] == "aborted"
    assert [event["node_id"] for event in events if event["type"] == "workflow:node_start"] == [2]
    assert events[-1]["status"] == "aborted"
    assert events[-1]["qualification_granted"] is False
    await walkthrough.stop()


@pytest.mark.parametrize("workflow", [
    _workflow(["flow/Start", "flow/IfElse", "flow/End"],
              [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]]),
    _workflow(["flow/Start", "liquid/Aspirate", "flow/End"],
              [[1, 1, 0, 2, 0, -1], [2, 1, 0, 3, 0, -1]]),
    _workflow(["flow/Start", "liquid/Aspirate", "flow/End"],
              [[1, 1, 0, 3, 0, -1]]),
    _workflow(["flow/Start", "liquid/Aspirate", "flow/End"],
              [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1],
               [3, 3, 0, 2, 0, -1]]),
    _workflow(["flow/Start", "flow/Loop", "liquid/Mix", "flow/End"],
              [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1],
               [3, 2, 1, 4, 0, -1], [4, 3, 0, 4, 0, -1]],
              properties={2: {"count": 2}}),
    _workflow(["flow/Start", "logic/Script", "flow/End"],
              [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1]]),
])
def test_malformed_or_executable_graph_fails_before_any_event(workflow):
    events = []
    with pytest.raises(ValueError):
        WorkflowWalkthrough(workflow, BravoProfile.default(), events.append)
    assert events == []


def test_walkthrough_visit_cap_prevents_expanding_large_static_loop():
    workflow = _workflow(
        ["flow/Start", "flow/Loop", "liquid/Mix", "flow/End"],
        [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1], [3, 2, 1, 4, 0, -1]],
        properties={2: {"count": 1000}, 3: {"location": 1}},
    )
    events = []
    with pytest.raises(ValueError, match="visit cap"):
        WorkflowWalkthrough(workflow, BravoProfile.default(), events.append,
                            max_visits=20)
    assert events == []
