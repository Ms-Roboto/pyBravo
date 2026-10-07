"""Collision asset identity is stable, cached, and sensitive to real edits."""

import hashlib
import os

from pybravo.physics.assets import _digest_snapshot, robot_assets_sha256


def asset_tree(tmp_path):
    urdf = tmp_path / "robot.urdf"
    urdf.write_bytes(b"synthetic robot")
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "b.stl").write_bytes(b"second mesh")
    (folder / "a.stl").write_bytes(b"first mesh")
    return urdf


def test_asset_digest_preserves_scene_provenance_algorithm_and_reuses_unchanged_bytes(tmp_path):
    urdf = asset_tree(tmp_path)
    expected = hashlib.sha256(urdf.read_bytes())
    for path in sorted((tmp_path / "assets").glob("*.stl")):
        expected.update(path.name.encode())
        expected.update(path.read_bytes())
    _digest_snapshot.cache_clear()
    assert robot_assets_sha256(urdf) == expected.hexdigest()
    before = _digest_snapshot.cache_info()
    assert robot_assets_sha256(urdf) == expected.hexdigest()
    after = _digest_snapshot.cache_info()
    assert after.misses == before.misses
    assert after.hits == before.hits + 1


def test_same_size_asset_edit_with_restored_mtime_invalidates_identity(tmp_path):
    urdf = asset_tree(tmp_path)
    mesh = tmp_path / "assets/a.stl"
    before = robot_assets_sha256(urdf)
    stat = mesh.stat()
    mesh.write_bytes(b"other mesh")
    assert mesh.stat().st_size == stat.st_size
    os.utime(mesh, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert mesh.stat().st_mtime_ns == stat.st_mtime_ns
    assert robot_assets_sha256(urdf) != before


def test_added_removed_and_replaced_assets_invalidate_identity(tmp_path):
    urdf = asset_tree(tmp_path)
    before = robot_assets_sha256(urdf)
    extra = tmp_path / "assets/new.stl"
    extra.write_bytes(b"new solid")
    assert robot_assets_sha256(urdf) != before
    extra.unlink()
    assert robot_assets_sha256(urdf) == before
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement robot")
    replacement.replace(urdf)
    assert robot_assets_sha256(urdf) != before
