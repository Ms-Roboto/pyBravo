"""Native liquid anchors describe the entire mounted footprint, not four UI quadrants."""

import pytest

from pybravo.bravo import Bravo
from pybravo.controllers.simulation import SimulationController
from pybravo.deck.labware import Labware, LabwareDefinition
from pybravo.head_mode import HeadMode, legal_plate_anchors, normalize_head_mode, plate_footprint_wells, plate_selection
from pybravo.profile.profile import BravoProfile
from pybravo.types import Axis, AxisRange, HeadType
from pybravo.workflow.executor import WorkflowExecutor, _parse_anchor, _resolve_dynamic_properties

HEAD = HeadType.HT_384_D_70
CORNERS = ["back_left", "back_right", "front_left", "front_right"]


def test_uninitialized_head_mode_cannot_claim_plate_access():
    # HeadMode's zero counts need head-specific normalization before planning.
    assert legal_plate_anchors(HEAD, HeadMode(), 32, 48, 2.25, 2.25) == []


@pytest.mark.parametrize("invalid_pitch", [float("nan"), float("inf"), -float("inf"), 0.0, 1e-320])
def test_invalid_plate_pitch_fails_closed(invalid_pitch):
    mode = normalize_head_mode(HEAD, "all_barrels", "back_left")
    assert plate_footprint_wells(HEAD, mode, 32, 48, invalid_pitch, 2.25, 0, 0) == []
    assert plate_footprint_wells(HEAD, mode, 32, 48, 2.25, invalid_pitch, 0, 0) == []


@pytest.mark.parametrize("corner", CORNERS)
@pytest.mark.parametrize("subset,rows,cols,plate_rows,plate_cols,pitch,expected", [
    ("all_barrels", 16, 24, 16, 24, 4.5, 1),
    ("column", 16, 1, 16, 24, 4.5, 24),
    ("row", 1, 24, 16, 24, 4.5, 16),
    ("rectangle", 3, 5, 16, 24, 4.5, 280),
    ("single_barrel", 1, 1, 16, 24, 4.5, 384),
    ("all_barrels", 16, 24, 32, 48, 2.25, 4),
    ("column", 16, 1, 32, 48, 2.25, 96),
    ("row", 1, 24, 32, 48, 2.25, 64),
    ("rectangle", 3, 5, 32, 48, 2.25, 1120),
    ("single_barrel", 1, 1, 32, 48, 2.25, 1536),
    ("single_barrel", 1, 1, 8, 12, 9.0, 96),
    ("all_barrels", 16, 24, 8, 12, 9.0, 0),
])
def test_native_anchors_cover_every_fitting_footprint(corner, subset, rows, cols, plate_rows, plate_cols, pitch, expected):
    mode = normalize_head_mode(HEAD, subset, corner, rows, cols)
    anchors = legal_plate_anchors(HEAD, mode, plate_rows, plate_cols, pitch, pitch)
    assert len(anchors) == expected
    for anchor in anchors:
        footprint = plate_footprint_wells(HEAD, mode, plate_rows, plate_cols, pitch, pitch, anchor.row, anchor.col)
        assert len(footprint) == rows * cols
        assert len(set(footprint)) == rows * cols
        assert footprint[0] == (anchor.row, anchor.col)
        assert all(0 <= r < plate_rows and 0 <= c < plate_cols for r, c in footprint)


@pytest.mark.parametrize("corner", CORNERS)
def test_unused_pitch_axis_does_not_reject_a_single_row_rectangle(corner):
    mode = normalize_head_mode(HEAD, "rectangle", corner, 1, 5)
    anchors = legal_plate_anchors(HEAD, mode, 8, 24, 4.5, 9.0)
    assert len(anchors) == 8 * 20
    assert plate_footprint_wells(HEAD, mode, 8, 24, 4.5, 9.0, 7, 19) == [(7, c) for c in range(19, 24)]


def _bravo_with_plate():
    profile = BravoProfile.default()
    profile.head.head_type = HEAD
    bravo = Bravo(profile=profile)
    bravo._controller = SimulationController()
    bravo.teachpoints.set_teachpoint(5, Axis.X, 150.0)
    bravo.teachpoints.set_teachpoint(5, Axis.Y, 120.0)
    bravo.teachpoints.set_teachpoint(5, Axis.Z, 140.0)
    plate = Labware.from_definition(LabwareDefinition(
        id="test-384", name="384 test plate", kind="sbs_plate", base_class="microplate",
        height_mm=15.0, wells=384, rows=16, cols=24,
        spacing_x_mm=4.5, spacing_y_mm=4.5, offset_x_mm=2.25, offset_y_mm=2.25,
    ))
    bravo.deck.set_single(5, plate)
    return bravo, plate


@pytest.mark.parametrize("corner,offset_x,offset_y", [
    ("back_left", 0, 0), ("back_right", 94.5, 0),
    ("front_left", 0, 63), ("front_right", 94.5, 63),
])
def test_rectangle_plate_mapping_accounts_for_physical_barrel_corner(corner, offset_x, offset_y):
    bravo, plate = _bravo_with_plate()
    mode = bravo.set_head_mode("rectangle", corner, 2, 3)
    target = bravo._plate_xy_target(5, plate, mode, plate_selection(5, 3, 5))
    assert target == pytest.approx((150 - 2.25 + 5 * 4.5 - offset_x, 120 - 2.25 + 3 * 4.5 - offset_y))


def test_plate_picker_retains_axis_travel_limits_for_corner_subsets():
    bravo, _ = _bravo_with_plate()
    bravo.set_head_mode("single_barrel", "front_right")
    bravo.profile.axes["X"].range = AxisRange(50, 100)
    bravo.profile.axes["Y"].range = AxisRange(55, 80)
    anchors = bravo.get_plate_selection_state(5)["legal_anchors"]
    # Target coordinates are X=44.25+4.5*column, Y=50.25+4.5*row.
    assert {(a["row"], a["col"]) for a in anchors} == {(r, c) for r in range(2, 7) for c in range(2, 13)}
    with pytest.raises(RuntimeError, match="not reachable"):
        bravo.set_plate_selection(5, 0, 0)


def _mount_subset_then_change_pickup_mode(bravo):
    mounted = bravo.set_head_mode("rectangle", "front_right", 2, 3)
    bravo._tips_on_head = True
    bravo._tips_on_head_mode = mounted
    bravo._attached_tip_length_mm = 26.1
    bravo.set_head_mode("all_barrels", "back_left")
    return mounted


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["aspirate", "dispense", "mix"])
async def test_liquid_selection_and_tasks_follow_mounted_tips_not_future_pickup_mode(operation):
    bravo, _ = _bravo_with_plate()
    mounted = _mount_subset_then_change_pickup_mode(bravo)
    selection = bravo.set_plate_selection(5, 3, 5)
    state = bravo.get_plate_selection_state(5)
    assert state["selection"] == selection.to_dict()
    assert state["footprint"] == [{"row": r, "col": c} for r in (3, 4) for c in (5, 6, 7)]
    assert len(state["legal_anchors"]) == 15 * 22
    captured = []

    async def record_task(task):
        captured.append(task)

    bravo._engine.execute = record_task
    await getattr(bravo, operation)(5, 5.0)
    task = captured[0]
    assert task._head_mode == mounted
    assert task._plate_selection == selection
    assert task._well_xy() == pytest.approx((75.75, 68.25))
    executor = WorkflowExecutor(bravo, {"nodes": [], "links": []})
    assert executor._get_well_xy(5) == pytest.approx(task._well_xy())


@pytest.mark.asyncio
async def test_animation_preserves_explicit_well_anchor_before_native_dispatch(monkeypatch):
    bravo, _ = _bravo_with_plate()
    _mount_subset_then_change_pickup_mode(bravo)
    selection = bravo.set_plate_selection(5, 3, 5)
    events = []

    async def no_delay(_):
        pass

    monkeypatch.setattr("pybravo.workflow.executor.asyncio.sleep", no_delay)
    executor = WorkflowExecutor(bravo, {"nodes": [], "links": []}, on_event=events.append)
    await executor._animate_task_motion("liquid/Aspirate", {"location": 5, "volume": 5, "anchor": "D6", "quadrant": "B2"})
    assert bravo._plate_selection[5] == selection
    positions = [event["positions"] for event in events if event["type"] == "workflow:positions"]
    assert positions
    assert all((point["X"], point["Y"]) == pytest.approx((75.75, 68.25)) for point in positions)


def test_mounting_full_head_cannot_be_reinterpreted_as_a_single_tip():
    bravo, _ = _bravo_with_plate()
    bravo._tips_on_head = True
    bravo._tips_on_head_mode = bravo.head_mode
    bravo.set_head_mode("single_barrel", "back_left")
    assert bravo.get_plate_selection_state(5)["legal_anchors"] == [{"row": 0, "col": 0}]
    with pytest.raises(RuntimeError, match="not legal"):
        bravo.set_plate_selection(5, 0, 1)


@pytest.mark.parametrize("label,cell", [("A1", (0, 0)), ("P24", (15, 23)), ("AA1", (26, 0)), ("AF48", (31, 47)), (" af48 ", (31, 47))])
def test_runtime_anchor_parser_includes_every_1536_plate_row(label, cell):
    assert _parse_anchor(label) == cell


@pytest.mark.parametrize("label", ["A0", "A-1", "A01", "Α1", "A１", "ß1", "A1.5", "", "iter:A1,AA1", "var:anchor"])
def test_runtime_anchor_parser_rejects_malformed_and_unresolved_labels(label):
    with pytest.raises(ValueError):
        _parse_anchor(label)


def test_dynamic_anchor_resolution_preserves_multi_letter_plate_rows():
    values = _resolve_dynamic_properties({"anchor": "iter:A1,AF48"}, [1], {})
    assert _parse_anchor(values["anchor"]) == (31, 47)
    values = _resolve_dynamic_properties({"anchor": "var:well"}, [], {"well": "AA7"})
    assert _parse_anchor(values["anchor"]) == (26, 6)
