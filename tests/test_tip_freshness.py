"""Native tip inventory separates physical presence from usable fresh supply."""

from __future__ import annotations

import pytest

from pybravo.head_mode import normalize_head_mode
from pybravo.state_machine.engine import TaskStatus
from pybravo.types import HeadType
from pybravo.workflow.executor import WorkflowExecutor
from tests.test_bravo_init import _make_workflow_executor_bravo


@pytest.fixture
def tip_bravo(monkeypatch):
    bravo, _ = _make_workflow_executor_bravo()
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="full")
    bravo.set_labware(4, "tipbox-384", tipbox_fill_state="empty")
    tasks = []

    async def complete(task):
        tasks.append(task)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(bravo._engine, "execute", complete)
    yield bravo, tasks
    bravo.disconnect()


@pytest.mark.asyncio
async def test_returned_full_head_is_physically_present_but_never_fresh(tip_bravo):
    bravo, tasks = tip_bravo
    await bravo.tips_on(2)
    await bravo.tips_off(4)
    inventory = bravo.get_state()["tipbox_inventory"]
    assert len(inventory["2"]["empty"]) == 384
    assert inventory["2"]["fresh"] == inventory["2"]["spent"] == []
    assert len(inventory["4"]["occupied"]) == len(inventory["4"]["spent"]) == 384
    assert inventory["4"]["fresh"] == inventory["4"]["empty"] == []
    assert inventory["4"]["legal_pickup_anchors"] == []
    with pytest.raises(RuntimeError, match="No legal fresh tip anchors"):
        await bravo.tips_on(4)
    assert len(tasks) == 2, "spent pickup must fail before a native motion task"


@pytest.mark.asyncio
async def test_same_box_cycle_cannot_repump_the_returned_tip(tip_bravo):
    bravo, tasks = tip_bravo
    bravo.set_head_mode("single_barrel", "back_left")
    await bravo.tips_on(2)
    picked = bravo._tips_on_head_selection
    await bravo.tips_off(2)
    assert bravo._spent_tip_wells(2) == {(picked.row, picked.col)}
    assert len(bravo._fresh_tip_wells(2)) == 383
    with pytest.raises(RuntimeError, match="spent tips"):
        bravo._validated_tip_wells(bravo.deck.get_stack(2).top, bravo.head_mode, picked)
    # Existing packing rules may find another accessible clean region, or
    # require a dedicated return box. They may never repick the spent cell.
    try:
        await bravo.tips_on(2)
    except RuntimeError:
        assert len(tasks) == 2
    else:
        assert bravo._tips_on_head_selection != picked


@pytest.mark.asyncio
async def test_twelve_single_barrel_cycles_use_distinct_fresh_supply(tip_bravo):
    bravo, _ = tip_bravo
    bravo.set_head_mode("single_barrel", "back_left")
    picked = []
    for _ in range(12):
        await bravo.tips_on(2)
        selection = bravo._tips_on_head_selection
        picked.append((selection.row, selection.col))
        await bravo.tips_off(4)
    assert len(set(picked)) == 12
    assert len(bravo._fresh_tip_wells(2)) == 372
    assert len(bravo._spent_tip_wells(4)) == 12
    assert bravo._fresh_tip_wells(4) == set()


@pytest.mark.asyncio
async def test_spent_interleaved_quadrant_does_not_fall_back_to_another(tip_bravo):
    bravo, _ = tip_bravo
    bravo.profile.head.head_type = HeadType.HT_96_D_70
    bravo._head_mode = normalize_head_mode(HeadType.HT_96_D_70, "all_barrels", "back_left")
    picked = bravo.set_tip_selection(2, 0, 0, row_stride=2, col_stride=2)
    await bravo.tips_on(2)
    bravo._tip_selection = None
    await bravo.tips_off(2)
    assert bravo._tip_selection == picked, "return must retain the exact interleaved pickup footprint"
    with pytest.raises(RuntimeError, match="spent tips"):
        bravo._effective_tip_selection(2, bravo.deck.get_stack(2).top, bravo.head_mode)
    assert bravo._tip_selection == picked
    bravo.set_tip_selection(2, 0, 1, row_stride=2, col_stride=2)
    await bravo.tips_on(2)
    assert len(bravo._spent_tip_wells(2)) == 96
    assert len(bravo._fresh_tip_wells(2)) == 192


@pytest.mark.asyncio
async def test_refresh_preserves_spent_state_and_explicit_restock_resets_it(tip_bravo):
    bravo, _ = tip_bravo
    await bravo.tips_on(2)
    await bravo.tips_off(2)
    assert len(bravo._spent_tip_wells(2)) == 384
    bravo.refresh_live_labware("tipbox-384")
    assert len(bravo._spent_tip_wells(2)) == 384
    with pytest.raises(RuntimeError, match="No legal fresh tip anchors"):
        await bravo.tips_on(2)
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="full")
    assert bravo._spent_tip_wells(2) == set()
    assert len(bravo._fresh_tip_wells(2)) == 384
    await bravo.tips_on(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked", [True, False])
async def test_spent_and_empty_inventory_follow_a_moved_rack(tip_bravo, monkeypatch, tracked):
    bravo, _ = tip_bravo
    if not tracked:
        bravo.set_labware(2, "tipbox-384", track_tips=False)
    await bravo.tips_on(2)
    await bravo.tips_off(2)
    bravo.clear_labware(4)

    async def move_rack(task):
        group = task._deck.remove_mounted_group(task._from_location)
        task._deck.add_mounted_group(task._to_location, group)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(bravo._engine, "execute", move_rack)
    await bravo.pick_place(2, 4)
    assert len(bravo._spent_tip_wells(4)) == 384
    assert bravo._fresh_tip_wells(4) == set()
    assert 2 not in bravo._tipbox_occupancy
    assert (4 not in bravo._tipbox_untracked) is tracked
    with pytest.raises(RuntimeError, match="No legal fresh tip anchors"):
        await bravo.tips_on(4)


@pytest.mark.asyncio
async def test_moving_an_exhausted_supply_rack_does_not_refill_it(tip_bravo, monkeypatch):
    bravo, _ = tip_bravo
    await bravo.tips_on(2)
    await bravo.tips_off(4)
    bravo.clear_labware(4)

    async def move_rack(task):
        group = task._deck.remove_mounted_group(task._from_location)
        task._deck.add_mounted_group(task._to_location, group)
        task.status = TaskStatus.COMPLETED

    monkeypatch.setattr(bravo._engine, "execute", move_rack)
    await bravo.pick_place(2, 4)
    assert bravo._occupied_tip_wells(4) == bravo._fresh_tip_wells(4) == set()
    with pytest.raises(RuntimeError, match="No legal fresh tip anchors"):
        await bravo.tips_on(4)


@pytest.mark.asyncio
async def test_an_occupied_return_rack_is_rejected_before_motion(tip_bravo):
    bravo, tasks = tip_bravo
    bravo.set_labware(4, "tipbox-384", tipbox_fill_state="full")
    await bravo.tips_on(2)
    with pytest.raises(RuntimeError, match="No legal tip anchors"):
        await bravo.tips_off(4)
    assert len(tasks) == 1
    assert bravo._tips_on_head
    assert bravo._spent_tip_wells(4) == set()


def test_explicit_spent_load_and_untracked_load_do_not_report_fresh_supply(tip_bravo):
    bravo, _ = tip_bravo
    bravo.set_labware(2, "tipbox-384", tipbox_fill_state="spent")
    assert len(bravo._spent_tip_wells(2)) == 384
    assert bravo._fresh_tip_wells(2) == set()
    bravo.set_labware(2, "tipbox-384", track_tips=False)
    inventory = bravo.get_state()["tipbox_inventory"]["2"]
    assert inventory["freshness_tracked"] is False
    assert inventory["fresh"] == []


@pytest.mark.asyncio
async def test_aborted_tip_return_does_not_mark_a_phantom_spent_tip(tip_bravo, monkeypatch):
    bravo, _ = tip_bravo
    await bravo.tips_on(2)

    async def abort(task):
        task.status = TaskStatus.ABORTED

    monkeypatch.setattr(bravo._engine, "execute", abort)
    await bravo.tips_off(4)
    assert bravo._tips_on_head
    assert bravo._occupied_tip_wells(4) == bravo._spent_tip_wells(4) == set()


@pytest.mark.asyncio
async def test_return_tip_definition_mismatch_is_rejected_before_motion(tip_bravo):
    bravo, tasks = tip_bravo
    await bravo.tips_on(2)
    bravo.deck.get_stack(4).top.metadata["tip_definition_id"] = "st_30ul"
    with pytest.raises(RuntimeError, match="matching tip definitions"):
        await bravo.tips_off(4)
    assert len(tasks) == 1
    assert bravo._tips_on_head


@pytest.mark.asyncio
async def test_strict_executor_stops_instead_of_repicking_spent_tips(tip_bravo):
    bravo, tasks = tip_bravo
    events = []

    async def event(payload):
        events.append(payload)

    graph = {"nodes": [
        {"id": 1, "type": "flow/Start", "outputs": [{"links": [1]}]},
        {"id": 2, "type": "tips/TipsOn", "properties": {"location": 2}, "outputs": [{"links": [2]}]},
        {"id": 3, "type": "tips/TipsOff", "properties": {"location": 2}, "outputs": [{"links": [3]}]},
        {"id": 4, "type": "tips/TipsOn", "properties": {"location": 2}, "outputs": [{"links": [4]}]},
        {"id": 5, "type": "flow/End"},
    ], "links": [[1, 1, 0, 2, 0, -1], [2, 2, 0, 3, 0, -1], [3, 3, 0, 4, 0, -1], [4, 4, 0, 5, 0, -1]]}
    executor = WorkflowExecutor(bravo, graph, strict_validation=True, preview_animation=False, on_event=event)
    await executor.execute()
    assert len(tasks) == 2
    assert events[-1]["type"] == "workflow:error"
    assert "fresh tip anchors" in events[-1]["error"]
    assert not any(item["type"] == "workflow:complete" for item in events)
