"""CAD identity must describe the meshes actually used by the scene."""

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.physics import scene as scene_module


@pytest.mark.parametrize("wells", [96, 384, 1536])
def test_cad_grasp_pads_center_on_native_pickup_plate(wells):
    """A plausible silhouette must also grip the plate at native pickup X.

    PickPlace moves to the deck's recorded X teachpoint. Centered SBS grids of
    all three formats therefore share a plate center 49.5 mm to its right.
    Check the actual inward-facing CAD contact flats, not a fitted bounding
    box around the whole finger or an independently drawn collision proxy.
    """
    import numpy as np

    from pybravo.deck.geometry import well_geometry_from_metadata

    plate = well_geometry_from_metadata({"wells": wells})
    plate_center_x = (plate.cols - 1) * plate.pitch_x_mm / 2 - plate.offset_x_mm
    geometry = scene_module._tool_geometry(26.1)
    for name in ("fingerleft_fingerleft", "fingerright_fingerright"):
        vertices, _, _ = geometry[name]
        # The two raised grasp pads are the extremal faces toward the opposing
        # finger; their eight-mm contact strips must straddle the plate center.
        inward_y = vertices[:, 1].min() if "left" in name else vertices[:, 1].max()
        contact = vertices[np.isclose(vertices[:, 1], inward_y, atol=1e-6)] * 1000
        assert len(contact) >= 8
        pad_center_x = (contact[:, 0].min() + contact[:, 0].max()) / 2
        assert pad_center_x == pytest.approx(plate_center_x, abs=0.01)
        assert contact[:, 0].min() < plate_center_x < contact[:, 0].max()


def test_cad_gripper_carriage_nests_alongside_head_without_lateral_air_gap():
    """The real assembly is adjacent to the head, not floating to its right."""
    geometry = scene_module._tool_geometry(26.1)
    head = geometry["384_head_384_head"][0] * 1000
    carriage = geometry["gripperzaxis_gripperzaxis"][0] * 1000
    assert carriage[:, 0].min() < head[:, 0].max()
    assert 0 < carriage[:, 0].max() - head[:, 0].max() < 8


def test_tool_geometry_cache_reloads_after_robot_assets_change(monkeypatch):
    original = scene_module.trimesh.load
    loaded = []

    def record_load(path, **options):
        loaded.append(path)
        return original(path, **options)

    revision = ["first-cad-revision"]
    monkeypatch.setattr(scene_module, "robot_assets_sha256", lambda path: revision[0])
    monkeypatch.setattr(scene_module.trimesh, "load", record_load)
    scene_module._load_tool_geometry.cache_clear()
    first = scene_module._tool_geometry(26.1)
    assert len(loaded) == len(first) > 0
    assert scene_module._tool_geometry(26.1) is first
    assert len(loaded) == len(first)
    revision[0] = "changed-cad-revision"
    second = scene_module._tool_geometry(26.1)
    assert second is not first
    assert len(loaded) == 2 * len(first)


def test_scene_rejects_asset_change_before_checking_motion(monkeypatch):
    bravo = Bravo(mode="simulation")
    bravo.connect()
    scene = scene_module.BravoCollisionScene(bravo)
    try:
        start = bravo.get_all_positions()
        assert scene.provenance["robot_assets_sha256"] == scene_module.robot_assets_sha256(scene_module._URDF)
        monkeypatch.setattr(scene_module, "robot_assets_sha256", lambda path: "changed-after-scene-created")
        with pytest.raises(scene_module.PhysicalSimulationError, match="assets changed during physical rehearsal"):
            scene.check_motion(start, start)
        assert scene.moves_checked == 0
        assert scene.samples_checked == 0
        assert scene.report()["status"] == "failed"
        assert bravo.get_all_positions() == start
    finally:
        scene.close()
        bravo.disconnect()
