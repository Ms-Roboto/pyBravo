"""Designer plate placements use native geometry and never move the live Bravo."""
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from pybravo.bravo import Bravo
from pybravo.deck.labware import InMemoryLabwareCatalog, LabwareDefinition
from pybravo.profile.profile import BravoProfile
from pybravo.types import HeadType
from pybravo.web import server


@pytest.fixture
def live(monkeypatch):
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.teachpoints.set_default_teachpoints(profile.head.head_type)
    robot = Bravo(profile=profile, mode="simulation")
    definitions = []
    for name, rows, cols, pitch in (("plate384", 16, 24, 4.5), ("plate1536", 32, 48, 2.25), ("plate96", 8, 12, 9)):
        definitions.append(LabwareDefinition(id=name, name=name, kind="sbs_plate", base_class="microplate",
            wells=rows * cols, rows=rows, cols=cols, spacing_x_mm=pitch, spacing_y_mm=pitch,
            height_mm=14.4, length_mm=127.76, width_mm=85.48, well_depth_mm=10, well_diameter_mm=3))
    definitions.append(LabwareDefinition(id="rack", name="Synthetic ST rack", kind="sbs_plate", base_class="tip_box",
        wells=384, rows=16, cols=24, spacing_x_mm=4.5, spacing_y_mm=4.5, height_mm=50,
        length_mm=127.76, width_mm=85.48, tip_definition_id="st_10ul", supported_tip_ids=["st_10ul"]))
    robot._labware_catalog = InMemoryLabwareCatalog(definitions)
    monkeypatch.setattr(server, "_bravo", robot)
    monkeypatch.setattr(Bravo, "_tip_length_for_labware", lambda *args: 26.1)
    def forbidden(*args, **kwargs):
        raise AssertionError("A design-time lookup must not connect or move any controller")
    monkeypatch.setattr(Bravo, "connect", forbidden)
    return robot


def workflow(mode="all_barrels", plate="plate384", **counts):
    nodes = [
        {"id": 1, "type": "flow/Start", "properties": {}},
        {"id": 2, "type": "tips/TipsOn", "properties": {"location": 1, "head_mode": {
            "subset_type": mode, "subset_config": "back_left", **counts}}},
        {"id": 3, "type": "liquid/Aspirate", "properties": {"location": 5, "anchor": "A1", "volume": 5}},
        {"id": 4, "type": "liquid/Dispense", "properties": {"location": 5, "anchor": "A1", "volume": 5}},
        {"id": 5, "type": "flow/End", "properties": {}},
    ]
    return {"name": "Anchor fixture", "deck": {"1": [{"labware_id": "rack"}], "5": [{"labware_id": plate}]},
        "graph": {"nodes": nodes, "links": [[i, i, 0, i + 1, 0, -1] for i in range(1, 5)]}}


async def options(data, node_id=3):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/workflows/plate_selection_options", json={"workflow": data, "node_id": node_id})
    assert response.status_code == 200, response.text
    return response.json()


async def test_full384_on384_exposes_only_a1_and_leaves_saved_and_live_state_unchanged(live):
    data = workflow()
    before = deepcopy(data)
    state = live.get_state()
    report = await options(data)
    assert report["status"] == "resolved", report
    assert [a["anchor"] for a in report["legal_anchors"]] == ["A1"]
    assert len(report["legal_anchors"][0]["covered_wells"]) == 384
    assert report["footprint"]["tip_node_id"] == 2
    assert report["motion_performed"] is False
    assert data == before and live.get_state() == state and not live.is_connected
    assert [a["anchor"] for a in (await options(data, 4))["legal_anchors"]] == ["A1"]


async def test_full384_on1536_has_four_interleaved_384_well_placements(live):
    report = await options(workflow(plate="plate1536"))
    assert {a["anchor"] for a in report["legal_anchors"]} == {"A1", "A2", "B1", "B2"}
    all_wells = [w["well"] for a in report["legal_anchors"] for w in a["covered_wells"]]
    assert len(all_wells) == len(set(all_wells)) == 1536
    assert "AF48" in all_wells


@pytest.mark.parametrize("mode,counts,expected,footprint", [
    ("column", {"column_count": 1}, 24, 16),
    ("row", {"row_count": 1}, 16, 24),
    ("single_barrel", {}, 384, 1),
    ("rectangle", {"row_count": 3, "column_count": 4}, 294, 12),
])
async def test_subset_placements_cover_the_plate_instead_of_quadrant_shortcuts(live, mode, counts, expected, footprint):
    report = await options(workflow(mode, **counts))
    assert report["status"] == "resolved", report
    assert len(report["legal_anchors"]) == expected
    assert all(len(a["covered_wells"]) == footprint for a in report["legal_anchors"])
    union = {(w["row"], w["col"]) for a in report["legal_anchors"] for w in a["covered_wells"]}
    assert len(union) == 384


async def test_single384_tip_can_address_96_plate_and_double_letter1536_rows(live):
    assert len((await options(workflow("single_barrel", "plate96")))["legal_anchors"]) == 96
    report = await options(workflow("single_barrel", "plate1536"))
    assert len(report["legal_anchors"]) == 1536
    assert report["legal_anchors"][-1] == {"anchor": "AF48", "row": 31, "col": 47,
        "covered_wells": [{"row": 31, "col": 47, "well": "AF48"}]}


async def test_native_travel_and_neighbor_constraints_filter_otherwise_fitting_anchors(live):
    data = workflow("column", column_count=1)
    baseline = await options(data)
    assert len(baseline["legal_anchors"]) == 24
    live.profile.axes["X"].range = replace(live.profile.axes["X"].range,
        max_pos=live.profile.teachpoints.get_teachpoint(5, server.Axis.X) + 10)
    limited = await options(data)
    assert [a["anchor"] for a in limited["legal_anchors"]] == ["A1", "A2", "A3"]
    live.profile.axes["X"].range = replace(live.profile.axes["X"].range, max_pos=600)
    data["deck"]["6"] = [{"labware_id": "rack"}]
    neighbor = await options(data)
    assert 0 < len(neighbor["legal_anchors"]) < 24


async def test_tiprack_cannot_be_offered_as_a_liquid_plate(live):
    data = workflow()
    data["graph"]["nodes"][2]["properties"]["location"] = 1
    report = await options(data)
    assert report["status"] == "unresolved" and report["legal_anchors"] == []
    assert "plate-style labware" in report["message"]


async def test_dynamic_target_does_not_guess_a_plate(live):
    data = workflow()
    data["graph"]["nodes"][2]["properties"]["location"] = "var:destination"
    report = await options(data)
    assert report["status"] == "unresolved" and report["legal_anchors"] == []


@pytest.mark.parametrize("head,tip", [
    ("HT_384_D_70", "st_30ul"),
    ("HT_96_D_70", "st_10ul"),
])
async def test_changed_virtual_head_or_tip_cannot_inherit_live_pickup_mode(live, monkeypatch, head, tip):
    live.profile.head.teach_tip_id = "st_10ul"
    live.profile.head.default_tip_id = "st_10ul"
    live.set_head_mode("column", "back_left", column_count=1)
    data = workflow()
    data["graph"]["nodes"][1]["properties"].pop("head_mode")
    data["protocol_simulation_target"] = {"machine_id": "fixture", "head_type": head, "tip_definition_id": tip}

    def apply_target(profile, target):
        profile.head.head_type = HeadType[target["head_type"]]
        profile.head.teach_tip_id = profile.head.default_tip_id = target["tip_definition_id"]
        return target

    monkeypatch.setattr(server, "_apply_generated_simulation_target", apply_target)
    report = await options(data)
    assert report["status"] == "unresolved" and report["legal_anchors"] == []
    assert "Configure the Tips On footprint" in report["message"]
    assert live.head_mode.subset_type == "column"
    assert live.profile.head.teach_tip_id == "st_10ul"


async def test_unchanged_virtual_target_preserves_inherited_live_pickup_mode(live, monkeypatch):
    live.profile.head.teach_tip_id = "st_10ul"
    live.set_head_mode("column", "back_left", column_count=1)
    data = workflow()
    data["graph"]["nodes"][1]["properties"].pop("head_mode")
    data["protocol_simulation_target"] = {"machine_id": "fixture", "head_type": "HT_384_D_70", "tip_definition_id": "st_10ul"}
    monkeypatch.setattr(server, "_apply_generated_simulation_target", lambda profile, target: target)
    report = await options(data)
    assert report["status"] == "resolved", report
    assert report["footprint"]["head_mode"]["subset_type"] == "column"
    assert len(report["legal_anchors"]) == 24
