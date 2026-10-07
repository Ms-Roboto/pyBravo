"""A physically open rack cell must also be reachable by the active barrel."""

from dataclasses import replace

import pytest

from pybravo.types import Axis
from tests.test_bravo_init import _make_workflow_executor_bravo

TEACHPOINTS = {
    1: (9.29, 9.98), 2: (195.98, 10.18), 3: (382.67, 9.68),
    4: (9.29, 118.87), 5: (195.88, 118.77), 6: (382.67, 119.07),
    7: (9.29, 227.97), 8: (196.28, 227.97), 9: (382.47, 228.17),
}
# Independent geometric expectations: rack cell offset minus head barrel
# offset, for a 4.5 mm 384 grid with -2.25 mm rack calibration offset.
CORNERS = [
    ("back_left", 15, 23, 101.25, 65.25),
    ("back_right", 15, 0, -105.75, 65.25),
    ("front_left", 0, 23, 101.25, -69.75),
    ("front_right", 0, 0, -105.75, -69.75),
]


@pytest.fixture
def bravo():
    instance, _ = _make_workflow_executor_bravo()
    for location, (x, y) in TEACHPOINTS.items():
        instance.teachpoints.set_teachpoint(location, Axis.X, x)
        instance.teachpoints.set_teachpoint(location, Axis.Y, y)
    yield instance
    instance.disconnect()


@pytest.mark.parametrize("location", range(1, 10))
@pytest.mark.parametrize("corner,row,col,dx,dy", CORNERS)
def test_explicit_pickup_matches_barrel_geometry_and_deck_travel_limits(
    bravo, location, corner, row, col, dx, dy,
):
    bravo.set_labware(location, "tipbox-384", tipbox_fill_state="full")
    bravo.set_head_mode("single_barrel", corner)
    rack = bravo.deck.get_stack(location).top
    teach_x, teach_y = TEACHPOINTS[location]
    x, y = teach_x + dx, teach_y + dy
    assert bravo._tip_xy_target(location, rack, bravo.head_mode, row, col) == pytest.approx((x, y))
    reachable = 0 <= x <= 390 and 0.5 <= y <= 231
    legal = bravo._legal_tip_anchors(location, rack, bravo.head_mode, purpose="pickup")
    assert bool(legal) is reachable
    if reachable:
        selection = bravo.set_tip_selection(location, row, col)
        assert (selection.row, selection.col) == (row, col)
    else:
        with pytest.raises(RuntimeError, match="outside the configured X/Y range"):
            bravo.set_tip_selection(location, row, col)
        assert bravo._tip_selection is None
    assert len(bravo._fresh_tip_wells(location)) == 384


@pytest.mark.asyncio
async def test_restored_p24_selection_at_position_8_cannot_start_native_motion(bravo, monkeypatch):
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="full")
    bravo.set_labware(8, "tipbox-384", tipbox_fill_state="full")
    bravo.set_head_mode("single_barrel", "back_left")
    at_two = bravo.set_tip_selection(2, 15, 23)
    bravo._tip_selection = replace(at_two, location=8)  # An old saved selection.
    before = {axis: bravo.get_position(axis) for axis in Axis}

    async def unexpected_motion(task):
        pytest.fail("An unreachable tip selection dispatched a native task")

    monkeypatch.setattr(bravo._engine, "execute", unexpected_motion)
    with pytest.raises(RuntimeError, match="No legal fresh tip anchors"):
        await bravo.tips_on(8)
    assert {axis: bravo.get_position(axis) for axis in Axis} == before
    assert len(bravo._fresh_tip_wells(8)) == 384
    assert not bravo._tips_on_head


@pytest.mark.parametrize("tracked", [True, False])
@pytest.mark.parametrize("purpose", ["pickup", "return"])
def test_explicit_validation_rejects_unreachable_cell_for_both_rack_roles(bravo, tracked, purpose):
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="full")
    bravo.set_head_mode("single_barrel", "back_left")
    selection = replace(bravo.set_tip_selection(2, 15, 23), location=8)
    bravo.set_labware(8, "tipbox-384", tipbox_fill_state="full" if purpose == "pickup" else "empty", track_tips=tracked)
    rack = bravo.deck.get_stack(8).top
    with pytest.raises(RuntimeError, match=r"Y 293\.22 mm;.*Y 0\.50\.\.231\.00"):
        bravo._validated_tip_wells(rack, bravo.head_mode, selection, purpose=purpose)


def test_reachability_is_rechecked_after_teachpoint_changes(bravo):
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="full")
    bravo.set_head_mode("single_barrel", "back_left")
    selection = bravo.set_tip_selection(2, 15, 23)
    bravo.teachpoints.set_teachpoint(2, Axis.Y, 227.97)
    with pytest.raises(RuntimeError, match="outside the configured X/Y range"):
        bravo._validated_tip_wells(bravo.deck.get_stack(2).top, bravo.head_mode, selection)
