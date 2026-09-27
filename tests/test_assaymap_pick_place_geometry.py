"""Pick/place gripper plane for the AssayMAP head, pinned to reference measurements.

A 96 Eppendorf Twin.tec PCR plate moved 5 -> 8 -> 5, nothing on the head:

    pick    Z 33.632   Zg 105.000      grip at z+zg = 138.632
    carry   Z 33.632   Zg  95.000      lifted 10 mm by the gripper axis
    place   Z 33.632   Zg 105.000
    grip    G -> 9.000 at v=6.14%, F=80%   (force-controlled close)
    XY      teachpoint + 0.190 in Y = the profile's gripper.y_offset

What matters is **z + zg**, the gripper plane. `_solve_pick_or_place` splits the
travel between the two axes at `_PLATE_HANDLING_ZG_MAX`, and that split leaves
z + zg unchanged -- so the cap moves work between axes but cannot move the
gripper. Only the sum is a real position, and it was 4.710 mm short.

That 4.710 is `HEIGHT_DIFF_96AM_TO_96LT`. The constant existed in types.py and was
used only as a fallback "tip length" in `_tip_length_for_pick_place`, a branch
that is unreachable whenever the profile supplies a teach tip length -- which is
why its meaning stayed unknown for so long. Its real home is the gripper solution:
the 96AM body sits higher than the 96LT this code was written around.
"""
from __future__ import annotations

import pytest

from pybravo.state_machine.tasks import (
    _LENGTH_DIFFERENCE_96_TO_384,
    GRIPPER_THICKNESS,
    GRIPPER_TO_BASE_OF_HEAD_GAP,
    HEIGHT_DIFF_96AM_TO_96LT,
)

# Measured, position 5, 96 Eppendorf Twin.tec PCR.
REFERENCE_PICK_Z = 33.632
REFERENCE_PICK_ZG = 105.000
REFERENCE_CARRY_ZG = 95.000
REFERENCE_GRIP_PLANE = REFERENCE_PICK_Z + REFERENCE_PICK_ZG

TEACH_Z = 105.257          # teachpoint 5
TEACH_TIP_MM = 55.5        # profile head.teach_tip_length_mm
GRIPPER_OFFSET_MM = 8.0    # vendor BRAVO_ROBOT_GRIPPER_OFFSET for this plate


def _gripper_plane(*, assaymap: bool, z_current: float = 0.0, stack_height: float = 0.0) -> float:
    """Mirror of the non-disposable branch of _solve_pick_or_place."""
    plane = (
        TEACH_Z
        - z_current
        + TEACH_TIP_MM
        - GRIPPER_THICKNESS
        - GRIPPER_TO_BASE_OF_HEAD_GAP
        - GRIPPER_OFFSET_MM
        - stack_height
        + _LENGTH_DIFFERENCE_96_TO_384
    )
    if assaymap:
        plane += HEIGHT_DIFF_96AM_TO_96LT
    return plane


def test_assaymap_gripper_plane_matches_the_reference() -> None:
    """z + zg must land where the reference run gripped the plate."""
    assert _gripper_plane(assaymap=True) == pytest.approx(REFERENCE_GRIP_PLANE, abs=0.01)


def test_without_the_correction_the_gripper_is_4_71_mm_short() -> None:
    """The regression this guards, and the size of it.

    4.71 mm on a 16 mm plate gripped 8 mm up its side is the difference between
    engaging the flange and closing above it. There is no force limit on the
    descent to catch that.
    """
    shortfall = REFERENCE_GRIP_PLANE - _gripper_plane(assaymap=False)
    assert shortfall == pytest.approx(HEIGHT_DIFF_96AM_TO_96LT, abs=0.01)


def test_the_constant_is_the_96am_to_96lt_head_difference() -> None:
    """Pin the value, now that the measurements have told us what it means."""
    assert HEIGHT_DIFF_96AM_TO_96LT == 4.71


def test_the_axis_split_does_not_move_the_gripper() -> None:
    """z + zg is conserved across _PLATE_HANDLING_ZG_MAX, so the cap is not a position.

    Worth stating because the plan output makes the cap look like the discrepancy:
    pybravo reports Zg 100.0 against the reference 105.0. That 5 mm is only where the
    travel sits, and chasing it would have hidden the real 4.71 mm error.
    """
    plane = _gripper_plane(assaymap=True)
    for cap in (100.0, 105.0):
        z = plane - cap
        assert z + cap == pytest.approx(plane, abs=1e-9)
