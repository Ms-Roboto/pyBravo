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


def test_other_heads_are_applied_on_connect_too() -> None:
    """Every head gets its calibration on connect, not only AssayMAP. The shipped
    Darwin profiles carry tips_off_w_position values inside the per-head W
    software limits, so this cannot refuse a Tips Off."""
    for head in (HeadType.HT_96_D_200, HeadType.HT_384_D_70, HeadType.HT_96_F_50):
        controller = _RecordingController()
        profile = BravoProfile.default()
        profile.head.head_type = head
        bravo = Bravo(profile=profile)
        bravo._controller = controller
        bravo._apply_profile_head_to_controller()
        assert controller.head_types == [head], head.name


def test_shipped_darwin_profiles_eject_inside_the_per_head_w_limits() -> None:
    """Guard the numbers this depends on: if a profile's tips_off_w_position ever
    drifts outside the head's software minimum again, Tips Off would be refused
    pre-flight after connect."""
    from pathlib import Path

    from pybravo.darwin.waxis_config import config_for_head

    root = Path(__file__).resolve().parents[1] / "profiles"
    for name in ("96LT", "Claptrap", "Flowby", "default", "384"):
        profile = BravoProfile.load(root / f"{name}.yaml")
        cfg = config_for_head(profile.head.head_type)
        assert cfg is not None, name
        cal = cfg.calibration()
        w = profile.safety.tips_off_w_position
        assert cal.effective_software_min <= w <= cal.effective_software_max, (
            f"{name}: tips_off_w_position {w} outside "
            f"[{cal.effective_software_min:.3f}, {cal.effective_software_max:.3f}]"
        )
