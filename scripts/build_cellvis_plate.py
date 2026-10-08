"""Build the Cellvis P384-1.5H-N visualization from its published drawing.

Source: https://www.cellvis.com/images/glass_bottom_plate_384_well_size.png
Product: https://www.cellvis.com/product_detail.php?product_id=53
Dimensions are mm. This is a dimensional well model, not vendor CAD.
"""
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "labware/editor_assets/lw-34358f93e2a0/Cellvis_P384_1.5H_N.glb"


def build_plate():
    vertices, faces = [], []

    def quad(points):
        start = len(vertices)
        vertices.extend(points)
        faces.extend(((start, start + 1, start + 2), (start, start + 2, start + 3)))

    def rectangle(x0, x1, y0, y1, z):
        return [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]

    def ring(outer, inner):
        for i in range(4):
            j = (i + 1) % 4
            quad([outer[i], outer[j], inner[j], inner[i]])

    # Top web and tapered square wells: 3.7 mm at the rim, 3.3 mm
    # at the glass, with an 11.38 mm inside depth. A1 centers are
    # 12.05/9.05 mm from the outside edges, giving a concentric grid.
    for row in range(16):
        y = -85.6 / 2 + 9.05 + row * 4.5
        for col in range(24):
            x = -127.6 / 2 + 12.05 + col * 4.5
            x0, x1 = (-63.8 if col == 0 else x - 2.25), (63.8 if col == 23 else x + 2.25)
            y0, y1 = (-42.8 if row == 0 else y - 2.25), (42.8 if row == 15 else y + 2.25)
            outer = rectangle(x0, x1, y0, y1, 14.33)
            top = rectangle(x - 1.85, x + 1.85, y - 1.85, y + 1.85, 14.33)
            bottom = rectangle(x - 1.65, x + 1.65, y - 1.65, y + 1.65, 2.95)
            ring(outer, top)
            ring(top, bottom)
    # The outside skirt establishes the actual deck datum at z=0.
    ring(rectangle(-63.8, 63.8, -42.8, 42.8, 0),
         rectangle(-63.8, 63.8, -42.8, 42.8, 14.33))
    frame = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    frame.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
        name="black polystyrene frame", baseColorFactor=[35, 39, 47, 255],
        metallicFactor=0.0, roughnessFactor=0.65, doubleSided=True,
    ))
    glass = trimesh.creation.box(extents=[108, 72, 0.17])
    glass.apply_translation([0, 0, 2.865])
    glass.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
        name="cover glass", baseColorFactor=[178, 201, 208, 180],
        metallicFactor=0.0, roughnessFactor=0.18, alphaMode="BLEND", doubleSided=True,
    ))
    scene = trimesh.Scene({"frame and tapered wells": frame, "cover glass": glass})
    # glTF is Y-up; the shared labware viewer converts it back to deck Z-up.
    matrix = np.eye(4)
    matrix[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    scene.apply_transform(matrix)
    return scene


if __name__ == "__main__":
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    MODEL.write_bytes(build_plate().export(file_type="glb"))
    print(MODEL)
