"""CAD identity must describe the meshes actually used by the scene."""

import pytest

pytest.importorskip("superdex.physics")
pytest.importorskip("trimesh")
pytest.importorskip("scipy")

from pybravo.bravo import Bravo
from pybravo.physics import scene as scene_module


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
