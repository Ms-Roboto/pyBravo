"""The graph editor and 3D scene must not depend on CDN availability."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pybravo.web import server

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
VENDOR = FRONTEND / "vendor"
ASSET_PATTERN = re.compile(r"/static/vendor/[A-Za-z0-9_./-]+\.(?:js|css)(?:\?v=[A-Za-z0-9_-]+)?")
MODULE_IMPORT_PATTERN = re.compile(r"^\s*(?:\}\s*|import[^\n]*\s+)from\s*['\"]([^'\"]+)['\"]", re.M)


def _import_map(html: str) -> dict:
    match = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.S)
    assert match, "the Three.js import map is required"
    return json.loads(match.group(1))["imports"]


@pytest.mark.parametrize("page", ["designer.html", "index.html"])
def test_critical_browser_dependencies_use_local_pinned_assets(page):
    html = (FRONTEND / page).read_text(encoding="utf-8")
    imports = _import_map(html)
    assert imports["three"].startswith("/static/vendor/three/0.162.0/build/")
    assert imports["three/addons/"] == "/static/vendor/three/0.162.0/examples/jsm/"
    assert "esm.sh/three" not in html
    if page == "designer.html":
        assert "/static/vendor/litegraph/0.7.18/css/litegraph.css?v=dev" in html
        assert "/static/vendor/litegraph/0.7.18/build/litegraph.min.js?v=dev" in html
        assert "/static/vendor/dagre/0.8.5/dist/dagre.min.js?v=dev" in html
        assert "cdn.jsdelivr.net/npm/litegraph" not in html
        assert "cdn.jsdelivr.net/npm/dagre" not in html
    for url in ASSET_PATTERN.findall(html):
        path = FRONTEND / urlsplit(url).path.removeprefix("/static/")
        assert path.is_file(), f"missing local browser asset {url}"
    versioned = server._version_static_assets(html, FRONTEND)
    assert "/static/vendor/three/0.162.0/build/three.module.js?v=dev" not in versioned


def test_three_addon_dependency_closure_is_local():
    root = VENDOR / "three" / "0.162.0" / "examples" / "jsm"
    needed = [
        root / "controls" / "OrbitControls.js",
        root / "loaders" / "GLTFLoader.js",
        root / "loaders" / "STLLoader.js",
        root / "utils" / "SkeletonUtils.js",
    ]
    seen: set[Path] = set()
    while needed:
        path = needed.pop()
        if path in seen:
            continue
        seen.add(path)
        for specifier in MODULE_IMPORT_PATTERN.findall(path.read_text(encoding="utf-8")):
            if specifier == "three":
                continue
            assert specifier.startswith("."), f"unexpected network/bare import {specifier} in {path}"
            dependency = (path.parent / specifier).resolve()
            assert dependency.is_relative_to(root.resolve())
            assert dependency.is_file(), f"missing Three.js addon dependency {dependency}"
            needed.append(dependency)
    assert root / "utils" / "BufferGeometryUtils.js" in seen


def test_vendored_distributions_match_their_pinned_checksums():
    entries = (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(entries) >= 12
    for entry in entries:
        digest, relative = entry.split("  ", 1)
        asset = VENDOR / relative
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == digest, relative


def test_run_server_default_port_override_mounts_frontend_static_files(monkeypatch):
    fresh_app = FastAPI()
    monkeypatch.setattr(server, "app", fresh_app)
    monkeypatch.setattr(server, "_labware_assets_mounted", False)
    monkeypatch.setattr(server, "_bravo", server._bravo)
    monkeypatch.setattr(server, "_profile_path", server._profile_path)
    monkeypatch.setattr(server, "_profile_dir", server._profile_dir)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    server.run_server(bravo=object(), port=8002)

    with TestClient(fresh_app) as client:
        for path in (
            "/static/vendor/litegraph/0.7.18/build/litegraph.min.js",
            "/static/vendor/litegraph/0.7.18/css/litegraph.css",
            "/static/vendor/dagre/0.8.5/dist/dagre.min.js",
            "/static/vendor/three/0.162.0/build/three.module.js",
            "/static/src/robot-scene.js",
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.content, path
        index = client.get("/")
        assert index.status_code == 200
        assert "/static/vendor/three/0.162.0/build/three.module.js?v=dev" not in index.text
