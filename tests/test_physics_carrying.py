"""Carried-plate geometry uses active task state without changing the deck."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pybravo.physics.carrying import active_grasp_location, carried_plate_poses
from pybravo.physics.errors import PhysicalSimulationError
from pybravo.types import Axis, HeadType


class _Stack:
    def __init__(self, plates, support=0.0):
        self.plates = list(plates)  # top-first mounted group
        self.support = support

    def mounted_group_from_top(self):
        return list(self.plates)

    def get_support_height_below_group(self):
        return self.support


class _Task:
    def __init__(self, group, *, head=HeadType.HT_96_D_200, support=0.0):
        self.name = "PickPlace_1_3"
        self._from_location = 1
        self._to_location = 3
        self._source_labware = group[0]
        self._engage_plate = group[-1]
        self._profile = SimpleNamespace(
            head=SimpleNamespace(head_type=head, teach_tip_length_mm=55.2),
            gripper=SimpleNamespace(pad_zg_reference_mm=7.0, pad_reference_tip_length_mm=26.1),
        )
        self._tp = SimpleNamespace(get_teachpoint=lambda location, axis: {
            Axis.X: 100.0, Axis.Y: 200.0, Axis.Z: 30.0,
        }[axis])
        self._plate_pick_verified = False
        self._force_continue_after_pickup_failure = False
        self._current_step_name = "grip_plate"
        self._live_status = {"step": "grip_plate"}

    def _tip_length_for_pick_place(self):
        return self._profile.head.teach_tip_length_mm

    def _gripper_y_offset(self):
        return 1.5


def _fixture(*, mounted=False, head=HeadType.HT_96_D_200):
    bottom = SimpleNamespace(name="bottom", gripper_offset=2.0)
    group = [SimpleNamespace(name="top", gripper_offset=1.0), bottom] if mounted else [bottom]
    task = _Task(group, head=head)
    stack = _Stack(group)
    deck = SimpleNamespace(get_stack=lambda location: stack)
    bravo = SimpleNamespace(_engine=SimpleNamespace(current_task=task), _deck=deck)
    return bravo, task, stack, group


def test_verified_carry_translates_original_bounds_and_survives_deck_removal():
    bravo, task, stack, group = _fixture(mounted=True)
    assert active_grasp_location(bravo) == 1  # also snapshots the mounted group
    assert carried_plate_poses(bravo, {}) == {}

    task._plate_pick_verified = True
    task._current_step_name = "move_xy_to_place"
    task._live_status["step"] = "move_xy_to_place"
    # 96LT calibration: pad_ref = 7 + (55.2 - 26.1) = 36.1 mm.
    # Pick baseline Z=0, Zg=64.1; lowering Zg to 54.1 raises the plate 10 mm.
    pose = {Axis.X: 120.0, Axis.Y: 204.5, Axis.Z: 0.0, Axis.Zg: 54.1}
    expected = (20.0, 3.0, 10.0)
    assert carried_plate_poses(bravo, pose) == {
        id(group[0]): pytest.approx(expected),
        id(group[1]): pytest.approx(expected),
    }

    stack.plates.clear()  # Simulate a later source-deck update.
    assert set(carried_plate_poses(bravo, pose)) == {id(group[0]), id(group[1])}
    assert active_grasp_location(bravo) is None  # transit has no deck contact allowance


def test_grasp_location_and_release_boundary_are_narrow():
    bravo, task, _, group = _fixture()
    assert active_grasp_location(bravo) == 1
    task._plate_pick_verified = True
    task._current_step_name = "move_to_place_height"
    assert active_grasp_location(bravo) == 3
    task._current_step_name = "release_plate"
    task._live_status["step"] = "release_plate"
    assert active_grasp_location(bravo) == 3
    assert id(group[0]) in carried_plate_poses(bravo, {
        Axis.X: 100.0, Axis.Y: 201.5, Axis.Z: 0.0, Axis.Zg: 64.1,
    })
    task._live_status["step"] = "release_plate_complete"
    assert carried_plate_poses(bravo, {}) == {}
    assert active_grasp_location(bravo) is None
    task._current_step_name = "return_gripper_to_nesting"
    assert carried_plate_poses(bravo, {}) == {}


def test_forced_unverified_pickup_fails_closed():
    bravo, task, _, _ = _fixture()
    task._force_continue_after_pickup_failure = True
    task._plate_pick_verified = True
    task._current_step_name = "move_to_carry_height"
    with pytest.raises(PhysicalSimulationError, match="unverified plate pickup"):
        carried_plate_poses(bravo, {})
    with pytest.raises(PhysicalSimulationError, match="unverified plate pickup"):
        active_grasp_location(bravo)

    task._force_continue_after_pickup_failure = False
    task._live_status["pickup_verification"] = {"forced_continue": True}
    with pytest.raises(PhysicalSimulationError, match="unverified plate pickup"):
        carried_plate_poses(bravo, {})


def test_fixed_head_uses_its_pick_place_gripper_plane():
    bravo, task, _, group = _fixture(head=HeadType.HT_96_F_50)
    task._profile.head.teach_tip_length_mm = 26.1
    task._plate_pick_verified = True
    task._current_step_name = "move_to_carry_height"
    reference = 26.1 - 18.7452 - 0.79 + 0.7
    pick_zg = 30.0 + reference - group[0].gripper_offset
    poses = carried_plate_poses(bravo, {
        Axis.X: 100.0, Axis.Y: 201.5, Axis.Z: 0.0, Axis.Zg: pick_zg - 8.0,
    })
    assert poses[id(group[0])] == pytest.approx((0.0, 0.0, 8.0))


def test_unrelated_task_has_no_carried_plate():
    bravo, task, _, _ = _fixture()
    task.name = "GripperTeachMove_1"
    assert carried_plate_poses(bravo, {}) == {}
    assert active_grasp_location(bravo) is None
