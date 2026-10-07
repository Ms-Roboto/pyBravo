"""Read-only identity of collision CAD assets, without importing the native SDK."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

ROBOT_URDF_PATH = Path(__file__).resolve().parents[1] / "model/pybravo_urdf/robot.urdf"


def _asset_snapshot(urdf: Path) -> tuple:
    paths = [urdf, *sorted((urdf.parent / "assets").glob("*.stl"))]
    snapshot = []
    for path in paths:
        stat = path.stat()
        snapshot.append((str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(snapshot)


@lru_cache(maxsize=8)
def _digest_snapshot(snapshot: tuple) -> str:
    # Keep the original scene-provenance algorithm: URDF bytes followed by
    # each STL filename and its bytes, in filename order.
    digest = hashlib.sha256(Path(snapshot[0][0]).read_bytes())
    for row in snapshot[1:]:
        path = Path(row[0])
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def robot_assets_sha256(urdf_path: str | Path | None = None) -> str:
    """Hash collision files, reusing bytes only while their identity is unchanged.

    Include ctime and inode in addition to size/mtime so a same-size edit or
    atomic replacement with preserved modification time invalidates the cache.
    Verify metadata again after hashing to avoid caching a mixed file snapshot.
    """
    urdf = Path(urdf_path).resolve() if urdf_path is not None else ROBOT_URDF_PATH
    for _ in range(3):
        snapshot = _asset_snapshot(urdf)
        digest = _digest_snapshot(snapshot)
        if snapshot == _asset_snapshot(urdf):
            return digest
    raise RuntimeError("Robot collision assets changed while their identity was being read; retry after editing finishes")
