"""Physical collision rehearsal against the real Bravo URDF and catalog dimensions."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.controllers.base import AxisMoveInfo
from pybravo.controllers.simulation import SimulationController
from pybravo.deck.labware import Labware
from pybravo.physics.runtime import CollisionRehearsal
from pybravo.physics.scene import BravoCollisionScene, PhysicalSimulationError
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis
from pybravo.workflow.executor import WorkflowExecutor

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
PP_384_GEOMETRY = {
    "base_class": "microplate",
    "rows": 16,
    "cols": 24,
    "spacing_x_mm": 4.5,
    "spacing_y_mm": 4.5,
    "offset_x_mm": 2.25,
    "offset_y_mm": 2.25,
    "well_depth_mm": 11.4,
    "well_diameter_mm": 3.5,
}


def _pp_plate(name: str) -> Labware:
    # Labcyte PP0200 dimensions in the checked-in labware catalog.
    return Labware(
        id=name,
        name=name,
        height=14.4,
        width=85.48,
        length=127.76,
        gripper_offset=7.0,
        stack_height=13.6,
        wells=384,
        metadata=dict(PP_384_GEOMETRY),
    )


def _raise_task_error(error) -> None:
    """Surface native step failures immediately instead of awaiting operator input."""
    raise error.original_exception


@pytest.fixture(scope="module")
def physical_scene():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    bravo._deck.add(1, _pp_plate("source"))
    for index in range(4):
        bravo._deck.add(2, _pp_plate(f"stack_{index}"))
    scene = BravoCollisionScene(bravo, sample_spacing_mm=1.0)
    try:
        yield bravo, scene
    finally:
        scene.close()


def _pose(bravo: Bravo, slot: int, *, z: float) -> dict[Axis, float]:
    pose = {axis: 0.0 for axis in Axis}
    pose.update(
        {
            Axis.X: bravo._teachpoints.get_teachpoint(slot, Axis.X),
            Axis.Y: bravo._teachpoints.get_teachpoint(1, Axis.Y),
            Axis.Z: z,
            Axis.Zg: -20.0,
        }
    )
    return pose


def test_low_lateral_sweep_rejects_midpath_stack_with_clear_endpoints(physical_scene):
    bravo, scene = physical_scene
    scene.set_context("sweep", "motion/Move", {})
    start = _pose(bravo, 1, z=115.0)
    end = _pose(bravo, 3, z=115.0)

    scene.check_motion(start, start)
    scene.check_motion(end, end)
    with pytest.raises(PhysicalSimulationError, match="Physical collision") as caught:
        scene.check_motion(start, end)
    details = caught.value.details
    assert any("labware/2/" in body for body in details["bodies"])
    assert any("robot/384_head" in body for body in details["bodies"])
    assert start[Axis.X] < details["pose"]["X"] < end[Axis.X]
    assert details["penetration_mm"] > 0


def test_retracted_lateral_sweep_clears_same_stack(physical_scene):
    bravo, scene = physical_scene
    scene.set_context("safe_sweep", "motion/Move", {})
    before = scene.moves_checked
    scene.check_motion(_pose(bravo, 1, z=0.0), _pose(bravo, 3, z=0.0))
    assert scene.moves_checked == before + 1


def test_endpoint_collision_is_checked(physical_scene):
    bravo, scene = physical_scene
    scene.set_context("endpoint", "motion/Move", {})
    unsafe = _pose(bravo, 2, z=115.0)
    with pytest.raises(PhysicalSimulationError, match="Physical collision"):
        scene.check_motion(unsafe, unsafe)


def test_homing_zero_exception_does_not_allow_ordinary_out_of_range_motion(physical_scene):
    bravo, scene = physical_scene
    start = _pose(bravo, 1, z=0.0)
    start[Axis.Y] = 5.0
    zero = {**start, Axis.Y: 0.0}
    scene.set_context("ordinary_zero", "motion/Move", {})
    with pytest.raises(PhysicalSimulationError, match="outside physical travel"):
        scene.check_motion(start, zero)

    scene.set_context("homing_beyond_zero", "system/Home", {"axes": ["Y"]})
    with pytest.raises(PhysicalSimulationError, match="outside physical travel"):
        scene.check_motion(start, {**zero, Axis.Y: -0.1})


def test_calibrated_well_entry_rejects_wrong_xy_and_bottom(physical_scene):
    bravo, scene = physical_scene
    scene.set_context("aspirate", "liquid/Aspirate", {"location": 1})
    bravo._tips_on_head = True
    bravo._attached_tip_length_mm = 26.1
    bravo._tips_on_head_mode = bravo._head_mode
    aligned = _pose(bravo, 1, z=130.0)
    aligned[Axis.X] -= 2.25
    aligned[Axis.Y] -= 2.25
    try:
        scene.check_motion(aligned, aligned)

        off_axis = {**aligned, Axis.X: aligned[Axis.X] + 1.8}
        with pytest.raises(PhysicalSimulationError, match="not centered inside a recorded well"):
            scene.check_motion(off_axis, off_axis)

        below_bottom = {**aligned, Axis.Z: 138.0}
        with pytest.raises(PhysicalSimulationError, match="well bottom"):
            scene.check_motion(below_bottom, below_bottom)
    finally:
        bravo._tips_on_head = False
        bravo._attached_tip_length_mm = None
        bravo._tips_on_head_mode = None


def test_simulation_controller_rejects_entire_colliding_move_atomically(physical_scene):
    bravo, scene = physical_scene
    scene.set_context("atomic", "motion/Move", {})
    controller = SimulationController(bravo.profile.head.head_type)
    controller.set_move_timing_enabled(False)
    start = _pose(bravo, 1, z=115.0)
    controller.move([AxisMoveInfo(axis, value) for axis, value in start.items()], wait=False)
    before = {axis: controller.get_position(axis) for axis in Axis}
    controller.set_motion_guard(scene.check_motion)

    with pytest.raises(PhysicalSimulationError, match="Physical collision"):
        controller.move(
            [
                AxisMoveInfo(Axis.X, _pose(bravo, 3, z=115.0)[Axis.X]),
                AxisMoveInfo(Axis.W, 10.0),
            ],
            wait=False,
        )
    assert {axis: controller.get_position(axis) for axis in Axis} == before


def test_gripper_finger_travel_matches_reflected_urdf_joint_axes(physical_scene):
    _bravo, scene = physical_scene
    docked = {axis: 0.0 for axis in Axis}
    moved = {**docked, Axis.G: 10.0}
    left = "robot/fingerleft_fingerleft"
    right = "robot/fingerright_fingerright"

    left_delta = scene._shift(scene.tool_axes[left], moved) - scene._shift(scene.tool_axes[left], docked)
    right_delta = scene._shift(scene.tool_axes[right], moved) - scene._shift(scene.tool_axes[right], docked)
    assert left_delta[1] == pytest.approx(-5.0)
    assert right_delta[1] == pytest.approx(5.0)


class _VerifiedCarryTask:
    """The native task state needed by the read-only carried-geometry provider."""

    name = "PickPlace_1_3"
    _from_location = 1
    _to_location = 3
    _plate_pick_verified = True
    _force_continue_after_pickup_failure = False
    _current_step_name = "move_xy_to_place"

    def __init__(self, bravo: Bravo):
        source = bravo._deck.get_stack(1).top
        self._source_labware = source
        self._engage_plate = source
        self._profile = bravo.profile
        self._tp = bravo._teachpoints
        self._live_status = {"step": "move_xy_to_place"}

    def _tip_length_for_pick_place(self):
        return self._profile.head.teach_tip_length_mm

    def _gripper_y_offset(self):
        # The 384-head PickPlaceTask's documented gripper Y compensation.
        return -2.25


def _carry_pose(bravo: Bravo, slot: int, *, zg: float) -> dict[Axis, float]:
    pose = _pose(bravo, slot, z=0.0)
    pose[Axis.Y] = bravo._teachpoints.get_teachpoint(slot, Axis.Y) - 2.25
    pose[Axis.Zg] = zg
    pose[Axis.G] = 9.0
    return pose


def test_carried_plate_hits_neighbor_stack_at_low_carry_height(physical_scene):
    bravo, scene = physical_scene
    task = _VerifiedCarryTask(bravo)
    bravo._engine._current_task = task
    try:
        scene.set_context("low_carry", "plate/PickPlace", {"from_location": 1, "to_location": 3})
        start = _carry_pose(bravo, 1, zg=100.0)
        end = _carry_pose(bravo, 3, zg=100.0)
        scene.check_motion(start, start)
        scene.check_motion(end, end)
        with pytest.raises(PhysicalSimulationError, match="Physical collision") as caught:
            scene.check_motion(start, end)
        bodies = caught.value.details["bodies"]
        assert any(body.startswith("labware/1/0/source") for body in bodies)
        assert any(body.startswith("labware/2/") for body in bodies)
        assert start[Axis.X] < caught.value.details["pose"]["X"] < end[Axis.X]
    finally:
        bravo._engine._current_task = None


def test_carried_plate_clears_neighbor_at_sufficient_height(physical_scene):
    bravo, scene = physical_scene
    task = _VerifiedCarryTask(bravo)
    bravo._engine._current_task = task
    try:
        scene.set_context("high_carry", "plate/PickPlace", {"from_location": 1, "to_location": 3})
        before = scene.moves_checked
        scene.check_motion(_carry_pose(bravo, 1, zg=70.0), _carry_pose(bravo, 3, zg=70.0))
        assert scene.moves_checked == before + 1
    finally:
        bravo._engine._current_task = None


def test_stack_nesting_allows_only_aligned_catalog_seat():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    definition = bravo._labware_catalog.get_definition("01KD4PYY7N4EG0E22P3QP1RHB9")
    assert definition is not None
    source = Labware.from_definition(definition)
    base = Labware.from_definition(definition)
    bravo._deck.add(1, source)
    bravo._deck.add(3, base)
    scene = BravoCollisionScene(bravo, sample_spacing_mm=1.0)
    task = _VerifiedCarryTask(bravo)
    task._current_step_name = "move_to_place_height"
    task._live_status = {"step": "move_to_place_height"}
    bravo._engine._current_task = task
    try:
        scene.set_context("nesting", "plate/Stack", {"source_location": 1, "base_location": 3})
        destination_base_z = -bravo._teachpoints.get_teachpoint(3, Axis.Z)
        plane_reference = (
            bravo.profile.gripper.pad_zg_reference_mm
            + bravo.profile.head.teach_tip_length_mm
            - bravo.profile.gripper.pad_reference_tip_length_mm
        )
        seated_zg = plane_reference - source.gripper_offset - (
            destination_base_z + base.stack_height
        )
        seated = _carry_pose(bravo, 3, zg=100.0)
        seated[Axis.Z] = seated_zg - seated[Axis.Zg]
        scene.check_motion(seated, seated)

        shifted = {**seated, Axis.X: seated[Axis.X] + 1.0}
        with pytest.raises(PhysicalSimulationError, match="Physical collision"):
            scene.check_motion(shifted, shifted)

        overlowered = {**seated, Axis.Z: seated[Axis.Z] + 1.0}
        with pytest.raises(PhysicalSimulationError, match="Physical collision"):
            scene.check_motion(overlowered, overlowered)
    finally:
        bravo._engine._current_task = None
        scene.close()


def test_provenance_identifies_robot_profile_and_deck_snapshot(physical_scene):
    _bravo, scene = physical_scene
    report = scene.report()
    assert report["engine"] == "SuperDex"
    assert "carried_plate_envelopes" in report["scope"]
    assert report["qualification_granted"] is False
    provenance = report["provenance"]
    assert set(provenance) >= {"robot_assets_sha256", "profile_sha256", "deck_sha256"}
    assert all(len(provenance[key]) == 64 for key in ("robot_assets_sha256", "profile_sha256", "deck_sha256"))


def test_unmodeled_labware_added_mid_rehearsal_fails_closed(physical_scene):
    bravo, scene = physical_scene
    bravo._deck.add(3, _pp_plate("unexpected"))
    try:
        with pytest.raises(PhysicalSimulationError, match="Unmodeled labware"):
            scene.set_context("new_plate", "motion/Move", {})
    finally:
        bravo._deck.remove(3)


def test_catalog_geometry_metadata_cannot_drift_behind_provenance(physical_scene):
    bravo, scene = physical_scene
    source = bravo._deck.get_stack(1).top
    original = source.metadata["well_depth_mm"]
    source.metadata["well_depth_mm"] = original + 1.0
    try:
        with pytest.raises(PhysicalSimulationError, match="geometry|metadata|provenance"):
            scene.set_context("metadata_changed", "motion/Move", {})
    finally:
        source.metadata["well_depth_mm"] = original


def test_teachpoint_cannot_drift_behind_provenance(physical_scene):
    bravo, scene = physical_scene
    original = bravo._teachpoints.get_teachpoint(2, Axis.X)
    bravo._teachpoints.set_teachpoint(2, Axis.X, original + 1.0)
    try:
        with pytest.raises(PhysicalSimulationError, match="(?i)(teachpoint|profile|geometry|provenance)"):
            scene.set_context("teachpoint_changed", "motion/Move", {})
    finally:
        bravo._teachpoints.set_teachpoint(2, Axis.X, original)


def test_profile_calibration_cannot_drift_behind_provenance(physical_scene):
    bravo, scene = physical_scene
    original = bravo.profile.gripper.pad_zg_reference_mm
    bravo.profile.gripper.pad_zg_reference_mm = original + 1.0
    try:
        with pytest.raises(PhysicalSimulationError, match="profile|calibration|provenance"):
            scene.set_context("profile_changed", "motion/Move", {})
    finally:
        bravo.profile.gripper.pad_zg_reference_mm = original


def test_missing_accessory_collision_geometry_fails_before_rehearsal():
    profile = BravoProfile.load(PROFILE_PATH)
    profile.accessories.barcode_reader.enabled = True
    with pytest.raises(PhysicalSimulationError, match="accessories need collision geometry"):
        BravoCollisionScene(Bravo(profile=profile, mode="simulation"))


@pytest.mark.asyncio
@pytest.mark.parametrize("is_lidded,is_sealed", [(True, False), (False, True), (True, True)])
async def test_covered_labware_setup_evidence_reaches_workflow_error(is_lidded, is_sealed):
    """The initial setup diagram has catalog evidence, never a fictitious contact."""
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    definition_id = "01KD4PYY7N4EG0E22P3QP1RHB9"
    events = []
    graph = {
        "nodes": [
            {"id": 1, "type": "flow/Start", "properties": {}, "outputs": [{"links": [1]}]},
            {"id": 2, "type": "flow/End", "properties": {}},
        ],
        "links": [[1, 1, 0, 2, 0, -1]],
    }
    executor = WorkflowExecutor.for_simulation(
        bravo, graph, on_event=events.append,
        deck_config={"7": [
            {"labware_id": definition_id},
            {"labware_id": definition_id, "is_lidded": is_lidded, "is_sealed": is_sealed},
        ]},
    )
    try:
        await executor.execute()
        assert [event["type"] for event in events] == ["workflow:error"]
        report = events[-1]["physical_simulation"]
        assert report["status"] == "failed"
        assert report["moves_checked"] == report["samples_checked"] == report["contact_queries"] == 0
        assert report["qualification_granted"] is False
        error = report["last_error"]
        assert error["kind"] == "covered_labware"
        assert error["stage"] == "initialization"
        assert error["location"] == 7
        assert error["stack_index"] == 1
        assert error["is_lidded"] is is_lidded
        assert error["is_sealed"] is is_sealed
        plate = bravo._deck.get_stack(7).top
        assert error["labware_id"] == plate.id
        assert error["labware_definition_id"] == definition_id
        assert error["labware_name"] == plate.name
        assert error["body"] == f"labware/7/1/{plate.name}"
        assert error["geometry"] == "catalog_envelope"
        assert error["coordinate_frame"] == "machine_xyz_z_up_mm"
        lower, upper = error["lower_mm"], error["upper_mm"]
        assert all(float("-inf") < value < float("inf") for value in lower + upper)
        assert [high - low for low, high in zip(lower, upper)] == pytest.approx(
            [plate.length, plate.width, plate.height]
        )
        support = bravo._deck.get_stack(7).items[0].stack_height
        assert lower[2] == pytest.approx(-bravo._teachpoints.get_teachpoint(7, Axis.Z) + support)
        assert not {"pose", "bodies", "penetration_mm", "sample_index"} & error.keys()
        assert bravo.controller._motion_guard is None
    finally:
        bravo.disconnect()


@pytest.mark.parametrize("operation", ["plate/Delid", "plate/Relid"])
def test_unsupported_lid_operation_has_setup_identity_without_collision_evidence(operation):
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    plate = _pp_plate("lid operation target")
    bravo._deck.add(3, plate)
    scene = BravoCollisionScene(bravo)
    try:
        with pytest.raises(PhysicalSimulationError, match="Lid collision geometry") as caught:
            scene.set_context("lid-node", operation, {"location": 3})
        error = caught.value.details
        assert error["kind"] == "unsupported_lid_operation"
        assert error["operation"] == operation
        assert error["node_id"] == "lid-node"
        assert error["location"] == 3
        assert error["labware_id"] == plate.id
        assert error["labware_name"] == plate.name
        assert not {"pose", "bodies", "penetration_mm", "lower_mm", "upper_mm"} & error.keys()
        assert scene.moves_checked == scene.samples_checked == 0
    finally:
        scene.close()


@pytest.mark.asyncio
async def test_native_pick_place_rehearses_real_task_stages_and_moves_catalog_plate():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    definition = bravo._labware_catalog.get_definition("01KD4PYY7N4EG0E22P3QP1RHB9")
    assert definition is not None
    source = Labware.from_definition(definition)
    bravo._deck.add(1, source)
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    completed_steps = []
    bravo._engine.set_step_handler(lambda _task, step: completed_steps.append(step))
    runtime = CollisionRehearsal(bravo)
    try:
        await runtime.initialize()
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("native_pick", "plate/PickPlace", {"pick_location": 1, "place_location": 3})
        await bravo.pick_place(1, 3)

        assert bravo._deck.get_stack(1).top is None
        assert bravo._deck.get_stack(3).top is source
        assert "move_to_carry_height" in completed_steps
        assert "move_xy_to_place" in completed_steps
        assert "release_plate" in completed_steps
        report = runtime.report()
        assert report["status"] == "checked"
        assert report["moves_checked"] >= 10
        assert report["samples_checked"] > 0
        assert report["qualification_granted"] is False
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_native_stack_seats_catalog_plate_on_matching_plate():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    definition = bravo._labware_catalog.get_definition("01KD4PYY7N4EG0E22P3QP1RHB9")
    assert definition is not None
    source = Labware.from_definition(definition)
    base = Labware.from_definition(definition)
    bravo._deck.add(1, source)
    bravo._deck.add(3, base)
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    bravo._engine.set_error_handler(_raise_task_error)
    runtime = CollisionRehearsal(bravo)
    try:
        await runtime.initialize()
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("native_stack", "plate/Stack", {"source_location": 1, "base_location": 3})
        result = await bravo.stack_plates(base_location=3, source_location=1)

        assert result["status"] == "completed"
        assert bravo._deck.get_stack(1).top is None
        assert bravo._deck.get_stack(3).items == [base, source]
        assert runtime.report()["status"] == "checked"
        assert runtime.report()["moves_checked"] >= 10
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_native_destack_removes_top_catalog_plate_from_two_plate_stack():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    definition = bravo._labware_catalog.get_definition("01KD4PYY7N4EG0E22P3QP1RHB9")
    assert definition is not None
    base = Labware.from_definition(definition)
    top = Labware.from_definition(definition)
    bravo._deck.add(1, base)
    bravo._deck.add(1, top)
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    bravo._engine.set_error_handler(_raise_task_error)
    runtime = CollisionRehearsal(bravo)
    try:
        await runtime.initialize()
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("native_destack", "plate/Destack", {"source_location": 1, "destination_location": 3})
        result = await bravo.destack_plate(source_location=1, destination_location=3)

        assert result["status"] == "completed"
        assert result["remaining_stack_count"] == 1
        assert bravo._deck.get_stack(1).items == [base]
        assert bravo._deck.get_stack(3).top is top
        assert runtime.report()["status"] == "checked"
        assert runtime.report()["moves_checked"] >= 10
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_native_y_home_from_nonzero_position_reaches_recorded_zero():
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    bravo._engine.set_error_handler(_raise_task_error)
    bravo.controller.move([AxisMoveInfo(Axis.Y, 5.0)], wait=False)
    assert bravo.controller.get_position(Axis.Y) == pytest.approx(5.0)
    runtime = CollisionRehearsal(bravo)
    try:
        await runtime.initialize()
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("native_y_home", "system/Home", {"axes": ["Y"]})
        homed = await bravo.home(axes=[Axis.Y], force=True)

        assert homed == [Axis.Y]
        assert bravo.controller.get_position(Axis.Y) == pytest.approx(0.0)
        assert runtime.report()["status"] == "checked"
        assert runtime.report()["moves_checked"] >= 1
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


def test_native_runtime_initializes_and_exits_in_a_fresh_process():
    """The SDK's process-exit cleanup must not hang after worker scene disposal."""
    script = textwrap.dedent(
        """
        import asyncio
        from pybravo.bravo import Bravo
        from pybravo.physics.runtime import CollisionRehearsal
        from pybravo.profile.profile import BravoProfile

        async def main():
            bravo = Bravo(profile=BravoProfile.load("profiles/simulation.yaml"), mode="simulation")
            rehearsal = CollisionRehearsal(bravo)
            try:
                await rehearsal.initialize()
                assert rehearsal.report()["engine"] == "SuperDex"
            finally:
                await rehearsal.close()

        asyncio.run(main())
        print("native runtime exited", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROFILE_PATH.parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native runtime exited" in result.stdout
