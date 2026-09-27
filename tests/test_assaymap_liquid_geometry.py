"""Liquid-handling Z geometry must account for what is mounted on the head.

Teachpoints are taught with one consumable and the machine runs with another, so
the head has to sit at a different Z to put the working tip where the teach tip
was. AssayMAP is the sharp case: taught with a 55.5 mm LT250 tip, operated with
29.2 mm cartridges — a 26.3 mm delta.

Before this was fixed AssayMAP fell outside the `is_disposable` branch entirely,
so the delta was dropped and every plane sat 26.3 mm off.

Crucially the head can also run **bare** or with LT250 tips, not only cartridges,
so the delta must follow whatever is fitted and liquid handling must NOT require
something to be mounted. That distinguishes it from the disposable-tip heads,
which do require tips.
"""
from __future__ import annotations

import pytest

from pybravo.deck.labware import Labware
from pybravo.deck.teachpoints import Teachpoints
from pybravo.state_machine.tasks import BARE_PROBE_LENGTH_MM, _build_liquid_z_geometry
from pybravo.types import HeadType

AM = HeadType.HT_96_ASSAYMAP
TEACH_TIP_MM = 55.5      # am_lt250_teach
CARTRIDGE_MM = 29.2      # am_cartridge_60ul


@pytest.fixture
def teachpoints():
    tp = Teachpoints()
    tp.set_default_teachpoints(AM)
    return tp


@pytest.fixture
def labware():
    return Labware(id="plate", name="plate", height=14.4, width=85.48, length=127.76)


def _geometry(teachpoints, labware, **overrides):
    kwargs = dict(
        teachpoints=teachpoints,
        location=1,
        labware=labware,
        head_type=AM,
        teach_tip_length_mm=TEACH_TIP_MM,
        attached_tip_length_mm=CARTRIDGE_MM,
        tips_on_head=True,
        distance_from_bottom_mm=1.0,
    )
    kwargs.update(overrides)
    return _build_liquid_z_geometry(**kwargs)


def test_assaymap_applies_the_cartridge_delta(teachpoints, labware):
    """55.5 mm taught, 29.2 mm fitted -> the head must sit 26.3 mm deeper."""
    geom = _geometry(teachpoints, labware)
    assert geom.tip_delta_mm == pytest.approx(TEACH_TIP_MM - CARTRIDGE_MM)
    assert geom.tip_delta_mm == pytest.approx(26.3)
    assert geom.target_head_z == pytest.approx(geom.target_tip_z + 26.3)


@pytest.mark.parametrize(
    "mode, attached, tips_on, expected_delta",
    [
        ("LT250 tips", TEACH_TIP_MM, True, 0.0),
        ("cartridges", CARTRIDGE_MM, True, TEACH_TIP_MM - CARTRIDGE_MM),
        ("bare head", None, False, TEACH_TIP_MM - BARE_PROBE_LENGTH_MM),
    ],
)
def test_assaymap_delta_follows_whatever_is_fitted(
    teachpoints, labware, mode, attached, tips_on, expected_delta
):
    """All three modes are legitimate on this head: bare, LT250 tips, cartridges.

    Each puts the working tip a different distance below the head reference, so
    each needs its own delta. Pipetting bare must be allowed, not refused.
    """
    geom = _geometry(
        teachpoints, labware,
        attached_tip_length_mm=attached, tips_on_head=tips_on,
    )
    assert geom.tip_delta_mm == pytest.approx(expected_delta), mode
    assert geom.target_head_z == pytest.approx(geom.target_tip_z + expected_delta)


def test_assaymap_refuses_without_a_known_cartridge_length(teachpoints, labware):
    with pytest.raises(RuntimeError, match="attached cartridge length"):
        _geometry(teachpoints, labware, attached_tip_length_mm=None)


def test_assaymap_refuses_without_a_taught_tip_length(teachpoints, labware):
    with pytest.raises(RuntimeError, match="taught tip length"):
        _geometry(teachpoints, labware, teach_tip_length_mm=None)


@pytest.mark.parametrize("head_type", [HeadType.HT_96_D_70, HeadType.HT_384_D_70])
def test_disposable_heads_are_unchanged(teachpoints, labware, head_type):
    """Regression: the heads that already worked keep the same behaviour and
    still say 'tips', not 'cartridges'."""
    tp = Teachpoints()
    tp.set_default_teachpoints(head_type)
    geom = _build_liquid_z_geometry(
        teachpoints=tp, location=1, labware=labware, head_type=head_type,
        teach_tip_length_mm=55.2, attached_tip_length_mm=26.1,
        tips_on_head=True, distance_from_bottom_mm=1.0,
    )
    assert geom.tip_delta_mm == pytest.approx(55.2 - 26.1)

    with pytest.raises(RuntimeError, match="requires tips on the head"):
        _build_liquid_z_geometry(
            teachpoints=tp, location=1, labware=labware, head_type=head_type,
            teach_tip_length_mm=55.2, attached_tip_length_mm=26.1,
            tips_on_head=False, distance_from_bottom_mm=1.0,
        )


@pytest.mark.parametrize(
    "head_type, expected",
    [
        (HeadType.HT_96_ASSAYMAP, True),
        (HeadType.HT_96_D_70, True),
        (HeadType.HT_384_D_70, True),
        (HeadType.HT_96_F_50, False),    # fixed tips
        (HeadType.HT_96_PINTOOL, False), # pin tool
    ],
)
def test_mounts_consumable_classification(head_type, expected):
    """AssayMAP is neither is_disposable nor is_fixed, which is exactly why it
    slipped through. mounts_consumable is the property that catches it."""
    assert head_type.mounts_consumable is expected


def test_assaymap_is_neither_disposable_nor_fixed():
    """Pin this, because it is the trap: any `if is_disposable / elif is_fixed`
    chain silently skips AssayMAP."""
    assert AM.is_disposable is False
    assert AM.is_fixed is False
    assert AM.mounts_consumable is True


# --- A17: homing W on a syringe head expels its contents ---------------------

class _FakeController:
    def __init__(self, w_position: float) -> None:
        self._w = w_position
        self.homed: list[str] = []
        self.moves: list[tuple[str, float]] = []

    def get_position(self, axis):
        return self._w

    def home_axes(self, axes, force=False):  # noqa: ARG002
        self.homed += [a.name for a in axes]

    def move(self, moves, wait=True):  # noqa: ARG002
        for m in moves:
            self.moves.append((m.axis.name, float(m.position)))
            self._w = float(m.position)


def _bravo_with(head_type, w_position):
    import asyncio  # noqa: F401  (used by callers)

    from pybravo.bravo import Bravo
    from pybravo.profile.profile import BravoProfile
    from pybravo.types import Axis

    bravo = Bravo.__new__(Bravo)           # bypass __init__; we only need two fields
    profile = BravoProfile.default()
    profile.head.head_type = head_type
    bravo._profile = profile
    bravo._controller = _FakeController(w_position)
    bravo._homed_axes = set()
    bravo._emit = lambda *a, **k: None
    return bravo, bravo._controller, Axis


def test_homing_w_refuses_when_the_assaymap_syringe_is_loaded():
    """No operator prompt exists on this path, so it must fail closed."""
    import asyncio
    bravo, ctrl, Axis = _bravo_with(HeadType.HT_96_ASSAYMAP, w_position=12.0)
    with pytest.raises(RuntimeError, match="would expel the contents"):
        asyncio.run(bravo.home_single_axis(Axis.W))
    assert ctrl.homed == [], "must refuse before homing, not after"


def test_homing_w_allowed_when_the_assaymap_syringe_is_empty():
    import asyncio
    bravo, ctrl, Axis = _bravo_with(HeadType.HT_96_ASSAYMAP, w_position=0.0)
    asyncio.run(bravo.home_single_axis(Axis.W))
    assert ctrl.homed == ["W"]


def test_homing_w_allowed_with_explicit_override():
    """Deliberate expulsion — e.g. positioned over waste."""
    import asyncio
    bravo, ctrl, Axis = _bravo_with(HeadType.HT_96_ASSAYMAP, w_position=12.0)
    asyncio.run(bravo.home_single_axis(Axis.W, expel_ok=True))
    assert ctrl.homed == ["W"]


@pytest.mark.parametrize("head_type", [HeadType.HT_96_D_70, HeadType.HT_384_D_70])
def test_homing_w_unchanged_for_disposable_heads(head_type):
    """Regression: on a tip plunger this was always fine and stays fine."""
    import asyncio
    bravo, ctrl, Axis = _bravo_with(head_type, w_position=12.0)
    asyncio.run(bravo.home_single_axis(Axis.W))
    assert ctrl.homed == ["W"]
    assert ("W", 0.0) in ctrl.moves
