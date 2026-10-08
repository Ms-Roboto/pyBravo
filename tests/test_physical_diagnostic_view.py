"""Exercise the setup preview with real Three geometry and controlled asset timing."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for diagnostic view checks")


@pytest.fixture(scope="module")
def modules(tmp_path_factory):
    directory = tmp_path_factory.mktemp("physical-diagnostic-view")
    (directory / "package.json").write_text('{"type":"module"}')
    vendor = directory / "node_modules/three"
    shutil.copytree(ROOT / "frontend/vendor/three/0.162.0", vendor)
    (vendor / "package.json").write_text(json.dumps({
        "name": "three", "type": "module",
        "exports": {".": "./build/three.module.js", "./addons/*": "./examples/jsm/*"},
    }))
    for name in ("robot-scene.js", "robot-appearance.js", "physical-diagnostic-view.js"):
        shutil.copyfile(ROOT / "frontend/src" / name, directory / name)
    return directory


IMPORTS = """
import assert from 'node:assert/strict';
import * as THREE from 'three';
import {RobotScene} from './robot-scene.js?v=diagnostic-deck1';
import {createPhysicalDiagnosticView, prepareDiagnosticDeckDetails} from './physical-diagnostic-view.js';
globalThis.location = {hostname:'test'};
globalThis.WebSocket = class {constructor(){throw new Error('Diagnostic connected hardware WebSocket');}};
globalThis.fetch = () => {throw new Error('Unexpected network request');};
const plate = {id:'plate-a',name:'Lidded plate',length_mm:127.76,width_mm:85.48,
    height_mm:14.4,lidded_height_mm:17.2,lid_resting_height_mm:9.7,stack_height_mm:12.4,is_lidded:true};
const error = {kind:'covered_labware',location:5,stack_index:0,labware_definition_id:'plate-a',
    is_lidded:true,is_sealed:false,lower_mm:[20,30,-130],upper_mm:[147.76,115.48,-112.8]};
function close(actual, expected) {assert.ok(Math.abs(actual-expected)<1e-7,`${actual} != ${expected}`);}
"""

FAKE_SURFACE = """
let currentScene;
let animationStarts = 0;
let rendererDisposals = 0;
const container = {
    children:[], get firstChild(){return this.children[0];},
    appendChild(child){this.children.push(child);},
    removeChild(child){this.children.splice(this.children.indexOf(child),1);},
    getBoundingClientRect(){return {width:680,height:360};},
};
globalThis.getComputedStyle = () => ({getPropertyValue:()=> '#0d0d14'});
RobotScene.prototype._setupRenderer = function() {
    currentScene = this;
    const canvas = {attributes:{},setAttribute(key,value){this.attributes[key]=value;}};
    this.renderer = {domElement:canvas,setSize(){},dispose(){rendererDisposals++;}};
    this.container.appendChild(canvas);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45,680/360,0.001,100);
    this.controls = {target:new THREE.Vector3(),dispose(){},update(){},saveState(){}};
};
RobotScene.prototype._loadURDF = async function() {
    const group = new THREE.Group();
    group.rotation.x = -Math.PI/2;
    group.add(this.labwareRoot);
    this.scene.add(group);
    this.urdfRobot = {group,_links:{}};
    this.deckSlotAnchors.set(5,new THREE.Vector3(0.2,0.1,0.01));
    this.deckSlotAnchors.set(6,new THREE.Vector3(0.35,0.1,0.01));
};
RobotScene.prototype._startAnimation = function(){animationStarts++;};
RobotScene.prototype.connectWebSocket = function(){throw new Error('Attempted hardware connection');};
"""


def run_js(modules, body, *, surface=False):
    script = modules / "exercise.mjs"
    script.write_text(IMPORTS + (FAKE_SURFACE if surface else "") + body)
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_lid_uses_backend_catalog_geometry(modules):
    run_js(modules, """
const source = {'5':[plate]};
const snapshot = JSON.stringify(source);
const setup = prepareDiagnosticDeckDetails(source,error);
assert.equal(JSON.stringify(source),snapshot);
close(setup.detail.base_height_mm,14.4);
close(setup.detail.height_mm,17.2);
close(setup.detail.generated_lid.height_mm,7.5);
const scene = new RobotScene({}, {autoConnect:false,deckOnly:true});
const mesh = await scene._buildLabwareMesh(setup.detail);
const lid = mesh.children.find(child=>child.userData.labwarePart==='lid');
assert.ok(lid, 'Must render the existing lid geometry');
const bounds = new THREE.Box3().setFromObject(lid);
close(bounds.min.z,0.0097);
close(bounds.max.z,0.0172);
close(bounds.max.x-bounds.min.x,0.12776);
""")


def test_selects_reported_stack_item_and_rejects_stale_snapshot(modules):
    run_js(modules, """
const source = {'5':[{...plate,is_lidded:false},{...plate,is_lidded:true}]};
const setup = prepareDiagnosticDeckDetails(source,{...error,stack_index:1});
assert.equal(setup.stackIndex,1);
assert.equal(setup.deckDetails['5'][0].generated_lid,undefined);
assert.ok(setup.deckDetails['5'][1].generated_lid);
assert.throws(()=>prepareDiagnosticDeckDetails(source,{...error,stack_index:2}),/no longer matches/);
assert.throws(()=>prepareDiagnosticDeckDetails(source,{...error,labware_definition_id:'different'}),/no longer matches/);
assert.throws(()=>prepareDiagnosticDeckDetails(source,{...error,kind:'collision'}),/covered deck position/);
assert.throws(()=>prepareDiagnosticDeckDetails({},error),/missing from this setup/);
""")


@pytest.mark.parametrize("sealed", [False, True])
def test_focuses_highlighted_cover_without_hardware_or_motion(modules, sealed):
    run_js(modules, f"const sealed = {json.dumps(sealed)};" + """
const source = {'5':[plate], '6':[{...plate,is_lidded:false}]};
const view = createPhysicalDiagnosticView(container,{error:{...error,is_lidded:!sealed,is_sealed:sealed},deckDetails:source});
assert.equal(typeof view.dispose,'function');
await view.ready;
assert.equal(currentScene.options.autoConnect,false);
assert.equal(currentScene.options.deckOnly,true);
assert.equal(currentScene.options.showGizmo,false);
currentScene._updateURDFJoints({Z:123}); // deckOnly must also skip profile fetch
assert.equal(container.children.length,1);
assert.equal(currentScene.labwareRoot.children.length,2);
const selected = currentScene.labwareRoot.children.find(group=>group.userData.deckLocation===5);
const neighbor = currentScene.labwareRoot.children.find(group=>group.userData.deckLocation===6);
let cover;
selected.traverse(object=>{
    if (object.userData.labwarePart===(sealed?'seal-annotation':'lid')) cover=object;
});
assert.ok(cover);
let coverMesh;
cover.traverse(object=>{if(object.isMesh)coverMesh=object;});
assert.equal(coverMesh.material.color.getHex(),0xff963d);
assert.equal(neighbor.children[0].material.color.getHex(),0x566074);
assert.notEqual(selected.children[0].material.color.getHex(),0x566074);
close(currentScene.controls.target.x,0.2);
close(currentScene.controls.target.z,-0.1);
const camera = currentScene.camera.position.clone();
currentScene.camera.position.set(9,9,9);
view.resetView();
assert.ok(currentScene.camera.position.distanceTo(camera)<1e-7);
view.dispose();
view.dispose();
assert.equal(container.children.length,0);
assert.equal(rendererDisposals,1);
""", surface=True)


def test_close_while_initializing_never_starts_animation_or_restores_canvas(modules):
    run_js(modules, """
let release;
RobotScene.prototype._loadURDF = () => new Promise(resolve=>{release=resolve;});
const view = createPhysicalDiagnosticView(container,{error,deckDetails:{'5':[plate]}});
await Promise.resolve();
assert.equal(container.children.length,1);
view.dispose();
assert.equal(container.children.length,0);
release();
await view.ready;
assert.equal(animationStarts,0);
assert.equal(container.children.length,0);
assert.equal(rendererDisposals,1);
""", surface=True)


def test_close_during_gltf_load_disposes_late_geometry(modules):
    run_js(modules, """
let release;
RobotScene.prototype._setupLighting = function() {
    this.gltfLoader = {load(_url, loaded){release=loaded;}};
};
const view = createPhysicalDiagnosticView(container,{error,deckDetails:{'5':[{...plate,model_3d:'/plate.glb'}]}});
while (!release) await Promise.resolve();
view.dispose();
const source = new THREE.Mesh(new THREE.BoxGeometry(0.1,0.02,0.08),new THREE.MeshStandardMaterial());
let geometryDisposals=0,materialDisposals=0;
source.geometry.addEventListener('dispose',()=>geometryDisposals++);
source.material.addEventListener('dispose',()=>materialDisposals++);
release({scene:source});
await view.ready;
assert.equal(geometryDisposals,1);
assert.equal(materialDisposals,1);
assert.equal(container.children.length,0);
assert.equal(currentScene.labwareRoot.children.length,0);
assert.equal(currentScene.labwareTemplateCache.size,0);
""", surface=True)
