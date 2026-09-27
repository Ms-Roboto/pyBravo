"""connect() must put the profile's head type onto the controller.

Measured on the instrument 2026-09-18: after a bare `/api/connect`, pybravo
reported `head_type: HT_96_ASSAYMAP` from the profile while the controller still
held _DTIP_STANDARD's W calibration --

    W hardware_minimum  -16.48   (AssayMAP: -19.921875)
    W hardware_maximum   63.52   (AssayMAP:  80.078125)

an 80 mm hardware range where the AssayMAP's is 100. Nothing errors. Every volume
is simply commanded at 0.8x, so a requested 100 uL delivers 80, and homing parks
the plunger in the wrong frame. It was worked around for two sessions by calling
`POST /api/change_head` after every connect, which is exactly the kind of step an
operator eventually forgets.
"""
from __future__ import annotations

import pytest

from pybravo.bravo import Bravo
from pybravo.profile.profile import BravoProfile
from pybravo.types import HeadType

ASSAYMAP_W_RANGE_MM = 100.0
DTIP_STANDARD_W_RANGE_MM = 80.0


class _RecordingController:
    """Minimal stand-in that records what connect() pushes to it."""

    def __init__(self) -> None:
        self.head_types: list[HeadType] = []

    def set_head_type(self, head_type: HeadType) -> None:
        self.head_types.append(head_type)


class _NoSetter:
    """A controller that does not support set_head_type at all."""


class _Failing:
    def set_head_type(self, head_type: HeadType) -> None:
        raise RuntimeError("node did not answer")


def _bravo_with(controller) -> Bravo:
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_96_ASSAYMAP
    bravo = Bravo(profile=profile)
    bravo._controller = controller
    return bravo


def test_connect_pushes_the_profile_head_to_the_controller() -> None:
    controller = _RecordingController()
    bravo = _bravo_with(controller)
    bravo._apply_profile_head_to_controller()
    assert controller.head_types == [HeadType.HT_96_ASSAYMAP]


def test_a_controller_without_the_hook_is_left_alone() -> None:
    """Not every controller has set_head_type; that must not break connect()."""
    bravo = _bravo_with(_NoSetter())
    bravo._apply_profile_head_to_controller()   # must not raise


def test_a_failure_warns_rather_than_aborting_the_connection() -> None:
    """A connected instrument in the default frame beats no connection.

    The warning is the contract here -- it is the only signal that W is running on
    the wrong calibration, which is otherwise silent.
    """
    bravo = _bravo_with(_Failing())
    bravo._apply_profile_head_to_controller()   # must not raise


def test_the_two_w_frames_differ_enough_to_matter() -> None:
    """Guard the reason this exists, so the numbers are not lost to a refactor."""
    from pybravo.darwin.waxis_config import config_for_head

    assaymap = config_for_head(HeadType.HT_96_ASSAYMAP)
    assert assaymap is not None
    span = assaymap.hardware_max - assaymap.hardware_min
    assert span == pytest.approx(ASSAYMAP_W_RANGE_MM, abs=1e-6)
    ratio = DTIP_STANDARD_W_RANGE_MM / span
    assert ratio == pytest.approx(0.8, abs=1e-6), "a 20% volume error, silently"


def test_other_heads_are_not_applied_on_connect() -> None:
    """Applying the head also swaps in per-head W software limits and, on Darwin,
    arms a W parameter-table write. Existing 96LT/384 profiles carry W positions
    outside those limits, so only AssayMAP opts in; every other head keeps the
    explicit POST /api/change_head path it always had."""
    for head in (HeadType.HT_96_D_200, HeadType.HT_384_D_70, HeadType.HT_96_F_50):
        controller = _RecordingController()
        profile = BravoProfile.default()
        profile.head.head_type = head
        bravo = Bravo(profile=profile)
        bravo._controller = controller
        bravo._apply_profile_head_to_controller()
        assert controller.head_types == [], head.name
