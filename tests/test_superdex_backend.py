"""Focused headless contact checks against the actual SuperDex engine."""

import pytest

pytest.importorskip("superdex.physics")

from pybravo.physics import SuperDexCollisionBackend


def test_box_contact_resets_requested_pose_between_queries():
    with SuperDexCollisionBackend() as backend:
        backend.add_box("head", (0.1, 0, 0), (0.01, 0.01, 0.01), movable=True)
        backend.add_box("plate", (0, 0, 0), (0.01, 0.01, 0.01))

        assert backend.contacts() == []
        backend.set_pose("head", (0.015, 0, 0))
        hits = backend.contacts()
        assert len(hits) == 1
        assert (hits[0].body_a, hits[0].body_b) == ("head", "plate")
        assert hits[0].penetration_m > 0.001

        # Contact resolution must not change the next commanded pose.
        assert backend.contacts()[0].penetration_m > 0.001
        backend.set_pose("head", (0.1, 0, 0))
        assert backend.contacts() == []


def test_movable_pairs_and_explicit_assembly_exclusions():
    with SuperDexCollisionBackend() as backend:
        backend.add_box("head", (0, 0, 0), (0.01, 0.01, 0.01), movable=True)
        backend.add_box("gripper", (0.015, 0, 0), (0.01, 0.01, 0.01), movable=True)
        assert len(backend.contacts()) == 1
        assert backend.contacts(exclude_pairs=[("head", "gripper")]) == []
        assert len(backend.contacts(include_pairs=[("head", "gripper")])) == 1


def _rectangular_frame_mesh():
    """Watertight extruded ring: bounding boxes overlap the clear center."""
    outer = [(-0.02, -0.02), (0.02, -0.02), (0.02, 0.02), (-0.02, 0.02)]
    inner = [(-0.008, -0.008), (0.008, -0.008), (0.008, 0.008), (-0.008, 0.008)]
    vertices = [
        (x, y, z)
        for z in (-0.005, 0.005)
        for ring in (outer, inner)
        for x, y in ring
    ]
    triangles = []
    for i in range(4):
        j = (i + 1) % 4
        triangles.extend(
            [
                (8 + i, 8 + j, 12 + j), (8 + i, 12 + j, 12 + i),
                (i, 4 + j, j), (i, 4 + i, 4 + j),
                (i, j, 8 + j), (i, 8 + j, 8 + i),
                (4 + i, 12 + j, 4 + j), (4 + i, 12 + i, 12 + j),
            ]
        )
    return vertices, triangles


def test_sdf_mesh_preserves_concave_clearance():
    vertices, triangles = _rectangular_frame_mesh()
    with SuperDexCollisionBackend() as backend:
        backend.add_mesh("frame", vertices, triangles)
        backend.add_box("probe", (0, 0, 0), (0.003, 0.003, 0.003), movable=True)

        assert backend.contacts() == []
        backend.set_pose("probe", (0.01, 0, 0))
        hits = backend.contacts()
        assert len(hits) == 1
        assert hits[0].penetration_m > 0.001
        backend.set_pose("probe", (0, 0, 0))
        assert backend.contacts() == []


def test_invalid_geometry_rejected_before_scene_mutation():
    with SuperDexCollisionBackend() as backend:
        with pytest.raises(ValueError, match="positive"):
            backend.add_box("bad", (0, 0, 0), (0.01, 0, 0.01))
        backend.add_box("good", (0, 0, 0), (0.01, 0.01, 0.01))
        with pytest.raises(ValueError, match="unique"):
            backend.add_box("good", (0, 0, 0), (0.01, 0.01, 0.01))
