"""Headless SuperDex contact queries for explicitly positioned rigid geometry.

This adapter reports geometric overlaps. It does not drive the Bravo, integrate a
robot trajectory, or establish that a liquid-transfer method is safe. All input
coordinates are in meters in a right-handed, Z-up frame. Each mesh is provided
in local coordinates and positioned with a world-space translation/quaternion.

SuperDex has a process-wide context. Initialize it on the Python main thread
before constructing any worker-owned scene; its own process-exit cleanup is
thread-sensitive. Closing a scene leaves that context alive, since shutdown
could invalidate another scene. Each scene stays on its creating thread.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from superdex import physics

Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
_IDENTITY_ROTATION: Quaternion = (0.0, 0.0, 0.0, 1.0)
_CONTEXT_LOCK = threading.RLock()


def ensure_physics_context() -> None:
    """Initialize the process-wide SDK context on the Python main thread.

    A first initialization in a worker can hang during SDK atexit cleanup on
    the main thread. Worker-created scenes remain supported after this call.
    """
    with _CONTEXT_LOCK:
        if physics.is_initialized():
            return
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "SuperDex physics context must be initialized on the main "
                "thread before creating a worker collision scene"
            )
        physics.initialize(num_worker_threads=0)


@dataclass(frozen=True)
class CollisionContact:
    """One penetrating body pair; ``penetration_m`` is the deepest sample."""

    body_a: str
    body_b: str
    penetration_m: float
    sample_count: int


@dataclass
class _Body:
    actor: physics.Actor
    shape: physics.ShapeHandle
    translation_m: Vector3
    rotation_xyzw: Quaternion
    movable: bool
    query: physics.QueryHandle | None


def _vector3(value: Sequence[float], label: str) -> Vector3:
    if len(value) != 3:
        raise ValueError(f"{label} must have three coordinates")
    xyz = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in xyz):
        raise ValueError(f"{label} must be finite")
    return xyz  # type: ignore[return-value]


def _quaternion(value: Sequence[float]) -> Quaternion:
    if len(value) != 4:
        raise ValueError("rotation_xyzw must have four coordinates")
    xyzw = tuple(float(component) for component in value)
    norm = math.sqrt(sum(component * component for component in xyzw))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("rotation_xyzw must be a finite, nonzero quaternion")
    return tuple(component / norm for component in xyzw)  # type: ignore[return-value]


def _box_mesh(half_extents_m: Vector3) -> physics.ShapeHandle:
    x, y, z = half_extents_m
    coordinates = [
        -x, -y, -z, x, -y, -z, x, y, -z, -x, y, -z,
        -x, -y, z, x, -y, z, x, y, z, -x, y, z,
    ]
    triangles = [
        0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7,
        0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5,
        2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7,
    ]
    return physics.create_tri_mesh_shape(coordinates, triangles)


class SuperDexCollisionBackend:
    """Persistent scene for contact checks at caller-supplied poses.

    Register moving robot bodies with ``movable=True`` and fixtures as static.
    ``contacts`` reports moving/static and moving/moving overlaps. Static/static
    pairs are not queried. A positive engine time step is necessary for contact
    query refresh; all actors are reset to their caller-supplied poses and zero
    velocity before every query, so dynamics never become the source of poses.
    """

    def __init__(self, *, query_step_s: float = 0.001) -> None:
        if not math.isfinite(query_step_s) or query_step_s <= 0:
            raise ValueError("query_step_s must be positive and finite")
        ensure_physics_context()
        self._owner_thread = threading.get_ident()
        self._scene = physics.create_scene("pybravo_contact_query")
        self._scene.set_gravity([0.0, 0.0, 0.0])
        self._query_step_s = query_step_s
        self._bodies: dict[str, _Body] = {}
        self._closed = False

    def __enter__(self) -> SuperDexCollisionBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SuperDex collision scene is closed")
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("SuperDex collision scene must be used on its owner thread")

    def _add(
        self,
        name: str,
        shape: physics.ShapeHandle,
        translation_m: Sequence[float],
        rotation_xyzw: Sequence[float],
        *,
        movable: bool,
        collider_type: physics.ColliderType,
    ) -> None:
        self._check_open()
        if not isinstance(name, str) or not name or name in self._bodies:
            raise ValueError("body name must be nonempty and unique")
        translation = _vector3(translation_m, "translation_m")
        rotation = _quaternion(rotation_xyzw)
        actor = self._scene.create_rigid_actor(
            name=name,
            shape=shape,
            world_from_local=physics.TransformRT(
                rotation=rotation, translation=translation
            ),
            is_static=not movable,
            has_gravity=False,
            collider_type=collider_type,
        )
        if actor is None:
            raise RuntimeError(f"SuperDex failed to create collision body {name!r}")
        query = None
        if movable:
            if not actor.is_query_supported(physics.QueryType.CONTACT_POINTS):
                raise RuntimeError(f"SuperDex cannot query contacts for {name!r}")
            query = actor.register_query(physics.QueryType.CONTACT_POINTS)
        self._bodies[name] = _Body(actor, shape, translation, rotation, movable, query)

    def add_box(
        self,
        name: str,
        center_m: Sequence[float],
        half_extents_m: Sequence[float],
        *,
        movable: bool = False,
        rotation_xyzw: Sequence[float] = _IDENTITY_ROTATION,
    ) -> None:
        """Add an oriented box; ``center_m`` is its world-space center."""
        extents = _vector3(half_extents_m, "half_extents_m")
        if any(value <= 0 for value in extents):
            raise ValueError("half_extents_m must be positive")
        self._add(
            name, _box_mesh(extents), center_m, rotation_xyzw,
            movable=movable, collider_type=physics.ColliderType.BOX,
        )

    def add_mesh(
        self,
        name: str,
        vertices_m: Sequence[Sequence[float]],
        triangles: Sequence[Sequence[int]],
        *,
        translation_m: Sequence[float] = (0.0, 0.0, 0.0),
        rotation_xyzw: Sequence[float] = _IDENTITY_ROTATION,
        movable: bool = False,
    ) -> None:
        """Add a closed triangle mesh using SuperDex's SDF collider.

        SDF quality depends on watertight, consistently oriented triangles and
        bake resolution. Callers must not interpret a clear result from a bad
        mesh as proof that a real instrument path is clear.
        """
        self._check_open()
        vertices = [_vector3(vertex, "vertex") for vertex in vertices_m]
        if len(vertices) < 4 or not triangles:
            raise ValueError("mesh needs at least four vertices and one triangle")
        faces: list[int] = []
        for triangle in triangles:
            if len(triangle) != 3 or any(
                not isinstance(index, int) or index < 0 or index >= len(vertices)
                for index in triangle
            ):
                raise ValueError("triangle indices must reference mesh vertices")
            if len(set(triangle)) != 3:
                raise ValueError("mesh triangles must have three distinct vertices")
            faces.extend(triangle)
        shape = physics.create_tri_mesh_shape(
            [value for vertex in vertices for value in vertex], faces
        )
        self._add(
            name, shape, translation_m, rotation_xyzw,
            movable=movable, collider_type=physics.ColliderType.SDF,
        )

    def add_stl(
        self,
        name: str,
        path: str | Path,
        *,
        scale: Sequence[float] = (1.0, 1.0, 1.0),
        translation_m: Sequence[float] = (0.0, 0.0, 0.0),
        rotation_xyzw: Sequence[float] = _IDENTITY_ROTATION,
        movable: bool = False,
    ) -> None:
        """Load an STL collision mesh; scale converts its vertices to meters."""
        self._check_open()
        factors = _vector3(scale, "scale")
        if any(value == 0 for value in factors):
            raise ValueError("scale components must be nonzero")
        mesh_path = Path(path)
        if not mesh_path.is_file() or mesh_path.suffix.lower() != ".stl":
            raise ValueError("path must name an existing STL file")
        shape = physics.load_shape_from_file(str(mesh_path), factors)
        self._add(
            name, shape, translation_m, rotation_xyzw,
            movable=movable, collider_type=physics.ColliderType.SDF,
        )

    def set_pose(
        self,
        name: str,
        translation_m: Sequence[float],
        rotation_xyzw: Sequence[float] = _IDENTITY_ROTATION,
    ) -> None:
        """Supply a body's absolute world pose for the next contact query."""
        self._check_open()
        body = self._bodies[name]
        body.translation_m = _vector3(translation_m, "translation_m")
        body.rotation_xyzw = _quaternion(rotation_xyzw)

    def contacts(
        self,
        *,
        exclude_pairs: Iterable[tuple[str, str]] = (),
        include_pairs: Iterable[tuple[str, str]] | None = None,
        min_penetration_m: float = 0.0,
    ) -> list[CollisionContact]:
        """Return penetrating pairs, excluding approved assembly contacts.

        ``include_pairs`` limits checks to explicit pairs; ``exclude_pairs``
        suppresses known benign assembly contacts. The threshold can suppress
        sub-resolution numerical noise, but callers should keep it conservative.
        """
        self._check_open()
        if not math.isfinite(min_penetration_m) or min_penetration_m < 0:
            raise ValueError("min_penetration_m must be nonnegative and finite")

        def pairs(values: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
            result = set()
            for first, second in values:
                if first not in self._bodies or second not in self._bodies:
                    raise ValueError("contact pair names must be registered bodies")
                result.add(tuple(sorted((first, second))))
            return result

        excluded = pairs(exclude_pairs)
        included = None if include_pairs is None else pairs(include_pairs)
        for body in self._bodies.values():
            body.actor.set_root_transform(
                physics.TransformRT(
                    rotation=body.rotation_xyzw,
                    translation=body.translation_m,
                )
            )
            body.actor.set_velocity([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        self._scene.step(self._query_step_s)
        deepest: dict[tuple[str, str], float] = {}
        counts: dict[tuple[str, str], int] = {}
        for body in self._bodies.values():
            if not body.movable:
                continue
            for point in body.actor.get_contact_points_world():
                actor_a = self._scene.get_actor(point.actor_a)
                actor_b = self._scene.get_actor(point.actor_b)
                if actor_a is None or actor_b is None:
                    raise RuntimeError("SuperDex returned a contact for an unknown actor")
                pair = tuple(sorted((actor_a.get_name(), actor_b.get_name())))
                if pair[0] == pair[1] or pair in excluded:
                    continue
                if included is not None and pair not in included:
                    continue
                penetration = -float(point.distance)
                if penetration <= min_penetration_m:
                    continue
                deepest[pair] = max(deepest.get(pair, 0.0), penetration)
                counts[pair] = counts.get(pair, 0) + 1
        return [
            CollisionContact(first, second, deepest[(first, second)], counts[(first, second)])
            for first, second in sorted(deepest)
        ]

    def close(self) -> None:
        """Destroy this scene without shutting down another user's context."""
        if self._closed:
            return
        self._check_open()
        physics.destroy_scene(self._scene)
        self._bodies.clear()
        self._closed = True
