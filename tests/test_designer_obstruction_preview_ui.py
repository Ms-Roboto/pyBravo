"""Covered-plate previews use failed setup evidence and the matching run's deck."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _run(tmp_path, assertions):
    html = (ROOT / "frontend" / "designer.html").read_text()
    names = (
        "hasCurrentPhysicalSimulationEvidence", "physicalSimulationSummary",
        "renderPhysicalSimulationReport", "renderSimulationBlockers",
        "physicalObstructionDeckDetails", "closePhysicalObstructionPreview",
        "resetPhysicalObstructionPreview", "updatePhysicalObstructionPreview",
        "openPhysicalObstructionPreview", "runWorkflow", "switchToTab",
    )
    functions = []
    for name in names:
        match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    state = re.search(r"let physicalObstructionRun = null;[\s\S]*?let physicalObstructionFocus = null;", html)
    assert state
    listeners = html.split("document.getElementById('physical-report-view').addEventListener", 1)[1]
    listeners = "document.getElementById('physical-report-view').addEventListener" + listeners.split("function workflowCompletionMessage", 1)[0]
    script = tmp_path / "obstruction-preview.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
class Element {
    constructor(id){this.id=id;this.hidden=true;this.open=false;this.textContent='';this.style={};this.handlers={};
        const names=new Set(['hidden']);this.classList={add:value=>names.add(value),remove:value=>names.delete(value),contains:value=>names.has(value)};}
    addEventListener(type,handler){this.handlers[type]=handler;}
    focus(){document.activeElement=this;}
    querySelectorAll(){return ['close','reset','edit','done'].map(id=>element('physical-obstruction-'+id));}
}
const elements=new Map();
const document={activeElement:null,getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);}};
const element=id=>document.getElementById(id);
const graph={_nodes:[],getNodeById(){return null;}};
const designerState={graph,deckConfig:{'7':[{labware_id:'plate',is_lidded:true,is_sealed:false}]},
    labwareCatalog:[{id:'plate',name:'Sample plate',length_mm:127.76,width_mm:85.48,height_mm:14.4,
        lidded_height_mm:16.4,lid_resting_height_mm:9.7,lid_gripper_offset_mm:0.5,
        nested:{source:'catalog'},is_lidded:false,is_sealed:true}]};
const openTabs=[{id:'test',name:'First',graph,deckConfig:designerState.deckConfig},
    {id:'other',name:'Other',graph:{_nodes:[],getNodeById(){return null;}},deckConfig:{}}];
let activeTabIdx=0,currentWorkflowId='test',currentWorkflowName='First',executionMode='simulate';
const API_BASE='';
function isCompiledProtocolPreviewTab(){return false;}
function isProtocolDraftTab(){return false;}
function isGeneratedProtocolDraftTab(){return false;}
function validateWorkflow(){return [];}
function clearActiveNodeHighlight(){}
function timelineReset(){}
function serializeWorkflow(){return {name:'First',graph:{},deck:structuredClone(designerState.deckConfig)};}
function recordDrafterPatch(){}
function syncActiveTabMeta(){}
function updateDeckCell(){}
function syncDeckTo3D(){}
function renderPropertiesPanel(){}
function loadLiquidClasses(){}
function renderTabStrip(){}
async function apiCall(){return {id:'test'};}
const launches=[];
async function fetch(path,options){launches.push({path,options});return {ok:true,json:async()=>({physical_engine:'SuperDex'})};}
const configured=[];
const window={configureDeckPosition(location){configured.push(location);}};
const views=[];
let factoryError=null;
function createPhysicalDiagnosticView(container,options){
    if(factoryError)throw factoryError;
    let resolve,reject;
    const view={container,options,disposed:0,resets:0,ready:new Promise((yes,no)=>{resolve=yes;reject=no;}),
        dispose(){this.disposed++;},resetView(){this.resets++;},resolve(){resolve();},reject(error){reject(error);}};
    views.push(view);return view;
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function report(overrides={}){return {engine:'SuperDex',status:'failed',moves_checked:0,samples_checked:0,contact_queries:0,
    last_error:{kind:'covered_labware',stage:'initialization',location:7,stack_index:0,
        labware_id:'instance-7',labware_definition_id:'plate',labware_name:'Sample plate',is_lidded:true,is_sealed:false,
        message:'Sample plate is lidded/sealed; accessible well and lid collision geometry is required',...overrides}};}
function rememberRun(){physicalObstructionRun={graph:designerState.graph,deckDetails:physicalObstructionDeckDetails()};}
""" + state.group(0) + "\n" + "\n".join(functions) + "\n" + listeners
        + "\n(async()=>{\n" + assertions + "\n})().catch(error=>{console.error(error);process.exitCode=1;});")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_report_auto_opens_lid_seal_and_combined_setup_picture(tmp_path):
    _run(tmp_path, """
for(const [is_lidded,is_sealed,title] of [[true,false,'Lid'],[false,true,'Seal'],[true,true,'Lid and seal']]){
    resetPhysicalObstructionPreview();rememberRun();
    renderPhysicalSimulationReport(report({is_lidded,is_sealed}),'workflow:error');
    assert.equal(element('physical-obstruction-modal').classList.contains('hidden'),false);
    assert.equal(element('physical-report-view').hidden,false);
    assert.equal(element('physical-obstruction-title').textContent,title+' covers the plate');
    assert.equal(element('physical-obstruction-kind').textContent,'Setup illustration');
    assert.equal(element('physical-obstruction-context').textContent,'Position 7 · Sample plate');
    assert.match(element('physical-obstruction-explanation').textContent,/stopped during setup; no collision motion was recorded/);
    assert.doesNotMatch(element('physical-report-body').textContent,/Contact:|Rejected pose/);
    const view=views.at(-1);
    assert.equal(view.options.error.is_lidded,is_lidded);
    assert.equal(view.options.error.is_sealed,is_sealed);
    view.resolve();await flush();
    assert.match(element('physical-obstruction-status').textContent,/illustrative; lid collision geometry has not been checked/);
}
""")


def test_duplicate_errors_do_not_reopen_but_button_can_reopen(tmp_path):
    _run(tmp_path, """
rememberRun();element('physical-report-view').focus();
renderPhysicalSimulationReport(report(),'workflow:error');
assert.equal(document.activeElement,element('physical-obstruction-close'));
const first=views[0];
renderPhysicalSimulationReport(report(),'workflow:error');
assert.equal(views.length,1);
element('physical-obstruction-done').handlers.click();
assert.equal(first.disposed,1);
assert.equal(document.activeElement,element('physical-report-view'));
renderPhysicalSimulationReport(report(),'workflow:error');
assert.equal(views.length,1,'Repeated events must not reopen a dismissed popup');
assert.equal(element('physical-report-view').hidden,false);
element('physical-report-view').handlers.click();
assert.equal(views.length,2);
element('physical-obstruction-reset').handlers.click();
assert.equal(views[1].resets,1);
element('physical-obstruction-edit').handlers.click();
assert.deepEqual(configured,[7]);
assert.equal(views[1].disposed,1);
renderPhysicalSimulationReport(report({location:8,labware_id:'instance-8'}),'workflow:error');
assert.equal(views.length,3,'A different obstruction should open its own picture');
""")


def test_run_snapshot_preserves_cover_metadata_and_resets_previous_picture(tmp_path):
    _run(tmp_path, """
await runWorkflow();
assert.equal(launches[0].path,'/api/workflows/test/simulate');
const saved=physicalObstructionRun.deckDetails['7'][0];
assert.equal(saved.is_lidded,true);assert.equal(saved.is_sealed,false,'Deck flags override catalog flags');
assert.equal(saved.definition_id,'plate');assert.equal(saved.lidded_height_mm,16.4);
assert.equal(saved.lid_resting_height_mm,9.7);assert.equal(saved.lid_gripper_offset_mm,0.5);
designerState.deckConfig['7'][0].is_lidded=false;
designerState.labwareCatalog[0].nested.source='edited after run';
const evidence=report();renderPhysicalSimulationReport(evidence,'workflow:error');
assert.equal(views[0].options.deckDetails['7'][0].is_lidded,true);
assert.equal(views[0].options.deckDetails['7'][0].nested.source,'catalog');
evidence.last_error.labware_name='Later mutation';
assert.equal(physicalObstructionSnapshot.error.labware_name,'Sample plate');
await runWorkflow();
assert.equal(views[0].disposed,1);
assert.equal(physicalObstructionSnapshot,null);
assert.equal(element('physical-report-view').hidden,true);
assert.equal(physicalObstructionRun.deckDetails['7'][0].is_lidded,false);
renderPhysicalSimulationReport(report(),'workflow:error');
assert.equal(views.length,2,'Rerunning resets duplicate suppression');
renderSimulationBlockers('Rack geometry missing');
assert.equal(views[1].disposed,1);assert.equal(physicalObstructionRun,null);
assert.equal(physicalObstructionSnapshot,null);assert.equal(element('physical-report-view').hidden,true);
""")


def test_wrong_graph_and_collision_reports_cannot_become_cover_previews(tmp_path):
    _run(tmp_path, """
rememberRun();
const original=designerState.graph;designerState.graph=openTabs[1].graph;
renderPhysicalSimulationReport(report(),'workflow:error');
assert.equal(views.length,0);assert.equal(element('physical-report-view').hidden,true);
designerState.graph=original;
for(const evidence of [report({kind:undefined}),report({kind:'unsupported_lid_operation'}),
    report({location:0}),report({location:10}),report({location:'7'}),report({is_lidded:false,is_sealed:false}),
    report({kind:undefined,pose:{X:12,Y:24,Z:36},bodies:['robot/head','labware/7/0/Sample plate'],penetration_mm:0.8})]){
    resetPhysicalObstructionPreview();rememberRun();
    renderPhysicalSimulationReport(evidence,'workflow:error');
    assert.equal(views.length,0);assert.equal(element('physical-report-view').hidden,true);
}
assert.ok(element('physical-report-body').textContent.includes('Contact: robot/head ↔ labware/7'));
renderPhysicalSimulationReport(report(),'workflow:node_step');
assert.equal(views.length,0,'Only terminal error events auto-open the picture');
renderPhysicalSimulationReport(report(),'workflow:error');assert.equal(views.length,1);
switchToTab(1);
assert.equal(views[0].disposed,1);assert.equal(physicalObstructionSnapshot,null);
assert.equal(physicalObstructionRun,null);assert.equal(designerState.graph,openTabs[1].graph);
renderPhysicalSimulationReport(report(),'workflow:error');assert.equal(views.length,1);
""")


def test_closed_or_replaced_async_views_cannot_overwrite_the_current_preview(tmp_path):
    _run(tmp_path, """
rememberRun();renderPhysicalSimulationReport(report(),'workflow:error');
const first=views[0];closePhysicalObstructionPreview();
const closedStatus=element('physical-obstruction-status').textContent;
first.resolve();await flush();
assert.equal(element('physical-obstruction-status').textContent,closedStatus);
assert.equal(first.disposed,1);assert.equal(physicalObstructionView,null);
openPhysicalObstructionPreview();const second=views[1];
openPhysicalObstructionPreview();const third=views[2];
third.resolve();await flush();
const activeStatus=element('physical-obstruction-status').textContent;
second.reject(Error('Late model failure'));await flush();
assert.equal(element('physical-obstruction-status').textContent,activeStatus);
assert.equal(second.disposed,1);assert.equal(physicalObstructionView,third);
openPhysicalObstructionPreview();const fourth=views[3];
fourth.reject(Error('Model unavailable'));await flush();
assert.equal(fourth.disposed,1);assert.equal(physicalObstructionView,null);
assert.match(element('physical-obstruction-status').textContent,/Model unavailable.*position 7.*covered/);
factoryError=Error('WebGL unavailable');openPhysicalObstructionPreview();
assert.match(element('physical-obstruction-status').textContent,/3D preview unavailable: WebGL unavailable/);
assert.equal(element('physical-obstruction-modal').classList.contains('hidden'),false);
""")


def test_dimensioned_cover_access_and_sampled_collision_are_distinct(tmp_path):
    _run(tmp_path, """
const lid_geometry={model:'manufacturer_exterior_envelope',length_mm:127.15,width_mm:85.05,
    height_mm:10,seated_bottom_mm:6.8};
rememberRun();
renderPhysicalSimulationReport(report({kind:'lid_access_blocked',geometry:lid_geometry.model,lid_geometry}),'workflow:error');
assert.equal(element('physical-obstruction-title').textContent,'Lid blocks well access');
assert.equal(element('physical-obstruction-kind').textContent,'Well-access check');
assert.match(element('physical-obstruction-context').textContent,/Lid: 127.15 × 85.05 × 10 mm/);
assert.match(element('physical-obstruction-explanation').textContent,/No approach motion was commanded/);
views[0].resolve();await flush();
assert.match(element('physical-obstruction-status').textContent,/Manufacturer-dimensioned exterior envelope/);
assert.doesNotMatch(element('physical-obstruction-status').textContent,/illustrative|has not been checked|Sampled pose/);
resetPhysicalObstructionPreview();rememberRun();
const pose={X:42,Y:50,Z:120,Zg:-20,G:0,W:5};
const runtime_state={head_type:'HT_384_D_70',tips_on_head:true};
renderPhysicalSimulationReport(report({kind:'lid_collision',geometry:lid_geometry.model,lid_geometry,pose,runtime_state}),'workflow:error');
assert.equal(element('physical-obstruction-title').textContent,'Lid interference');
assert.equal(element('physical-obstruction-kind').textContent,'Sampled collision');
assert.match(element('physical-obstruction-explanation').textContent,/sampled motion intersects/);
assert.deepEqual(views[1].options.error.pose,pose);
assert.deepEqual(views[1].options.error.runtime_state,runtime_state);
views[1].resolve();await flush();
assert.match(element('physical-obstruction-status').textContent,/Sampled pose: X 42.00 · Y 50.00 · Z 120.00 · Zg -20.00 mm/);
assert.doesNotMatch(element('physical-obstruction-status').textContent,/illustrative|has not been checked/);
""")


def test_obstruction_prefers_actual_failed_deck_after_plate_movement(tmp_path):
    _run(tmp_path, """
rememberRun();
const deck_details={'8':[{labware_id:'plate',name:'Sample plate',is_lidded:true}], '7':[]};
const evidence=report({kind:'lid_collision',location:8,deck_details});
renderPhysicalSimulationReport(evidence,'workflow:error');
assert.deepEqual(views[0].options.deckDetails,deck_details);
deck_details['8'][0].name='later mutation';
assert.equal(views[0].options.deckDetails['8'][0].name,'Sample plate');
assert.equal(physicalObstructionRun.deckDetails['7'][0].name,'Sample plate');
""")
