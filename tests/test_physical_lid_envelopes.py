"""Manufacturer lid envelopes participate in actual sampled collision checks."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.deck.labware import Labware, synthesize_lid_labware
from pybravo.head_mode import normalize_head_mode
from pybravo.physics.scene import BravoCollisionScene, PhysicalSimulationError
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis
from pybravo.workflow.executor import WorkflowExecutor

ROOT = Path(__file__).resolve().parents[1]
PLATE_ID = "lw-34358f93e2a0"
RACK_ID = "lw-4914769d0af7"


def _bravo():
    return Bravo(profile=BravoProfile.load(ROOT / "profiles/simulation.yaml"), mode="simulation")


def _cellvis(bravo, *, covered=True):
    definition = bravo.labware_catalog.get_definition(PLATE_ID)
    assert definition is not None and definition.lid_geometry is not None
    return Labware.from_definition(definition, is_lidded=covered)


@pytest.fixture
def covered_scene():
    bravo = _bravo()
    plate = _cellvis(bravo)
    bravo._deck.add(2, plate)
    scene = BravoCollisionScene(bravo, sample_spacing_mm=1.0)
    try:
        yield bravo, plate, scene
    finally:
        scene.close()


def _pose(bravo, z):
    return {
        Axis.X: bravo._teachpoints.get_teachpoint(2, Axis.X) - 2.25,
        Axis.Y: bravo._teachpoints.get_teachpoint(2, Axis.Y) - 2.25,
        Axis.Z: z, Axis.Zg: -20.0, Axis.G: 0.0, Axis.W: 0.0,
    }


def _mount_single_tip(bravo):
    bravo._tips_on_head = True
    bravo._attached_tip_length_mm = 19.9
    bravo._tip_definition_id = "st_10ul"
    bravo._tips_on_head_mode = normalize_head_mode(bravo.profile.head.head_type, "single_barrel", "back_left")


def test_known_cover_has_separate_plate_body_and_dimensioned_lid(covered_scene):
    bravo, plate, scene = covered_scene
    body = scene._known_labware[id(plate)]
    lid = scene._lid_obstacles[id(plate)]
    assert body.upper_mm - body.lower_mm == pytest.approx([127.6, 85.6, 14.33])
    assert lid.upper_mm - lid.lower_mm == pytest.approx([127.15, 85.05, 10])
    assert lid.lower_mm[2] - body.lower_mm[2] == pytest.approx(6.8)
    assert lid.upper_mm[2] - body.lower_mm[2] == pytest.approx(16.8)
    assert (lid.lower_mm[:2] + lid.upper_mm[:2]) / 2 == pytest.approx(
        (body.lower_mm[:2] + body.upper_mm[:2]) / 2
    )
    # A safe motion over a covered neighbor is now actually sampled.
    scene.set_context("travel", "motion/Move", {})
    scene.check_motion(_pose(bravo, 0), _pose(bravo, 0))
    assert scene.report()["moves_checked"] == 1


@pytest.mark.parametrize("operation", ["liquid/Aspirate", "liquid/Dispense", "liquid/Mix"])
def test_covered_well_access_has_dimensioned_evidence_without_fabricated_pose(covered_scene, operation):
    _, plate, scene = covered_scene
    with pytest.raises(PhysicalSimulationError, match="remove the lid") as caught:
        scene.set_context("liquid", operation, {"location": 2})
    error = caught.value.details
    assert error["kind"] == "lid_access_blocked"
    assert error["reason"] == "well_access_blocked"
    assert error["geometry"] == "manufacturer_exterior_envelope"
    assert error["labware_definition_id"] == PLATE_ID
    assert error["labware_id"] == plate.id
    assert error["location"] == 2
    assert error["lid_geometry"]["source"]
    assert not {"pose", "bodies", "penetration_mm", "tip_segment_mm"} & error.keys()
    assert scene.samples_checked == scene.moves_checked == 0


def test_sampled_tip_descent_hits_lid_with_pose_and_segment_evidence(covered_scene):
    bravo, plate, scene = covered_scene
    _mount_single_tip(bravo)
    lid = scene._lid_obstacles[id(plate)]
    scene.set_context("down", "motion/Move", {"location": 2})
    crossing_z = scene.teach_length - bravo._attached_tip_length_mm - lid.upper_mm[2]
    start, end = _pose(bravo, crossing_z - 2), _pose(bravo, crossing_z + 2)
    with pytest.raises(PhysicalSimulationError, match="tip intersects the lid envelope") as caught:
        scene.check_motion(start, end)
    error = caught.value.details
    assert error["kind"] == "lid_collision"
    assert error["location"] == 2 and error["stack_index"] == 0
    assert start[Axis.Z] < error["pose"]["Z"] < end[Axis.Z]
    assert scene.samples_checked > 1
    assert error["tip"] == [0, 0]
    assert error["tip_segment_mm"][0][2] < error["upper_mm"][2]
    assert error["tip_segment_mm"][1][2] > error["upper_mm"][2]
    assert error["runtime_state"]["attached_tip_length_mm"] == 19.9
    assert error["runtime_state"]["teachpoints"]["2"]["Z"] == bravo._teachpoints.get_teachpoint(2, Axis.Z)


def test_tip_shaft_cannot_skip_cover_after_tip_end_has_crossed_below_it(covered_scene):
    bravo, plate, scene = covered_scene
    _mount_single_tip(bravo)
    lid = scene._lid_obstacles[id(plate)]
    scene.set_context("transit", "motion/Move", {})
    tip_below_cover = lid.lower_mm[2] - 1
    pose = _pose(bravo, scene.teach_length - bravo._attached_tip_length_mm - tip_below_cover)
    with pytest.raises(PhysicalSimulationError, match="tip intersects the lid envelope") as caught:
        scene._check_tips(pose)
    assert caught.value.details["tip_segment_mm"][0][2] < lid.lower_mm[2]
    assert caught.value.details["tip_segment_mm"][1][2] > lid.upper_mm[2]
    # The exact same axis above the lid is clear. A head with no mounted tips
    # does not inherit a fictitious tip collision.
    scene._check_tips(_pose(bravo, 0))
    bravo._tips_on_head = False
    scene._check_tips(pose)


def test_sampled_tool_mesh_strikes_lid_envelope_without_tips(covered_scene):
    bravo, plate, scene = covered_scene
    lid = scene._lid_obstacles[id(plate)]
    scene.set_context("neighbor", "motion/Move", {})
    end_z = scene.teach_length - lid.upper_mm[2] + 1
    with pytest.raises(PhysicalSimulationError, match="Physical collision") as caught:
        scene.check_motion(_pose(bravo, 0), _pose(bravo, end_z))
    error = caught.value.details
    assert error["kind"] == "lid_collision"
    assert error["penetration_mm"] > 0
    assert any(body.startswith("robot/") for body in error["bodies"])
    assert error["runtime_state"]["tips_on_head"] is False
    assert scene.samples_checked > 0 and scene.contact_queries > 0


def test_cover_mutation_does_not_silently_remove_collision_geometry(covered_scene):
    _, plate, scene = covered_scene
    plate.is_lidded = False
    with pytest.raises(PhysicalSimulationError, match="cover state changed"):
        scene.set_context("unsafe_edit", "motion/Move", {})


@pytest.mark.parametrize("change", ["missing", "invalid", "sealed"])
def test_unknown_or_sealed_cover_still_fails_before_sampling(change):
    bravo = _bravo()
    plate = _cellvis(bravo)
    if change == "missing":
        plate.metadata.pop("lid_geometry")
    elif change == "invalid":
        plate.metadata["lid_geometry"]["height_mm"] = float("nan")
    else:
        plate.is_sealed = True
    bravo._deck.add(2, plate)
    with pytest.raises(PhysicalSimulationError) as caught:
        BravoCollisionScene(bravo)
    assert caught.value.details["kind"] == "covered_labware"
    assert "pose" not in caught.value.details


def test_known_lidded_plate_and_lid_follow_the_same_deck_move(covered_scene):
    bravo, plate, scene = covered_scene
    body = scene._known_labware[id(plate)]
    lid = scene._lid_obstacles[id(plate)]
    original_body = body.lower_mm.copy()
    original_lid = lid.lower_mm.copy()
    bravo._deck.get_stack(2).remove_top()
    bravo._deck.add(5, plate)
    scene.set_context("moved", "motion/Move", {})
    assert lid.location == body.location == 5
    assert lid.lower_mm - original_lid == pytest.approx(body.lower_mm - original_body)
    assert lid.upper_mm - lid.lower_mm == pytest.approx([127.15, 85.05, 10])


def test_preloaded_standalone_lid_requires_a_recorded_placement_datum():
    bravo = _bravo()
    bravo._deck.add(2, synthesize_lid_labware(_cellvis(bravo)))
    with pytest.raises(PhysicalSimulationError, match="standalone lid") as caught:
        BravoCollisionScene(bravo)
    assert caught.value.details["kind"] == "unsupported_lid_operation"
    assert caught.value.details["operation"] == "setup/standalone_lid"


def test_diagnostic_tip_inventory_preserves_removed_and_empty_rack_cells_without_mutation():
    bravo = _bravo()
    bravo.set_labware(1, RACK_ID, tip_definition_id="st_10ul", tipbox_fill_state="full")
    bravo.set_labware(3, RACK_ID, tip_definition_id="st_10ul", tipbox_fill_state="empty")
    bravo._tipbox_occupancy[1].remove((0, 23))
    original = deepcopy(bravo._tipbox_occupancy)
    scene = BravoCollisionScene.__new__(BravoCollisionScene)
    scene.bravo = bravo
    scene.teach_length = bravo.profile.head.teach_tip_length_mm
    state = scene._diagnostic_runtime_state()
    assert state["tipbox_removed_cells"]["1"] == ["0:23"]
    assert len(state["tipbox_removed_cells"]["3"]) == 384
    assert bravo._tipbox_occupancy == original


def _workflow():
    tasks = [
        ("flow/Start", {}),
        ("tips/TipsOn", {
            "location": 1,
            "head_mode": {"subset_type": "column", "subset_config": "back_left", "column_count": 1},
            "tip_anchor_row": 0, "tip_anchor_col": 23,
        }),
        ("liquid/Aspirate", {"location": 2, "anchor": "A1", "volume": 10, "distance_from_bottom": 1}),
        ("liquid/Dispense", {"location": 2, "anchor": "A1", "volume": 10, "distance_from_bottom": 1}),
        ("tips/TipsOff", {"location": 3, "tip_anchor_row": 0, "tip_anchor_col": 0}),
        ("flow/End", {}),
    ]
    return {
        "nodes": [{"id": i, "type": kind, "properties": props, "outputs": [{"links": [i]}]}
                  for i, (kind, props) in enumerate(tasks, 1)],
        "links": [[i, i, 0, i + 1, 0, -1] for i in range(1, len(tasks))],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("covered", [True, False])
async def test_native_workflow_checks_tips_then_rejects_covered_plate_or_enters_open_wells(covered):
    bravo = _bravo()
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    events = []
    deck = {
        "1": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "full"}],
        "2": [{"labware_id": PLATE_ID, "is_lidded": covered}],
        "3": [{"labware_id": RACK_ID, "tip_definition_id": "st_10ul", "tipbox_fill_state": "empty"}],
    }
    executor = WorkflowExecutor.for_simulation(
        bravo, _workflow(), deck_config=deepcopy(deck), on_event=events.append,
        runtime_state={"positions": {"X": 195.98, "Y": 10.18, "Z": 0, "W": 0, "G": 0, "Zg": -20}},
    )
    try:
        await asyncio.wait_for(executor.execute(), 90)
        report = events[-1]["physical_simulation"]
        assert report["samples_checked"] > report["moves_checked"] > 0
        if covered:
            assert events[-1]["type"] == "workflow:error"
            error = report["last_error"]
            assert error["kind"] == "lid_access_blocked"
            assert error["node_id"] == 3 and error["node_type"] == "liquid/Aspirate"
            assert "pose" not in error
            assert any(event["type"] == "workflow:tips_change" and event["tips_on"] for event in events)
        else:
            assert events[-1]["type"] == "workflow:complete", report
            assert report["status"] == "checked"
            assert any(event["type"] == "workflow:node_step" and event.get("step_name") == "lower_to_liquid"
                       for event in events)
        assert not report["qualification_granted"]
    finally:
        bravo.disconnect()
