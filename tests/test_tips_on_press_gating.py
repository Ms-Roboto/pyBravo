"""The two-move tips-on press (fast unforced approach, then a bounded force
press) is AssayMAP-only. Every other head keeps the original single
force-limited jog covering the whole descent.

The difference is a safety property, not a speed one: on a controller without
tip_force_jog (Darwin, simulation) the original jog keeps the current limit on
for the entire travel, so a wrong labware height or an object on the tip box is
met under force control. The fast approach gives that up for the first part of
the descent, which is acceptable only where it has been verified on hardware --
the AssayMAP press -- and must not leak to 96LT/384 installs.
"""
from __future__ import annotations

import asyncio

import pytest

from pybravo.deck.labware import Labware
from pybravo.deck.teachpoints import Teachpoints
from pybravo.head_mode import TipSelection, normalize_head_mode
from pybravo.profile.profile import BravoProfile
from pybravo.state_machine.tasks import TipsOnTask
from pybravo.types import Axis, HeadType


class _PressRecorder:
    """A controller with move/jog but deliberately no tip_force_jog."""

    def __init__(self) -> None:
        self.moves: list[tuple[str, float, float | None]] = []
        self.jogs: list = []
        self.positions = {Axis.Z: 0.0, Axis.W: 0.0, Axis.G: 0.0, Axis.Zg: 0.0}

    def move(self, moves, wait=True):  # noqa: ARG002
        for m in moves:
            self.moves.append((m.axis.name, float(m.position), getattr(m, "velocity", None)))
            self.positions[m.axis] = float(m.position)

    def jog(self, params):
        self.jogs.append(params)
        self.positions[Axis.Z] = float(params.max_position)
        return float(params.max_position)

    def get_position(self, axis):
        return self.positions[axis]


def _build(head_type: HeadType, consumable: str) -> tuple[TipsOnTask, _PressRecorder]:
    profile = BravoProfile.default()
    profile.head.head_type = head_type
    teachpoints = Teachpoints()
    teachpoints.set_default_teachpoints(head_type)
    labware = Labware(
        id="tipbox", name="tip box", height=49.9, width=85.48, length=127.76,
        metadata={"tip_definition_id": consumable},
    )
    mode = normalize_head_mode(head_type, "all_barrels", "back_left")
    selection = TipSelection(
        location=1, row=0, col=0,
        row_count=mode.row_count, column_count=mode.column_count,
    )
    ctrl = _PressRecorder()
    task = TipsOnTask(ctrl, teachpoints, profile, labware, mode, selection, 1, tip_length_mm=0.0)
    return task, ctrl


@pytest.mark.parametrize("head_type,consumable", [
    (HeadType.HT_96_D_200, "lt_200ul"),
    (HeadType.HT_384_D_70, "st_70ul"),
])
def test_standard_heads_keep_the_single_force_limited_press(head_type, consumable):
    task, ctrl = _build(head_type, consumable)
    asyncio.run(task._lower_z_to_tips())

    assert [m for m in ctrl.moves if m[0] == "Z"] == [], (
        "no unforced Z move may precede the press on a standard head"
    )
    assert len(ctrl.jogs) == 1
    jog = ctrl.jogs[0]
    assert jog.axis is Axis.Z
    assert jog.velocity == pytest.approx(25.0)
    assert jog.acceleration == pytest.approx(250.0)
    assert jog.peak_current > 0


def test_assaymap_uses_the_fast_approach_then_a_bounded_press():
    task, ctrl = _build(HeadType.HT_96_ASSAYMAP, "am_cartridge_60ul")
    asyncio.run(task._lower_z_to_tips())

    z_moves = [m for m in ctrl.moves if m[0] == "Z"]
    assert len(z_moves) == 1, "one unforced approach move"
    assert len(ctrl.jogs) == 1
    approach_z = z_moves[0][1]
    press = ctrl.jogs[0]
    assert press.max_position - approach_z == pytest.approx(25.0)
