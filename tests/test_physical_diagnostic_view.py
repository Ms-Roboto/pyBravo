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
import {RobotScene} from './robot-scene.js?v=cellvis-lid1';
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


def test_dimensioned_lid_uses_report_geometry_without_shrinking_plate(modules):
    run_js(modules, """
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const evidence={...error,kind:'lid_access_blocked',geometry:lid_geometry.model,lid_geometry,
    coordinate_frame:'machine_xyz_z_up_mm',lower_mm:[20,30,-130],upper_mm:[147.15,115.05,-120]};
const source={'5':[{...plate,height_mm:14.33}]};
const sourceBefore=JSON.stringify(source);
const setup=prepareDiagnosticDeckDetails(source,evidence);
assert.equal(JSON.stringify(source),sourceBefore);
close(setup.detail.base_height_mm,14.33);
close(setup.detail.total_height_mm,16.8);
close(setup.detail.generated_lid.height_mm,10);
close(setup.detail.generated_lid.length_mm,127.15);
const view=createPhysicalDiagnosticView(container,{error:evidence,deckDetails:source});
const result=await view.ready;
assert.equal(result.dimensioned,true);assert.equal(result.sampledPoseShown,false);
const selected=currentScene.labwareRoot.children[0];
const lid=selected.children.find(child=>child.userData.labwarePart==='lid');
assert.equal(lid.userData.geometryModel,'manufacturer_exterior_envelope');
const localBounds=new THREE.Box3().setFromObject(lid);
close(localBounds.max.x-localBounds.min.x,0.12715);
close(localBounds.max.z-localBounds.min.z,0.08505);
assert.equal(lid.children.find(child=>child.isMesh).material.opacity,0.28);
view.dispose();
""", surface=True)


def test_sampled_lid_collision_freezes_snapshot_and_draws_recorded_tip_axis(modules):
    run_js(modules, """
const originalLoad=RobotScene.prototype._loadURDF;
const jointValues={};
RobotScene.prototype._buildTipTemplate=async()=>null;
RobotScene.prototype._loadURDF=async function(){
    await originalLoad.call(this);
    this.urdfRobot.setJointValue=(name,value)=>{jointValues[name]=value;};
};
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const pose={X:42,Y:50,Z:120,Zg:-20,G:0,W:5};
const runtime_state={head_type:'HT_384_D_70',tips_on_head:true,
    head_mode:{subset_type:'column',subset_config:'back_left',column_count:1},tips_on_head_mode:{subset_type:'column',subset_config:'back_left',column_count:1},
    tips_on_head_selection:{anchor_row:0,anchor_col:23},attached_tip_length_mm:19.9,
    active_tip_capacity_ul:10,teach_tip_length_mm:26.1,tipbox_removed_cells:{'1':['0:23']},
    teachpoints:{'5':{X:42,Y:50,Z:140},'6':{X:180,Y:50,Z:140}}};
const evidence={...error,kind:'lid_collision',geometry:lid_geometry.model,lid_geometry,pose,runtime_state,
    coordinate_frame:'machine_xyz_z_up_mm',lower_mm:[20,30,-130],upper_mm:[147.15,115.05,-120],
    tip_segment_mm:[[42,50,-121],[42,50,-101.1]]};
const view=createPhysicalDiagnosticView(container,{error:evidence,deckDetails:{'5':[plate]}});
const result=await view.ready;
assert.equal(result.dimensioned,true);assert.equal(result.sampledPoseShown,true);assert.equal(result.tipAxisShown,true);
assert.equal(currentScene.options.deckOnly,false);assert.equal(currentScene.options.autoConnect,false);
assert.deepEqual(currentScene.positions,pose);assert.deepEqual(currentScene.renderPositions,pose);
assert.equal(currentScene.attachedTipLengthMm,19.9);
assert.equal(currentScene.teachTipLengthMm,26.1);
assert.equal(currentScene.teachpoints['5'].x,42);
assert.deepEqual(currentScene.tipboxTransientState,{'1':['0:23']});
close(jointValues.zaxis,-0.0939);
const selected=currentScene.labwareRoot.children[0];
const line=selected.getObjectByName('sampled-tip-axis');
assert.ok(line);assert.equal(line.material.color.getHex(),0xff414b);
const positions=line.geometry.getAttribute('position');
close(positions.getX(0),(42-83.575)/1000);
close(positions.getY(0),(50-72.525)/1000);
close(positions.getZ(0),0.0158);
close(positions.getZ(1),0.0357);
assert.match(currentScene.renderer.domElement.attributes['aria-label'],/Sampled interference/);
view.dispose();
""", surface=True)


def test_incomplete_sampled_state_cannot_invent_a_robot_pose_or_contact(modules):
    run_js(modules, """
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const evidence={...error,kind:'lid_collision',geometry:lid_geometry.model,lid_geometry,
    runtime_state:{head_type:'HT_384_D_70',tips_on_head:true,teach_tip_length_mm:26.1},
    pose:{X:42,Y:50,Z:120},tip_segment_mm:[[42,50,-121],[42,50,-101.1]]};
const view=createPhysicalDiagnosticView(container,{error:evidence,deckDetails:{'5':[plate]}});
const result=await view.ready;
assert.equal(result.sampledPoseShown,false);assert.equal(result.tipAxisShown,false);
assert.equal(currentScene.options.deckOnly,true);
assert.equal(currentScene.labwareRoot.children[0].getObjectByName('sampled-tip-axis'),undefined);
view.dispose();
""", surface=True)


def test_rejected_catalog_lid_metadata_stays_an_illustration(modules):
    run_js(modules, """
const invalid={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const source={'5':[{...plate,lid_geometry:invalid,
    generated_lid:{lid_geometry:invalid,height_mm:10}}]};
// The catalog top remains 17.2, inconsistent with the proposed geometry's 16.8.
const setup=prepareDiagnosticDeckDetails(source,error);
assert.equal(setup.detail.lid_geometry,undefined);
assert.equal(setup.detail.generated_lid.lid_geometry,undefined);
close(setup.detail.generated_lid.height_mm,7.5);
assert.ok(source['5'][0].generated_lid.lid_geometry,'Must not mutate the snapshot');
""")


def test_carried_lid_picture_uses_sampled_translation_not_source_deck_pose(modules):
    run_js(modules, """
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const evidence={...error,kind:'lid_collision',geometry:lid_geometry.model,lid_geometry,
    coordinate_frame:'machine_xyz_z_up_mm',carried_lid:true,carry_offset_mm:[130,-20,40],
    lower_mm:[150,10,-90],upper_mm:[277.15,95.05,-80]};
const source={'5':[plate]};
const view=createPhysicalDiagnosticView(container,{error:evidence,deckDetails:source});
await view.ready;
const target=currentScene.labwareRoot.children[0];
close(target.position.x,0.33);close(target.position.y,0.08);close(target.position.z,0.05);
currentScene._updateLabwareAnimation();
close(target.position.x,0.33);close(target.position.y,0.08);close(target.position.z,0.05);
assert.equal(source['5'][0].name,'Lidded plate');
view.dispose();
const incomplete=createPhysicalDiagnosticView(container,{error:{...evidence,carry_offset_mm:null},deckDetails:source});
await assert.rejects(incomplete.ready,/carried lid position is missing/);
assert.equal(container.children.length,0);
""", surface=True)


def test_close_during_sampled_tip_asset_load_keeps_preview_disposed(modules):
    run_js(modules, """
let release;
RobotScene.prototype._setupLighting=function(){
    this.gltfLoader={load(_url,loaded){release=loaded;}};
};
const originalLoad=RobotScene.prototype._loadURDF;
RobotScene.prototype._loadURDF=async function(){
    await originalLoad.call(this);this.urdfRobot.setJointValue=()=>{};
};
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8,source:'https://www.cellvis.com/drawing.png'};
const evidence={...error,kind:'lid_collision',geometry:lid_geometry.model,lid_geometry,
    pose:{X:42,Y:50,Z:120,Zg:-20,G:0,W:5},runtime_state:{
        head_type:'HT_384_D_70',tips_on_head:true,teach_tip_length_mm:26.1,
        tips_on_head_mode:{subset_type:'column',subset_config:'back_left',column_count:1},
        tips_on_head_selection:{anchor_row:0,anchor_col:23},attached_tip_length_mm:19.9}};
const view=createPhysicalDiagnosticView(container,{error:evidence,deckDetails:{'5':[plate]}});
while(!release)await Promise.resolve();
view.dispose();
const source=new THREE.Mesh(new THREE.BoxGeometry(.01,.02,.01),new THREE.MeshStandardMaterial());
let geometryDisposals=0,materialDisposals=0;
source.geometry.addEventListener('dispose',()=>geometryDisposals++);
source.material.addEventListener('dispose',()=>materialDisposals++);
release({scene:source});
await view.ready;
assert.equal(container.children.length,0);
assert.equal(currentScene.headTipsRoot.children.length,0);
assert.equal(currentScene.tipTemplateCache.size,0);
assert.equal(geometryDisposals,1);assert.equal(materialDisposals,1);
""", surface=True)
