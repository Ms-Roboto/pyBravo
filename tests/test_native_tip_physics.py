"""Native tip motions checked by SuperDex with explicit synthetic rack geometry.

The synthetic rack holes below are mechanical test fixtures, not qualified
catalog dimensions. The deployed rack's unknown holes remain a separate,
expected failure rather than receiving those synthetic values.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.physics.errors import PhysicalSimulationError
from pybravo.physics.runtime import CollisionRehearsal
from pybravo.profile.profile import BravoProfile
from tests.test_bravo_init import _catalog_from_definitions

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "simulation.yaml"
RACK_ID = "lw-4914769d0af7"


def _raise_task_error(error):
    raise error.original_exception


def _native_tip_bravo(*, synthetic_holes: bool) -> Bravo:
    bravo = Bravo(profile=BravoProfile.load(PROFILE_PATH), mode="simulation")
    original = bravo._labware_catalog.get_definition(RACK_ID)
    assert original is not None
    if synthetic_holes:
        fixture = replace(
            original,
            id="synthetic-st10-rack",
            name="Synthetic mechanical ST10 rack fixture",
            well_diameter_mm=3.5,
            well_depth_mm=50.0,
        )
        bravo._labware_catalog = _catalog_from_definitions(fixture)
        rack_id = fixture.id
    else:
        # Reproduce the deployed missing-hole condition in a private fixture.
        # Later catalog review may supply the real diameter; that should not
        # remove this regression for any other record with unknown holes.
        fixture = replace(original, id='unknown-hole-rack', name='Unknown-hole rack fixture', well_diameter_mm=0.0)
        bravo._labware_catalog = _catalog_from_definitions(fixture)
        rack_id = fixture.id
    bravo.set_labware(2, rack_id, tipbox_fill_state="full")
    bravo.set_labware(4, rack_id, tipbox_fill_state="empty")
    bravo.set_head_mode("single_barrel", "back_left")
    bravo.connect()
    bravo.controller.set_move_timing_enabled(False)
    bravo._engine.set_error_handler(_raise_task_error)
    return bravo


@pytest.mark.asyncio
async def test_twelve_native_single_tip_cycles_have_distinct_clean_and_spent_cells():
    bravo = _native_tip_bravo(synthetic_holes=True)
    runtime = CollisionRehearsal(bravo)
    completed_steps = []
    bravo._engine.set_step_handler(lambda task_name, step: completed_steps.append((task_name, step)))
    picked = []
    returned = []
    try:
        await asyncio.wait_for(runtime.initialize(), timeout=60)
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("dock", "system/DockGripper", {})
        await asyncio.wait_for(bravo.dock_gripper(), timeout=30)
        for iteration in range(12):
            await runtime.set_context(f"on-{iteration}", "tips/TipsOn", {"location": 2})
            await asyncio.wait_for(bravo.tips_on(2), timeout=30)
            selection = bravo._tips_on_head_selection
            assert selection is not None
            picked.append((selection.row, selection.col))
            await runtime.set_context(f"off-{iteration}", "tips/TipsOff", {"location": 4})
            await asyncio.wait_for(bravo.tips_off(4), timeout=30)
            selection = bravo._tip_selection
            assert selection is not None
            returned.append((selection.row, selection.col))
        assert len(set(picked)) == len(set(returned)) == 12
        assert len(bravo._fresh_tip_wells(2)) == 372
        assert len(bravo._spent_tip_wells(4)) == 12
        assert bravo._fresh_tip_wells(4) == set()
        assert not bravo._tips_on_head
        assert sum(step == "lower_z_to_tips" for _, step in completed_steps) == 12
        assert sum(step == "eject_tips" for _, step in completed_steps) == 12
        report = runtime.report()
        assert report["status"] == "checked"
        assert report["moves_checked"] >= 120
        assert report["samples_checked"] > report["moves_checked"]
        assert report["qualification_granted"] is False
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_native_return_to_deployed_rack_with_unknown_holes_fails_visibly():
    bravo = _native_tip_bravo(synthetic_holes=False)
    runtime = CollisionRehearsal(bravo)
    try:
        await asyncio.wait_for(runtime.initialize(), timeout=60)
        bravo.controller.set_motion_guard(runtime.check_motion)
        await runtime.set_context("dock", "system/DockGripper", {})
        await asyncio.wait_for(bravo.dock_gripper(), timeout=30)
        await runtime.set_context("on", "tips/TipsOn", {"location": 2})
        await asyncio.wait_for(bravo.tips_on(2), timeout=30)
        assert bravo._tips_on_head
        await runtime.set_context("off", "tips/TipsOff", {"location": 4})
        with pytest.raises(PhysicalSimulationError, match="lacks recorded well_diameter_mm"):
            await asyncio.wait_for(bravo.tips_off(4), timeout=30)
        assert bravo._tips_on_head
        assert bravo._spent_tip_wells(4) == set()
        report = runtime.report()
        assert report["status"] == "failed"
        assert report["last_error"]["node_id"] == "off"
        assert "physical tip entry" in report["last_error"]["message"]
        assert report['last_error']['missing_geometry'] == ['well_diameter_mm']
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()


@pytest.mark.asyncio
async def test_undocked_gripper_blocks_native_subset_pickup_before_inventory_changes():
    bravo = _native_tip_bravo(synthetic_holes=True)
    runtime = CollisionRehearsal(bravo)
    try:
        await asyncio.wait_for(runtime.initialize(), timeout=60)
        bravo.controller.set_motion_guard(runtime.check_motion)
        completed_cycles = 0
        with pytest.raises(RuntimeError, match="Physical collision.*finger"):
            for iteration in range(12):
                await runtime.set_context(f"on-{iteration}", "tips/TipsOn", {"location": 2})
                await asyncio.wait_for(bravo.tips_on(2), timeout=30)
                await runtime.set_context(f"off-{iteration}", "tips/TipsOff", {"location": 4})
                await asyncio.wait_for(bravo.tips_off(4), timeout=30)
                completed_cycles += 1
        assert 0 < completed_cycles < 12
        assert len(bravo._fresh_tip_wells(2)) == 384 - completed_cycles
        assert len(bravo._spent_tip_wells(4)) == completed_cycles
        assert not bravo._tips_on_head
        report = runtime.report()
        assert report["status"] == "failed"
        assert report["last_error"]["node_type"] == "tips/TipsOn"
        assert any("finger" in body for body in report["last_error"]["bodies"])
        assert report["contact_queries"] > 0
    finally:
        bravo.controller.set_motion_guard(None)
        await runtime.close()
        bravo.disconnect()
