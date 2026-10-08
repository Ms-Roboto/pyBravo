"""The shipped Cellvis model must expose the same well depth as motion uses."""

from pathlib import Path

import pytest
import yaml

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")

ROOT = Path(__file__).resolve().parents[1]
PLATE_ID = "lw-34358f93e2a0"
MODEL_URL = f"/labware-assets/{PLATE_ID}/Cellvis_P384_1.5H_N.glb"


@pytest.fixture(scope="module")
def plate():
    scene = trimesh.load(
        ROOT / "labware/editor_assets" / PLATE_ID / "Cellvis_P384_1.5H_N.glb",
        force="scene",
    )
    # Apply the same Y-up glTF -> Z-up deck rotation as the labware viewer.
    rotation = np.eye(4)
    rotation[:3, :3] = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    scene.apply_transform(rotation)
    return scene.to_geometry()


def first_surface_z(mesh, xy):
    """Intersect vertical rays with actual triangles, without optional rtree."""
    triangles = mesh.triangles
    a = triangles[:, 0]
    ab, ac = triangles[:, 1] - a, triangles[:, 2] - a
    determinant = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    usable = np.abs(determinant) > 1e-10
    a, ab, ac, determinant = a[usable], ab[usable], ac[usable], determinant[usable]
    hits = []
    for x, y in xy:
        px, py = x - a[:, 0], y - a[:, 1]
        u = (px * ac[:, 1] - py * ac[:, 0]) / determinant
        v = (ab[:, 0] * py - ab[:, 1] * px) / determinant
        inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6)
        assert inside.any(), f"No plate surface below ({x}, {y})"
        hits.append(np.max((a[:, 2] + u * ab[:, 2] + v * ac[:, 2])[inside]))
    return np.asarray(hits)


def test_cellvis_asset_matches_both_catalogs_and_physical_grid(plate):
    editor = yaml.safe_load((ROOT / "config/labware_editor.yaml").read_text())
    entry = next(row for row in editor["labware_types"] if row["labware_type_id"] == PLATE_ID)
    snapshot = yaml.safe_load((ROOT / "config/labware_catalog.snapshot.yaml").read_text())
    catalog = next(row for row in snapshot["labware"] if row["id"] == PLATE_ID)
    assert entry["model_3d"] == catalog["model_3d"] == MODEL_URL
    expected_size = np.array([127.6, 85.6, 14.33])
    assert plate.extents == pytest.approx(expected_size, abs=1e-5)
    assert plate.bounds[0, 2] == pytest.approx(0)
    for axis, size in zip(("length", "width", "height"), expected_size):
        assert catalog[f"{axis}_mm"] == entry["plate_dimensions_mm"][f"{axis}_mm"] == size
    wells = entry["well_dimensions_mm"]
    assert catalog["well_depth_mm"] == wells["depth_mm"] == 11.38
    assert catalog["well_diameter_mm"] == wells["diameter_mm"] == 3.3
    assert (wells["rows"], wells["cols"]) == (16, 24)
    for axis, count, size, physical_a1 in (("x", 24, 127.6, 12.05), ("y", 16, 85.6, 9.05)):
        assert catalog[f"spacing_{axis}_mm"] == wells[f"spacing_{axis}_mm"] == 4.5
        assert (size - (count - 1) * 4.5) / 2 == pytest.approx(physical_a1)
        # Native offsets reference the taught 96-well A1, not the exterior edge.
        assert catalog[f"offset_{axis}_mm"] == wells[f"offset_{axis}_mm"] == 2.25


def test_all_384_wells_are_open_to_the_inside_glass_bottom(plate):
    centers = [(col * 4.5 - 51.75, row * 4.5 - 33.75) for row in range(16) for col in range(24)]
    # A solid top/lid or an incorrectly raised well floor makes this fail.
    assert first_surface_z(plate, centers) == pytest.approx(np.full(384, 14.33 - 11.38), abs=1e-5)


def test_cellvis_square_taper_and_glass_have_published_dimensions(plate):
    x, y = -51.75, -33.75
    # A near-corner ray reaches the glass: the opening is square, not circular.
    assert first_surface_z(plate, [(x + 1.6, y + 1.6)])[0] == pytest.approx(2.95, abs=1e-5)
    # The taper narrows from 3.7 mm at the rim to 3.3 mm at the glass.
    taper_z = 2.95 + 11.38 * (1.8 - 1.65) / (1.85 - 1.65)
    assert first_surface_z(plate, [(x + 1.8, y)])[0] == pytest.approx(taper_z, abs=1e-4)
    assert first_surface_z(plate, [(x + 1.9, y)])[0] == pytest.approx(14.33, abs=1e-5)
    # The underside of the #1.5H cover glass is 0.17 mm below the well floor.
    bottom_surface = -first_surface_z(
        trimesh.Trimesh(vertices=plate.vertices * [1, 1, -1], faces=plate.faces, process=False),
        [(x, y)],
    )[0]
    assert 2.95 - bottom_surface == pytest.approx(0.17, abs=1e-5)
