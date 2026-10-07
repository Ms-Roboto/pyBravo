"""Read-only carried-labware poses for sampled PickPlace controller motion.

The returned offsets translate each carried labware's *original* deck bounds.
This module never changes the deck, task, controller, or collision scene.
"""

from __future__ import annotations

import math
import weakref
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, NoReturn

from pybravo.state_machine.tasks import _LENGTH_DIFFERENCE_96_TO_384
from pybravo.types import (
    GRIPPER_THICKNESS,
    GRIPPER_TO_BASE_OF_HEAD_GAP,
    HEIGHT_DIFF_96AM_TO_96LT,
    Axis,
)


@dataclass(frozen=True)
class _SourceSnapshot:
    labware: tuple[Any, ...]
    pickup_x_mm: float
    pickup_y_mm: float
    engage_base_z_mm: float
    engage_offset_mm: float
    gripper_plane_reference_mm: float


_snapshots: weakref.WeakKeyDictionary[Any, _SourceSnapshot] = weakref.WeakKeyDictionary()
_snapshot_lock = RLock()
_CARRY_STEPS = frozenset({
    "move_to_carry_height", "move_xy_to_place", "move_to_place_height", "release_plate",
})


def _fail(message: str) -> NoReturn:
    from .errors import PhysicalSimulationError

    raise PhysicalSimulationError(message)


def _pick_place_task(bravo: Any) -> Any | None:
    engine = getattr(bravo, "_engine", None)
    task = getattr(engine, "current_task", None)
    if task is None:
        task = getattr(engine, "_current_task", None)
    # Stack and Destack both execute a PickPlaceTask. Teach moves, Delid, and
    # Relid use different task names and have different carried geometry.
    if task is None or not str(getattr(task, "name", "")).startswith("PickPlace_"):
        return None
    return task


def _check_forced_continuation(task: Any) -> None:
    verification = (getattr(task, "_live_status", None) or {}).get("pickup_verification") or {}
    if getattr(task, "_force_continue_after_pickup_failure", False) or verification.get("forced_continue"):
        _fail("Physical carrying cannot be rehearsed after an unverified plate pickup was forced to continue")


def _plane_reference_mm(task: Any) -> float:
    profile = task._profile
    head = profile.head.head_type
    tip_length_fn = getattr(task, "_tip_length_for_pick_place", None)
    try:
        tip_length = (
            float(tip_length_fn()) if callable(tip_length_fn)
            else float(profile.head.teach_tip_length_mm)
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        _fail(f"PickPlace teach-tip length is unavailable: {exc}")
    if not math.isfinite(tip_length) or tip_length <= 0:
        _fail("PickPlace teach-tip length must be positive and finite")
    if head.is_disposable:
        gripper = profile.gripper
        reference = (
            float(gripper.pad_zg_reference_mm)
            + tip_length - float(gripper.pad_reference_tip_length_mm)
        )
    else:
        # Mirrors PickPlaceTask._solve_pick_or_place for fixed and AssayMAP
        # heads; the disposable branch uses the paired profile calibration.
        reference = (
            tip_length - GRIPPER_THICKNESS - GRIPPER_TO_BASE_OF_HEAD_GAP
            + _LENGTH_DIFFERENCE_96_TO_384
        )
        if head.is_assaymap:
            reference += HEIGHT_DIFF_96AM_TO_96LT
    if not math.isfinite(reference):
        _fail("PickPlace gripper-plane calibration is nonfinite")
    return reference


def _snapshot_for(task: Any, bravo: Any) -> _SourceSnapshot:
    with _snapshot_lock:
        cached = _snapshots.get(task)
        if cached is not None:
            return cached
        source = int(task._from_location)
        stack = bravo._deck.get_stack(source)
        group = tuple(stack.mounted_group_from_top())
        if not group or group[0] is not task._source_labware or group[-1] is not task._engage_plate:
            _fail("PickPlace source group is unavailable for carried-labware geometry")
        support_height = float(stack.get_support_height_below_group())
        tp = task._tp
        x = float(tp.get_teachpoint(source, Axis.X))
        y = float(tp.get_teachpoint(source, Axis.Y)) + float(task._gripper_y_offset())
        z = -float(tp.get_teachpoint(source, Axis.Z)) + support_height
        engage_offset = float(task._engage_plate.gripper_offset)
        plane_reference = _plane_reference_mm(task)
        if not all(math.isfinite(value) for value in (x, y, z, engage_offset)):
            _fail("PickPlace source geometry is nonfinite")
        snapshot = _SourceSnapshot(
            labware=group,
            pickup_x_mm=x,
            pickup_y_mm=y,
            engage_base_z_mm=z,
            engage_offset_mm=engage_offset,
            gripper_plane_reference_mm=plane_reference,
        )
        _snapshots[task] = snapshot
        return snapshot


def _released(task: Any) -> bool:
    return (
        getattr(task, "_current_step_name", None) == "release_plate"
        and (getattr(task, "_live_status", None) or {}).get("step") == "release_plate_complete"
    )


def carried_plate_poses(
    bravo: Any, pose: Mapping[Axis, float],
) -> dict[int, tuple[float, float, float]]:
    """Translate each carried item's original deck bounds at a sampled pose.

    Returns an empty map before verified grip and after release. The source
    group is cached by task identity, preserving mounted-group membership if
    the deck is later updated. A forced, unverified pickup fails closed.
    """
    task = _pick_place_task(bravo)
    if task is None:
        return {}
    _check_forced_continuation(task)
    step = getattr(task, "_current_step_name", None)
    if step not in _CARRY_STEPS or _released(task) or not getattr(task, "_plate_pick_verified", False):
        return {}
    source = _snapshot_for(task, bravo)
    try:
        x, y, z, zg = (float(pose[axis]) for axis in (Axis.X, Axis.Y, Axis.Z, Axis.Zg))
    except (KeyError, TypeError, ValueError) as exc:
        _fail(f"PickPlace sampled pose is missing a finite axis: {exc}")
    if not all(math.isfinite(value) for value in (x, y, z, zg)):
        _fail("PickPlace sampled pose contains a nonfinite axis")
    current_base_z = source.gripper_plane_reference_mm - z - zg - source.engage_offset_mm
    displacement = (
        x - source.pickup_x_mm,
        y - source.pickup_y_mm,
        current_base_z - source.engage_base_z_mm,
    )
    return {id(item): displacement for item in source.labware}


def active_grasp_location(bravo: Any) -> int | None:
    """Return only the source/destination with intentional grasp/support contact.

    The location is a stage hint, not blanket permission to ignore collision
    with other labware or neighboring deck positions.
    """
    task = _pick_place_task(bravo)
    if task is None:
        return None
    _check_forced_continuation(task)
    step = getattr(task, "_current_step_name", None)
    if step in {"move_to_pick_height", "grip_plate", "move_to_carry_height"}:
        _snapshot_for(task, bravo)
        return int(task._from_location)
    if step in {"move_to_place_height", "release_plate"} and not _released(task):
        _snapshot_for(task, bravo)
        return int(task._to_location)
    return None


__all__ = ["active_grasp_location", "carried_plate_poses"]
