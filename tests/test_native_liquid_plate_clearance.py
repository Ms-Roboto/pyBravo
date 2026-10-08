"""A mounted 384-head column enters CellVis wells beside real ST10 racks.

The plate dimensions come from the CellVis P384-1.5H-N drawing. This is a
mechanical regression fixture, not a liquid-method qualification. In particular,
an above-rim rehearsal must not satisfy the bottom-clearance assertions below.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis
from pybravo.workflow.executor import WorkflowExecutor

ROOT = Path(__file__).resolve().parents[1]
RACK_ID = "lw-4914769d0af7"
PLATE_ID = "lw-34358f93e2a0"


def _fixture_bravo():
    robot = Bravo(profile=BravoProfile.load(ROOT / "profiles/simulation.yaml"), mode="simulation")
    rack = robot.labware_catalog.get_definition(RACK_ID)
    original_plate = robot.labware_catalog.get_definition(PLATE_ID)
    assert rack is not None and original_plate is not None
    plate = replace(
        original_plate,
        length_mm=127.6,
        width_mm=85.6,
        height_mm=14.33,
        rows=16,
        cols=24,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        well_depth_mm=11.38,
        # Narrowest square-well width; the circular collision aperture is
        # conservatively inscribed inside it.
        well_diameter_mm=3.3,
    )
    robot._labware_catalog = InMemoryLabwareCatalog([rack, plate])
    robot.connect()
    robot.controller.set_move_timing_enabled(False)
    return robot


def _graph():
    tasks = [
        ("flow/Start", {}),
        ("tips/TipsOn", {
            "location": 1,
            "head_mode": {"subset_type": "column", "subset_config": "back_left", "column_count": 1},
            "tip_anchor_row": 0,
            "tip_anchor_col": 23,
        }),
    ]
    for anchor in ("A1", "A2", "A3"):
        for operation in ("Aspirate", "Dispense"):
            tasks.append((f"liquid/{operation}", {
                "location": 2, "anchor": anchor, "volume": 10, "distance_from_bottom": 1,
            }))
    tasks += [
        ("tips/TipsOff", {"location": 3, "tip_anchor_row": 0, "tip_anchor_col": 0}),
        ("flow/End", {}),
    ]
    return {
        "nodes": [{"id": i, "type": kind, "properties": props, "outputs": [{"links": [i]}]}
                  for i, (kind, props) in enumerate(tasks, 1)],
        "links": [[i, i, 0, i + 1, 0, -1] for i in range(1, len(tasks))],
    }


async def test_native_column_aspirate_and_dispense_enter_first_three_columns_between_racks():
    robot = _fixture_bravo()
    events = []
    executor = WorkflowExecutor.for_simulation(
        robot, _graph(),
        deck_config={
            "1": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "full"}],
            "2": [{"labware_id": PLATE_ID}],
            "3": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "empty"}],
        },
        runtime_state={"positions": {"X": 195.98, "Y": 10.18, "Z": 0, "W": 0, "G": 0, "Zg": -20}},
        on_event=events.append,
    )
    try:
        await asyncio.wait_for(executor.execute(), timeout=90)
        assert events[-1]["type"] == "workflow:complete", events[-1]
        report = events[-1]["physical_simulation"]
        assert report["status"] == "checked" and report["last_error"] is None
        assert report["samples_checked"] > report["moves_checked"] > 24
        assert report["qualification_granted"] is False

        positions = {}
        tip_length = None
        lowered = {}
        for event in events:
            if event["type"] == "workflow:positions":
                positions = event["positions"]
            if event["type"] == "workflow:tips_change" and event["tips_on"]:
                tip_length = event["attached_tip_length_mm"]
                assert event["tips_on_head_mode"]["row_count"] == 16
                assert event["tips_on_head_mode"]["column_count"] == 1
            if event["type"] == "workflow:node_step" and event.get("step_name") == "lower_to_liquid":
                lowered[event["node_id"]] = positions.copy()

        assert tip_length == pytest.approx(19.9)
        assert set(lowered) == {3, 4, 5, 6, 7, 8}
        teach_z = robot._teachpoints.get_teachpoint(2, Axis.Z)
        rim_world_z = -teach_z + 14.33
        bottom_world_z = rim_world_z - 11.38
        teach_tip_length = robot.profile.head.teach_tip_length_mm
        for node_id, pose in lowered.items():
            column = (node_id - 3) // 2
            assert pose["X"] == pytest.approx(robot._teachpoints.get_teachpoint(2, Axis.X) - 2.25 + 4.5 * column)
            assert pose["Y"] == pytest.approx(robot._teachpoints.get_teachpoint(2, Axis.Y) - 2.25)
            assert pose["Zg"] == -20
            tip_world_z = teach_tip_length - pose["Z"] - tip_length
            assert tip_world_z - bottom_world_z == pytest.approx(1)
            assert rim_world_z - tip_world_z == pytest.approx(10.38)
        assert len(robot._fresh_tip_wells(1)) == 368
        assert len(robot._spent_tip_wells(3)) == 16
        assert not robot._tips_on_head
    finally:
        robot.disconnect()
