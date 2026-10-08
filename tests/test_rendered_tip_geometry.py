"""Measure mounted tip meshes with the real Three.js renderer geometry code."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Three.js geometry checks")


@pytest.fixture(scope="module")
def rendering_modules(tmp_path_factory):
    """Use bundled modules in Node without browser, network, or WebGL mocks."""
    directory = tmp_path_factory.mktemp("rendered-tip-geometry")
    (directory / "package.json").write_text('{"type":"module"}')
    vendor = directory / "node_modules" / "three"
    shutil.copytree(ROOT / "frontend/vendor/three/0.162.0", vendor)
    (vendor / "package.json").write_text(json.dumps({
        "name": "three", "type": "module",
        "exports": {".": "./build/three.module.js", "./addons/*": "./examples/jsm/*"},
    }))
    for name in ("robot-scene.js", "robot-appearance.js"):
        shutil.copyfile(ROOT / "frontend/src" / name, directory / name)
    return directory


@pytest.mark.parametrize("mode", ["gltf", "fallback"])
@pytest.mark.parametrize("length_mm", [19.9, 26.1])
def test_attached_tip_bottom_matches_native_protruding_length(rendering_modules, mode, length_mm):
    script = rendering_modules / "measure-tip.mjs"
    script.write_text(r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {RobotScene} from './robot-scene.js';

// Three's FileLoader uses this DOM event for embedded buffer progress only.
globalThis.ProgressEvent ??= class ProgressEvent {
    constructor(type, values) { this.type=type; Object.assign(this,values); }
};
const [mode, lengthText, asset] = process.argv.slice(2);
const length = Number(lengthText) / 1000;
const host = new THREE.Group();
const barrels = new THREE.Mesh(new THREE.BoxGeometry(0.1065,0.0705,0.007),new THREE.MeshBasicMaterial());
barrels.position.set(0.011,-0.009,0.14);
host.add(barrels);
const barrelBottom = new THREE.Box3().setFromObject(barrels).min.z;
const scene = Object.create(RobotScene.prototype);
Object.assign(scene, {
    urdfRobot:{_links:{'384_head_384_head':host}},
    headTipsRoot:new THREE.Group(), tipsOnHead:true,
    headType:'HT_384_D_70',
    tipsOnHeadMode:{subset_type:'column',subset_config:'back_left',column_count:1},
    attachedTipLengthMm:Number(lengthText), activeTipCapacityUl:10,
    tipTemplateCache:new Map(),
});
const loader = new GLTFLoader();
let parsed = null;
if (mode === 'gltf') {
    parsed = await loader.parseAsync(fs.readFileSync(asset,'utf8'),'');
}
scene.gltfLoader = {
    load(_url,onLoad,_progress,onError) {
        if (parsed) onLoad(parsed);
        else onError(new Error('Exercise production cone/cylinder fallback'));
    },
};
function close(actual,expected,label) {
    assert.ok(Math.abs(actual-expected)<1e-7,
        `${label}: expected ${expected*1000} mm; got ${actual*1000} mm`);
}
// Measure the output meshes, including source glTF rotation and scaling.
// The two render branches previously used opposite local Z conventions.
for (const z of [0,-0.065,-0.1299]) {
    host.position.set(0.19598,0.01018,z);
    await scene._renderHeadTipsFromState();
    host.updateMatrixWorld(true);
    assert.equal(scene.headTipsRoot.children.length,16);
    const barrelPlane = barrelBottom+z;
    for (const tip of scene.headTipsRoot.children) {
        const bounds = new THREE.Box3().setFromObject(tip);
        close(bounds.min.z,barrelPlane-length,'Tip point below barrel datum');
        close(bounds.max.z,barrelPlane,'Tip seating plane meets barrel datum');
        close(bounds.max.z-bounds.min.z,length,'Rendered protruding length');
    }
}
scene.tipsOnHead=false;
await scene._renderHeadTipsFromState();
assert.equal(scene.headTipsRoot.children.length,0);
""")
    result = subprocess.run(
        [NODE, str(script), mode, str(length_mm), str(ROOT / "labware/editor_assets/tips/d10.gltf")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
