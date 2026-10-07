"""Collision rehearsal of Bravo tool motion against a catalog-backed deck.

The scene uses the URDF's closed collision meshes for the tool and gripper,
with teachpoint-relative labware envelopes. It samples actual controller moves,
not the Designer's illustrative animation. Envelopes are conservative: this is
not calibration, fluid dynamics, or a hardware-release certificate.
"""

from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from scipy.spatial.transform import Rotation

from pybravo.deck.geometry import well_center_offset_from_teachpoint_mm, well_geometry_from_metadata
from pybravo.head_mode import active_head_wells, head_geometry_for_type
from pybravo.types import Axis

from .carrying import active_grasp_location, carried_plate_poses
from .errors import PhysicalSimulationError
from .superdex_backend import SuperDexCollisionBackend

_URDF = Path(__file__).resolve().parents[1] / "model/pybravo_urdf/robot.urdf"
_TOOL_LINKS = {"384_head_384_head", "fingerleft_fingerleft", "fingerright_fingerright", "gripperzaxis_gripperzaxis"}


def _origin(element):
    matrix = np.eye(4)
    if element is not None:
        matrix[:3, 3] = np.fromstring(element.get("xyz", "0 0 0"), sep=" ")
        matrix[:3, :3] = Rotation.from_euler("xyz", np.fromstring(element.get("rpy", "0 0 0"), sep=" ")).as_matrix()
    return matrix


@lru_cache(maxsize=8)
def _tool_geometry(teach_length_mm: float):
    """Read collision assets and establish a tool A1/barrel datum in mm.

    Joint datums match the documented digital twin; they never change commands.
    STL meshes are reflected into machine +X/+Y/Z-up and winding is reversed.
    """
    root = ET.parse(_URDF).getroot()
    joints = list(root.findall("joint"))
    values = {
        "xaxis": -182.64 / 1000,
        "yaxis": -2.3 / 1000,
        "zaxis": teach_length_mm / 1000,
        "zaxis-gripper": (-20 + 25 - teach_length_mm) / 1000,
    }
    transforms = {"base_link": np.eye(4)}
    axes = {"base_link": set()}
    remaining = list(joints)
    while remaining:
        progressed = False
        for joint in list(remaining):
            parent, child = joint.find("parent").get("link"), joint.find("child").get("link")
            if parent not in transforms:
                continue
            matrix = _origin(joint.find("origin"))
            if child in {"384_head", "gripperzaxis"}:
                matrix[1, 3] -= 0.006
            if joint.get("name") in {"ygripper-left", "ygripper-right"}:
                matrix[1, 3] -= 0.010
            shift = np.eye(4)
            shift[:3, 3] = np.fromstring(joint.find("axis").get("xyz"), sep=" ") * values.get(joint.get("name"), 0)
            transforms[child] = transforms[parent] @ matrix @ shift
            axes[child] = axes[parent] | ({joint.get("name")} if joint.get("type") == "prismatic" else set())
            remaining.remove(joint)
            progressed = True
        if not progressed:
            raise PhysicalSimulationError("URDF joint tree is disconnected")
    meshes = {}
    for link in root.findall("link"):
        name = link.get("name")
        if name not in _TOOL_LINKS:
            continue
        collision = link.find("collision")
        filename = collision.find("geometry/mesh").get("filename")
        path = _URDF.parent / "assets" / Path(filename).name
        mesh = trimesh.load(path, force="mesh")
        if not mesh.is_watertight or not mesh.is_winding_consistent:
            raise PhysicalSimulationError(f"Collision mesh {name} is not closed and consistently wound")
        matrix = transforms[name] @ _origin(collision.find("origin"))
        if name in {"384_head_384_head", "gripperzaxis_gripperzaxis"}:
            matrix[:3, 3] += transforms[name][:3, :3] @ np.array([0, 0.008, 0])
        if "finger" in name:
            matrix[:3, 3] += transforms[name][:3, :3] @ np.array([0, 0, 0.012])
        mesh.apply_transform(matrix)
        meshes[name] = mesh
    head = meshes["384_head_384_head"]
    bottom = head.vertices[head.vertices[:, 2] <= head.bounds[0, 2] + 0.005]
    center = (bottom.min(axis=0) + bottom.max(axis=0)) / 2
    # The existing mesh is a 384 head; its barrel array is 24x16 at 4.5 mm.
    a1 = np.array([center[0] - 0.05175, center[1] + 0.03375, head.bounds[0, 2]])
    result = {}
    for name, mesh in meshes.items():
        vertices = (mesh.vertices - a1) * np.array([1, -1, 1])
        vertices[:, 2] += teach_length_mm / 1000
        result[name] = (vertices, mesh.faces[:, ::-1], axes[name])
    return result


@dataclass
class _Obstacle:
    name: str
    location: int
    labware: Any
    lower_mm: np.ndarray
    upper_mm: np.ndarray
    top_item: bool


class BravoCollisionScene:
    """Fail before accepting a sampled command that intersects modeled solids."""

    def __init__(self, bravo, *, sample_spacing_mm: float = 0.5):
        if not math.isfinite(sample_spacing_mm) or not 0 < sample_spacing_mm <= 1:
            raise ValueError("Physical sampling spacing must be >0 and <=1 mm")
        self.bravo = bravo
        self.spacing = sample_spacing_mm
        self.backend = SuperDexCollisionBackend()
        self.context: dict[str, Any] = {}
        self.moves_checked = 0
        self.samples_checked = 0
        self.contact_queries = 0
        self.last_error: dict[str, Any] | None = None
        self.obstacles: list[_Obstacle] = []
        self.robot_bounds = {}
        self.tool_axes = {}
        self._known_labware = {}
        self._metadata_digests = {}
        self._profile_snapshot = json.dumps(bravo.profile._to_dict(), sort_keys=True)
        self._teachpoint_snapshot = tuple(
            bravo._teachpoints.get_teachpoint(loc, axis) for loc in range(1, 10) for axis in (Axis.X, Axis.Y, Axis.Z)
        )
        self.teach_length = float(bravo.profile.head.teach_tip_length_mm or 0)
        if not math.isfinite(self.teach_length) or self.teach_length <= 0:
            self.close()
            raise PhysicalSimulationError("A recorded teach-tip length is required for physical simulation")
        try:
            accessories = bravo.profile.accessories
            if any(device.enabled for device in accessories.devices) or accessories.barcode_reader.enabled:
                raise PhysicalSimulationError("Enabled accessories need collision geometry before physical rehearsal")
            geometry = _tool_geometry(self.teach_length)
            self.mesh_head = bravo.profile.head.head_type.name in {"HT_384_D_70", "HT_384_D_70_S2"}
            for link, (vertices, faces, axes) in geometry.items():
                if "finger" in link:
                    # Tie the mesh's lower grasp plane to the recorded paired
                    # gripper calibration. This is a collision-scene datum;
                    # it does not modify commanded hardware teachpoints.
                    vertices = vertices.copy()
                    g = bravo.profile.gripper
                    plane = float(g.pad_zg_reference_mm) + self.teach_length - float(g.pad_reference_tip_length_mm) + 20
                    vertices[:, 2] += plane / 1000 - vertices[:, 2].min()
                if link == "384_head_384_head" and not self.mesh_head:
                    # Other head housings have no separate CAD export; conservative
                    # envelope uses the existing mechanical footprint constants.
                    from pybravo.state_machine.tasks import _full_head_footprint_bounds_mm

                    x0, x1, y0, y1 = _full_head_footprint_bounds_mm(
                        bravo.profile.head.head_type, 0, 0, gripper_present=False
                    )
                    lower = np.array([x0, y0, self.teach_length]) / 1000
                    upper = np.array([x1, y1, self.teach_length + 296]) / 1000
                    self.backend.add_box("robot/" + link, (lower + upper) / 2, (upper - lower) / 2, movable=True)
                else:
                    self.backend.add_mesh("robot/" + link, vertices.tolist(), faces.tolist(), movable=True)
                    lower, upper = vertices.min(axis=0), vertices.max(axis=0)
                self.robot_bounds["robot/" + link] = (lower * 1000, upper * 1000)
                self.tool_axes["robot/" + link] = axes
            self._add_deck()
            digest = hashlib.sha256(_URDF.read_bytes())
            for path in sorted((_URDF.parent / "assets").glob("*.stl")):
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
            self.provenance = {
                "robot_assets_sha256": digest.hexdigest(),
                "profile_sha256": hashlib.sha256(
                    json.dumps(bravo.profile._to_dict(), sort_keys=True).encode()
                ).hexdigest(),
                "deck_sha256": hashlib.sha256(json.dumps(self._geometry_record(), sort_keys=True).encode()).hexdigest(),
            }
        except Exception:
            self.close()
            raise

    def close(self):
        self.backend.close()

    def set_context(self, node_id, node_type, properties):
        self.context = {"node_id": node_id, "node_type": node_type, "properties": dict(properties)}
        if json.dumps(self.bravo.profile._to_dict(), sort_keys=True) != self._profile_snapshot:
            self._fail("Profile calibration changed during physical rehearsal; rebuild the scene")
        if node_type in {"plate/Delid", "plate/Relid"}:
            self._fail("Lid collision geometry is not configured; no physical clearance result can be granted.")
        self._refresh_deck()

    def _add_deck(self):
        for loc in range(1, 10):
            try:
                tx = self.bravo._teachpoints.get_teachpoint(loc, Axis.X)
                ty = self.bravo._teachpoints.get_teachpoint(loc, Axis.Y)
                tz = self.bravo._teachpoints.get_teachpoint(loc, Axis.Z)
            except (KeyError, ValueError):
                raise PhysicalSimulationError(f"Position {loc} lacks a teachpoint")
            if not all(math.isfinite(v) for v in (tx, ty, tz)):
                raise PhysicalSimulationError(f"Position {loc} has a nonfinite teachpoint")
            # Known platepad footprint and deck plane; do not render a cosmetic
            # STL offset as an alternative physical teachpoint.
            lower = np.array([tx - 17.12, ty - 13.97, -tz - 35.08])
            upper = np.array([tx + 116.10, ty + 76.96, -tz])
            self._add_obstacle(f"deck/{loc}", loc, None, lower, upper, False)
            stack = self.bravo._deck.get_stack(loc)
            support = -tz
            for idx, item in enumerate(stack.items):
                dims = (float(item.length), float(item.width), float(item.height))
                if not all(math.isfinite(v) and v > 0 for v in dims):
                    raise PhysicalSimulationError(f"Position {loc}: {item.name} lacks positive collision dimensions")
                md = item.metadata or {}
                offset_x = float(md.get("offset_x_mm") or 0)
                offset_y = float(md.get("offset_y_mm") or 0)
                wg = well_geometry_from_metadata(md)
                if wg.rows > 0 and wg.cols > 0:
                    # Catalog offsets are relative to the taught well anchor,
                    # not the exterior SBS edge. Center the recorded well grid
                    # inside the recorded body dimensions.
                    offset_x += (dims[0] - (wg.cols - 1) * wg.pitch_x_mm) / 2
                    offset_y += (dims[1] - (wg.rows - 1) * wg.pitch_y_mm) / 2
                lower = np.array([tx - offset_x, ty - offset_y, support])
                upper = lower + np.array(dims)
                self._add_obstacle(
                    f"labware/{loc}/{idx}/{item.name}", loc, item, lower, upper, idx == len(stack.items) - 1
                )
                self._known_labware[id(item)] = self.obstacles[-1]
                self._metadata_digests[id(item)] = json.dumps(item.metadata, sort_keys=True)
                if item.is_lidded or item.is_sealed:
                    raise PhysicalSimulationError(
                        f"{item.name} is lidded/sealed; accessible well and lid collision geometry is required"
                    )
                support += float(item.stack_height or item.height)

    def _add_obstacle(self, name, loc, item, lower, upper, top_item):
        self.backend.add_box(name, (lower + upper) / 2000, (upper - lower) / 2000, movable=item is not None)
        self.obstacles.append(_Obstacle(name, loc, item, lower, upper, top_item))

    def _geometry_record(self):
        return [
            {
                "body": o.name,
                "catalog_id": getattr(o.labware, "id", None),
                "lower_mm": o.lower_mm.tolist(),
                "upper_mm": o.upper_mm.tolist(),
                "metadata": getattr(o.labware, "metadata", {}),
            }
            for o in self.obstacles
        ]

    def _refresh_deck(self):
        """Follow known plate moves; never substitute dimensions for new bodies."""
        teachpoints = tuple(
            self.bravo._teachpoints.get_teachpoint(loc, axis)
            for loc in range(1, 10)
            for axis in (Axis.X, Axis.Y, Axis.Z)
        )
        if teachpoints != self._teachpoint_snapshot:
            self._fail("Teachpoint geometry changed during physical rehearsal; rebuild the scene")
        seen = set()
        for loc in range(1, 10):
            tx = self.bravo._teachpoints.get_teachpoint(loc, Axis.X)
            ty = self.bravo._teachpoints.get_teachpoint(loc, Axis.Y)
            support = -self.bravo._teachpoints.get_teachpoint(loc, Axis.Z)
            stack = self.bravo._deck.get_stack(loc)
            for idx, item in enumerate(stack.items):
                obstacle = self._known_labware.get(id(item))
                if obstacle is None:
                    self._fail("Unmodeled labware was added during physical rehearsal; rebuild the scene")
                if json.dumps(item.metadata, sort_keys=True) != self._metadata_digests[id(item)]:
                    self._fail("Labware metadata geometry changed during physical rehearsal; rebuild the scene")
                dims = np.array([item.length, item.width, item.height], dtype=float)
                if not np.allclose(dims, obstacle.upper_mm - obstacle.lower_mm, atol=1e-6):
                    self._fail("Labware geometry changed during physical rehearsal; rebuild the scene")
                wg = well_geometry_from_metadata(item.metadata)
                ox = wg.offset_x_mm + (dims[0] - (wg.cols - 1) * wg.pitch_x_mm) / 2 if wg.cols else 0
                oy = wg.offset_y_mm + (dims[1] - (wg.rows - 1) * wg.pitch_y_mm) / 2 if wg.rows else 0
                obstacle.lower_mm = np.array([tx - ox, ty - oy, support])
                obstacle.upper_mm = obstacle.lower_mm + dims
                obstacle.location = loc
                obstacle.top_item = idx == len(stack.items) - 1
                support += float(item.stack_height or item.height)
                seen.add(id(item))
        if seen != set(self._known_labware):
            self._fail("Labware disappeared from the modeled deck; rebuild the physical rehearsal")

    def _shift(self, axes, pose):
        # URDF prismatic chain has constant orientations, so rigid tool meshes
        # translate without rotating. G is half-travel for each opposed finger.
        shift = np.array([pose[Axis.X], pose[Axis.Y], 0.0])
        if "zaxis" in axes:
            shift[2] -= pose[Axis.Z]
        if "zaxis-gripper" in axes:
            shift[2] -= pose[Axis.Z] + pose[Axis.Zg] + 20
        if "ygripper-left" in axes:
            shift[1] -= pose[Axis.G] / 2
        if "ygripper-right" in axes:
            shift[1] += pose[Axis.G] / 2
        return shift

    def _fail(self, message, **details):
        self.last_error = {**self.context, **details, "message": message}
        raise PhysicalSimulationError(message, details=self.last_error)

    def check_motion(self, start, end):
        if not all(math.isfinite(float(v)) for v in (*start.values(), *end.values())):
            self._fail("Nonfinite motion cannot be physically simulated")
        for axis in (Axis.X, Axis.Y, Axis.Z, Axis.Zg, Axis.G):
            limits = self.bravo.profile.axes[axis.name].range
            # Some profiles record a home just outside the operating range.
            # A native homing primitive may reach that recorded datum; an
            # ordinary move may only remain at a stationary zero home.
            value = end[axis]
            homing = self.context.get("node_type") in {"system/Home", "system/Initialize"}
            recorded_home = float(self.bravo.profile.axes[axis.name].homing_offset)
            if not limits.min_pos <= value <= limits.max_pos and not (
                value == start[axis] == 0 or (homing and value == recorded_home)
            ):
                self._fail(
                    f"{axis.name} target {value:g} is outside physical travel [{limits.min_pos:g}, {limits.max_pos:g}]"
                )
        travel = max(abs(end[a] - start[a]) for a in (Axis.X, Axis.Y, Axis.Z, Axis.Zg, Axis.G))
        count = max(1, math.ceil(travel / self.spacing))
        if count > 5000:
            self._fail("Physical motion exceeds the bounded sampling budget")
        for idx in range(count + 1):
            pose = {a: start[a] + (end[a] - start[a]) * idx / count for a in Axis}
            self._check_pose(pose)
            self.samples_checked += 1
        self.moves_checked += 1

    def _check_pose(self, pose):
        self._refresh_deck()
        try:
            carried = carried_plate_poses(self.bravo, pose)
            grasp_location = active_grasp_location(self.bravo)
        except PhysicalSimulationError as exc:
            self._fail(str(exc))
        bounds = {}
        for obstacle in self.obstacles:
            offset = np.array(carried.get(id(obstacle.labware), (0, 0, 0)))
            lower, upper = obstacle.lower_mm + offset, obstacle.upper_mm + offset
            bounds[obstacle.name] = (lower, upper)
            if obstacle.labware is not None:
                self.backend.set_pose(obstacle.name, (lower + upper) / 2000)
        pairs = []
        for name, (lower, upper) in self.robot_bounds.items():
            shift = self._shift(self.tool_axes[name], pose)
            self.backend.set_pose(name, shift / 1000)
            for obstacle in self.obstacles:
                ol, ou = bounds[obstacle.name]
                if np.all(upper + shift > ol) and np.all(lower + shift < ou):
                    pairs.append((name, obstacle.name))
        for moving in self.obstacles:
            if id(moving.labware) not in carried:
                continue
            lower, upper = bounds[moving.name]
            for other in self.obstacles:
                if other is moving or id(other.labware) in carried:
                    continue
                ol, ou = bounds[other.name]
                if np.all(upper > ol) and np.all(lower < ou):
                    pairs.append((moving.name, other.name))
        # Attached-tip entry is checked against actual well axes and the recorded
        # bottom, avoiding the false assertion that a plate is an opaque block.
        self._check_tips(pose)
        if not pairs:
            return
        self.contact_queries += 1
        contacts = self.backend.contacts(include_pairs=pairs, min_penetration_m=0.00005)
        for contact in contacts:
            if self._intentional_stack_nesting(contact, bounds, carried, grasp_location):
                continue
            obstacle = next(o for o in self.obstacles if o.name in (contact.body_a, contact.body_b))
            # Seating a tip is an intentional contact at its catalog rack. Only
            # the lower tooling/rack seating plane may meet; unrelated bodies,
            # lateral motion through racks, and deep penetration remain fatal.
            props = self.context.get("properties", {})
            if (
                self.context.get("node_type") == "tips/TipsOn"
                and obstacle.top_item
                and obstacle.location == int(props.get("location", -1))
                and "robot/384_head_384_head" in (contact.body_a, contact.body_b)
                and contact.penetration_m <= 0.001
            ):
                continue
            # A conservative rectangular plate envelope includes its flange.
            # Permit finger contact only at that plate's calibrated grasp band;
            # head/housing contacts and contact with neighbors remain errors.
            robot = next((b for b in (contact.body_a, contact.body_b) if b.startswith("robot/")), None)
            if robot and "/finger" in robot and obstacle.labware is not None:
                if id(obstacle.labware) in carried or (obstacle.top_item and obstacle.location == grasp_location):
                    fl, fu = self.robot_bounds[robot]
                    fs = self._shift(self.tool_axes[robot], pose)
                    ol, ou = bounds[obstacle.name]
                    flange = ol[2] + float(obstacle.labware.gripper_offset)
                    if abs(fl[2] + fs[2] - flange) <= 0.1 and contact.penetration_m <= 0.006:
                        continue
            self._fail(
                f"Physical collision: {contact.body_a} intersects {contact.body_b} ({contact.penetration_m * 1000:.2f} mm penetration).",
                bodies=[contact.body_a, contact.body_b],
                penetration_mm=contact.penetration_m * 1000,
                pose={a.name: pose[a] for a in Axis},
            )

    def _intentional_stack_nesting(self, contact, bounds, carried, grasp_location):
        """Allow only the catalog's aligned nesting overlap at pick/place.

        Solid envelopes overlap by height minus stacking thickness. That is
        intentional for an aligned plate on its recorded support; it never
        permits lateral transit through a stack or lowering past its seat.
        """
        pair = [o for o in self.obstacles if o.name in (contact.body_a, contact.body_b)]
        if len(pair) != 2 or any(o.labware is None for o in pair):
            return False
        moving = next((o for o in pair if id(o.labware) in carried), None)
        if moving is None:
            return False
        support = next(o for o in pair if o is not moving)
        if id(support.labware) in carried or support.location != grasp_location:
            return False
        ml, mu = bounds[moving.name]
        sl, su = bounds[support.name]
        stacking = float(support.labware.stack_height or support.labware.height)
        nesting = float(support.labware.height) - stacking
        return (
            math.isfinite(stacking)
            and stacking > 0
            and nesting > 0
            and np.allclose(ml[:2], sl[:2], atol=0.05)
            and np.allclose(mu[:2], su[:2], atol=0.05)
            and ml[2] >= sl[2] + stacking - 0.05
            and su[2] - ml[2] <= nesting + 0.05
        )

    def _check_tips(self, pose):
        if not self.bravo._tips_on_head:
            return
        length = self.bravo._attached_tip_length_mm
        if length is None or not math.isfinite(length) or length <= 0:
            self._fail("Mounted tips lack a recorded length for physical simulation")
        head = self.bravo.profile.head.head_type
        geometry = head_geometry_for_type(head)
        mode = self.bravo._tips_on_head_mode or self.bravo._head_mode
        tip_z = self.teach_length - pose[Axis.Z] - length
        for row, col in active_head_wells(head, mode):
            x, y = pose[Axis.X] + col * geometry.pitch_x_mm, pose[Axis.Y] + row * geometry.pitch_y_mm
            for obstacle in self.obstacles:
                if not (
                    obstacle.lower_mm[0] < x < obstacle.upper_mm[0]
                    and obstacle.lower_mm[1] < y < obstacle.upper_mm[1]
                    and tip_z < obstacle.upper_mm[2] - 0.05
                ):
                    continue
                if obstacle.labware is None:
                    self._fail(
                        f"Mounted tip would strike deck position {obstacle.location}",
                        tip=[row, col],
                        pose={a.name: pose[a] for a in Axis},
                    )
                md = obstacle.labware.metadata or {}
                props = self.context.get("properties", {})
                target = int(props.get("location", -1))
                task = self.context.get("node_type", "")
                returning = task == "tips/TipsOff" and md.get("base_class") == "tip_box"
                if (
                    not obstacle.top_item
                    or obstacle.location != target
                    or not (task.startswith("liquid/") or returning)
                ):
                    self._fail(f"Mounted tip would strike {obstacle.name}", tip=[row, col])
                wg = well_geometry_from_metadata(md)
                depth = float(obstacle.labware.height) if returning else float(md.get("well_depth_mm") or 0)
                diameter = float(md.get("well_diameter_mm") or 0)
                pitch_x = float(md.get("spacing_x_mm") or md.get("spacing_mm") or 0)
                pitch_y = float(md.get("spacing_y_mm") or md.get("spacing_mm") or 0)
                dimensions = {
                    "plate_height_mm" if returning else "well_depth_mm": depth,
                    "well_diameter_mm": diameter,
                    "spacing_x_mm": pitch_x,
                    "spacing_y_mm": pitch_y,
                }
                missing = [key for key, value in dimensions.items() if not math.isfinite(value) or value <= 0]
                if wg.rows <= 0 or wg.cols <= 0:
                    missing.append("well_grid_rows_columns")
                if missing:
                    self._fail(
                        f"{obstacle.labware.name} lacks recorded {', '.join(missing)} for physical tip entry",
                        missing_geometry=missing,
                        labware_id=obstacle.labware.id,
                    )
                tx = self.bravo._teachpoints.get_teachpoint(target, Axis.X)
                ty = self.bravo._teachpoints.get_teachpoint(target, Axis.Y)
                ox, oy = well_center_offset_from_teachpoint_mm(md, row=0, col=0)
                c, r = round((x - tx - ox) / wg.pitch_x_mm), round((y - ty - oy) / wg.pitch_y_mm)
                wx, wy = tx + ox + c * wg.pitch_x_mm, ty + oy + r * wg.pitch_y_mm
                # A 0.25 mm radial model margin is a detection buffer, not a
                # calibrated tip taper. Unknown taper remains a reported limit.
                if not (0 <= r < wg.rows and 0 <= c < wg.cols) or math.hypot(x - wx, y - wy) + 0.25 >= diameter / 2:
                    self._fail(
                        f"Mounted tip is not centered inside a recorded well of {obstacle.labware.name}",
                        tip=[row, col],
                        well=[r, c],
                    )
                if tip_z < obstacle.upper_mm[2] - depth + 0.1:
                    self._fail(f"Mounted tip would hit the well bottom of {obstacle.labware.name}", tip=[row, col])

    def report(self):
        return {
            "engine": "SuperDex",
            "engine_version": "1.0.0",
            "scope": "tool_meshes_catalog_deck_and_carried_plate_envelopes",
            "status": "failed" if self.last_error else "checked",
            "moves_checked": self.moves_checked,
            "samples_checked": self.samples_checked,
            "contact_queries": self.contact_queries,
            "sample_spacing_mm": self.spacing,
            "contact_penetration_threshold_mm": 0.05,
            "trajectory": "straight_interpolation_of_each_native_axis_command",
            "head_geometry": "URDF_collision_mesh" if self.mesh_head else "conservative_envelope",
            "provenance": self.provenance,
            "limitations": [
                "Sampled commanded geometry; firmware axis timing, fluid and force behavior are not modeled.",
                "Catalog plate envelopes and well-axis checks; detailed tip taper is not qualified.",
                "Carried plate envelopes follow verified native PickPlace stages; grasp force and flange detail are not simulated.",
                "Lids and accessory-specific geometry are unsupported and block physical rehearsal.",
                "Robot internal assemblies and gantry/frame self-collision are not certified.",
            ],
            "qualification_granted": False,
            "last_error": self.last_error,
        }
