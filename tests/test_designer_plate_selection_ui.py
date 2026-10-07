"""Plate selection displays native placements, never a hardcoded quadrant list."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer checks")


def _run(tmp_path, assertions):
    html = (ROOT / "frontend" / "designer.html").read_text()
    names = ("plateSelectionContextCurrent", "openPlateSelectionPicker", "loadPlateSelectionOptions",
             "choosePlateSelectionLocation", "plateSelectionRowLabel",
             "renderPlateSelectionGrid", "closePlateSelectionPicker", "applyPlateSelection",
             "renderPlateSelectionField", "liquidLocationInfo", "renderLiquidLocationField",
             "renderPropertiesPanel", "serializeWorkflow")
    functions = []
    for name in names:
        match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    script = tmp_path / "plate-selection.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
class Element {
  constructor(tag='div'){this.tag=tag;this.style={};this.children=[];this.handlers={};this.text='';this.attrs={};this.className='';this.disabled=false;
    this.classList={add:name=>{this.className+=' '+name;},remove:name=>{this.className=this.className.split(' ').filter(n=>n!==name).join(' ');}};}
  appendChild(child){this.children.push(child);return child;}
  append(...children){this.children.push(...children);}
  setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(event,callback){this.handlers[event]=callback;}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text+this.children.map(child=>child.textContent||'').join('');}
}
const elements=new Map();
for(const id of ['plate-selection-modal','plate-selection-grid','plate-selection-context','plate-selection-status','plate-selection-apply','plate-selection-location','plate-selection-location-field','properties-body'])elements.set(id,new Element());
const document={getElementById(id){assert.ok(elements.has(id),id);return elements.get(id);},createElement(tag){return new Element(tag);}};
let plateSelectionDraft=null, dirty=0;
const liquid={id:5,type:'liquid/Dispense',title:'Dispense sample',properties:{location:5,anchor:'A1'},setDirtyCanvas(){}};
const tips={id:3,type:'tips/TipsOn',properties:{head_mode:{subset_type:'all_barrels'}}};
const graph={_nodes:[tips,liquid],serialize(){return structuredClone({nodes:this._nodes,links:[[1,3,0,5,0]]});}};
// LiteGraph serialization omits methods.
graph.serialize=()=>({nodes:graph._nodes.map(n=>({id:n.id,type:n.type,properties:structuredClone(n.properties)})),links:[[1,3,0,5,0]]});
const tab={library:'',protocolMetadata:{protocol_simulation_target:{head_type:'HT_384_D_70'}}};
let activeTab=tab;
function getActiveTab(){return activeTab;}
function markActiveDirty(){dirty++;}
function isCompiledProtocolPreviewTab(){return false;}
function appendVarHint(){}
const designerState={graph,deckConfig:{'2':[{labware_id:'source384'}],'5':[{labware_id:'destination'}]}};
let currentWorkflowId='saved-workflow',currentWorkflowName='Unsaved changes';
const calls=[];
let response=null;
async function apiCall(path,method,body){calls.push({path,method,body:structuredClone(body)});return typeof response==='function'?response(body):structuredClone(response);}
const el=id=>elements.get(id);
const cells=()=>el('plate-selection-grid').children.filter(child=>child.tag==='button');
const legal=()=>cells().filter(cell=>!cell.disabled);
const covered=()=>cells().filter(cell=>cell.className.split(' ').includes('covered'));
function pattern(anchor,row,col,rows,cols,rowStride=1,colStride=1){return {anchor,row,col,covered_wells:Array.from({length:rows},(_,r)=>Array.from({length:cols},(_,c)=>({row:row+r*rowStride,col:col+c*colStride,well:'fixture'}))).flat()};}
function resolved(rows,columns,anchors,description='384 channels, all barrels',location=5){
  return {status:'resolved',message:'Native plate geometry',labware:{name:'Labcyte plate',location,rows,columns},footprint:{description,tip_node_id:3},legal_anchors:anchors,
    plate_options:[{location:2,name:'Source plate',labware_id:'source384'},{location:5,name:'Destination plate',labware_id:'destination'}]};
}
""" + "\n".join(functions) + "\n(async()=>{\n" + assertions + "\n})().catch(error=>{console.error(error);process.exitCode=1;});")
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_384_plate_has_one_start_and_preserves_invalid_saved_anchor_until_applied(tmp_path):
    _run(tmp_path, """
liquid.properties.anchor='B2';
renderPropertiesPanel(liquid);
assert.match(el('properties-body').textContent,/Plate selection/);
assert.ok(!el('properties-body').textContent.includes('A2'));
response=resolved(16,24,[pattern('A1',0,0,16,24)]);
await openPlateSelectionPicker(liquid);
assert.equal(calls[0].path,'/api/workflows/plate_selection_options');
assert.equal(calls[0].method,'POST');
assert.equal(calls[0].body.node_id,5);
assert.equal(calls[0].body.workflow.graph.nodes[0].properties.head_mode.subset_type,'all_barrels');
assert.equal(calls[0].body.workflow.protocol_simulation_target.head_type,'HT_384_D_70');
assert.deepEqual(calls[0].body.workflow.deck,designerState.deckConfig);
assert.equal(cells().length,384);
assert.equal(legal().length,1);
assert.match(legal()[0].attrs['aria-label'],/^A1 /);
assert.ok(!el('plate-selection-status').textContent.includes('Native plate geometry'));
assert.equal(el('plate-selection-apply').disabled,true);
assert.match(el('plate-selection-status').textContent,/Saved anchor B2 is not a valid placement/);
assert.equal(liquid.properties.anchor,'B2');
legal()[0].handlers.click();
assert.equal(covered().length,384);
assert.equal(plateSelectionContextCurrent(plateSelectionDraft),true,'Preview selection does not mutate serialized context');
assert.equal(liquid.properties.anchor,'B2','Preview never rewrites the saved target');
applyPlateSelection();
assert.equal(liquid.properties.anchor,'A1');
assert.equal(dirty,1);
assert.equal(plateSelectionDraft,null);
""")


def test_1536_quadrants_and_partial_columns_display_exact_server_well_patterns(tmp_path):
    _run(tmp_path, """
response=resolved(32,48,[pattern('A1',0,0,16,24,2,2),pattern('A2',0,1,16,24,2,2),pattern('B1',1,0,16,24,2,2),pattern('B2',1,1,16,24,2,2)]);
await openPlateSelectionPicker(liquid);
assert.equal(legal().length,4);
assert.equal(cells().length,1536);
legal().find(cell=>cell.attrs['aria-label'].startsWith('B2 ')).handlers.click();
assert.equal(covered().length,384);
assert.ok(covered().some(cell=>cell.attrs['aria-label'].startsWith('AF48 ')));
assert.ok(!covered().some(cell=>cell.attrs['aria-label'].startsWith('A1 ')));
assert.equal(liquid.properties.anchor,'A1');
closePlateSelectionPicker();
response=resolved(16,24,[pattern('A9',0,8,16,1),pattern('A10',0,9,16,1)],'One mounted column');
await openPlateSelectionPicker(liquid);
assert.equal(legal().length,2);
assert.match(el('plate-selection-context').textContent,/One mounted column/);
legal()[1].handlers.click();
assert.equal(covered().length,16);
assert.ok(covered().every(cell=>/^[A-P]10 /.test(cell.attrs['aria-label'])));
""")


def test_unresolved_failed_or_stale_contexts_never_enable_selection(tmp_path):
    _run(tmp_path, """
response={status:'unresolved',message:'Tips On footprint differs between the incoming branches.'};
await openPlateSelectionPicker(liquid);
assert.equal(legal().length,0);
assert.equal(el('plate-selection-apply').disabled,true);
assert.match(el('plate-selection-status').textContent,/differs between the incoming branches/);
applyPlateSelection();assert.equal(dirty,0);
response=()=>{throw Error('Connection unavailable');};
await openPlateSelectionPicker(liquid);
assert.match(el('plate-selection-status').textContent,/Connection unavailable/);
assert.equal(legal().length,0);
let release;
response=()=>new Promise(resolve=>{release=resolve;});
const pending=openPlateSelectionPicker(liquid);
tips.properties.head_mode={subset_type:'single_barrel'};
release(resolved(16,24,[pattern('A1',0,0,16,24)]));await pending;
assert.match(el('plate-selection-status').textContent,/workflow changed/);
assert.equal(legal().length,0);
response=resolved(16,24,[pattern('A1',0,0,16,24)]);
await openPlateSelectionPicker(liquid);
designerState.deckConfig['5']=[{labware_id:'different-plate'}];
applyPlateSelection();
assert.equal(dirty,0);
assert.equal(el('plate-selection-apply').disabled,true);
assert.equal(liquid.properties.anchor,'A1');
""")


def test_typed_iteration_and_variable_anchors_round_trip_without_fixed_selection(tmp_path):
    _run(tmp_path, """
for(const expression of ['iter:A1,A2,B1,B2','var:plate_anchor']){
  liquid.properties.anchor=expression;
  renderPropertiesPanel(liquid);
  const field=el('properties-body').children.find(child=>child.children[0]?.textContent==='Plate selection');
  const input=field.children[1].children[0];
  assert.equal(input.value,expression);
  input.handlers.change();
  assert.equal(liquid.properties.anchor,expression);
  response=resolved(16,24,[pattern('A1',0,0,16,24)]);
  await openPlateSelectionPicker(liquid);
  assert.match(el('plate-selection-status').textContent,/replaces this expression/);
  assert.equal(el('plate-selection-apply').disabled,true);
  closePlateSelectionPicker();
  assert.equal(liquid.properties.anchor,expression);
}
""")


def test_resolved_plate_with_no_fitting_anchor_shows_disabled_grid_and_native_reason(tmp_path):
    _run(tmp_path, """
response=resolved(8,12,[]);
response.message='The mounted tips cannot fit this plate pitch within the travel limits. SuperDex rehearsal is still required.';
await openPlateSelectionPicker(liquid);
assert.equal(cells().length,96);
assert.equal(legal().length,0);
assert.equal(covered().length,0);
assert.equal(el('plate-selection-apply').disabled,true);
assert.match(el('plate-selection-status').textContent,/cannot fit this plate pitch/);
assert.match(el('plate-selection-status').textContent,/SuperDex rehearsal is still required/);
assert.equal(plateSelectionDraft.result.status,'resolved');
assert.equal(liquid.properties.anchor,'A1');
applyPlateSelection();
assert.equal(dirty,0);
""")


def test_rack_target_offers_plate_without_switching_and_cancel_preserves_saved_target(tmp_path):
    _run(tmp_path, """
liquid.properties.location=1;
liquid.properties.anchor='B2';
tips.properties.head_mode={subset_type:'single_column',subset_index:8};
designerState.deckConfig={'1':[{labware_id:'tip-rack'}],'2':[{labware_id:'source384'}]};
const original=serializeWorkflow();
response={status:'unresolved',message:'Position 1 contains a tip rack, not a plate.',
  plate_options:[{location:2,name:'Source 384 plate',labware_id:'source384'}]};
await openPlateSelectionPicker(liquid);
assert.equal(calls.length,1,'A single available plate does not trigger an automatic switch');
assert.equal(plateSelectionDraft.location,1);
assert.equal(el('plate-selection-location-field').hidden,false);
assert.equal(el('plate-selection-location').value,'');
assert.deepEqual(el('plate-selection-location').children.map(option=>option.value),['','2']);
assert.match(el('plate-selection-location').textContent,/Position 2.*Source 384 plate/);
assert.match(el('plate-selection-status').textContent,/tip rack/);
assert.equal(legal().length,0);
assert.equal(el('plate-selection-apply').disabled,true);
await choosePlateSelectionLocation('1');
await choosePlateSelectionLocation('9');
assert.equal(calls.length,1,'Only returned plate locations may be queried');
response=resolved(16,24,[pattern('A9',0,8,16,1),pattern('A10',0,9,16,1)],'One mounted column',2);
response.plate_options=[{location:2,name:'Source 384 plate',labware_id:'source384'}];
await choosePlateSelectionLocation('2');
const expected=structuredClone(original);
expected.graph.nodes.find(node=>node.id===liquid.id).properties.location=2;
assert.deepEqual(calls[1].body.workflow,expected,'Preview overrides only the selected liquid task location');
assert.deepEqual(serializeWorkflow(),original,'Preview leaves the live workflow untouched');
assert.equal(plateSelectionContextCurrent(plateSelectionDraft),true);
assert.equal(el('plate-selection-location').value,'2');
assert.equal(cells().length,384);
assert.equal(legal().length,2);
legal().find(cell=>cell.attrs['aria-label'].startsWith('A10 ')).handlers.click();
assert.equal(covered().length,16);
assert.ok(covered().every(cell=>/^[A-P]10 /.test(cell.attrs['aria-label'])));
closePlateSelectionPicker();
assert.equal(plateSelectionDraft,null);
assert.deepEqual(serializeWorkflow(),original,'Cancel preserves the saved rack target and well');
assert.equal(dirty,0);
""")


def test_apply_saves_chosen_plate_and_starting_well_together(tmp_path):
    _run(tmp_path, """
liquid.properties.location=1;
liquid.properties.anchor='B2';
response={status:'unresolved',message:'Position 1 contains a tip rack.',
  plate_options:[{location:2,name:'Source plate',labware_id:'source384'}]};
await openPlateSelectionPicker(liquid);
response=resolved(16,24,[pattern('A9',0,8,16,1),pattern('A10',0,9,16,1)],'One mounted column',2);
await choosePlateSelectionLocation('2');
assert.equal(el('plate-selection-apply').disabled,true,'A new plate still needs a valid starting well');
legal().find(cell=>cell.attrs['aria-label'].startsWith('A10 ')).handlers.click();
assert.equal(liquid.properties.location,1);
assert.equal(liquid.properties.anchor,'B2');
assert.equal(el('plate-selection-apply').disabled,false);
applyPlateSelection();
assert.equal(liquid.properties.location,2);
assert.equal(liquid.properties.anchor,'A10');
assert.equal(dirty,1);
assert.equal(plateSelectionDraft,null);
assert.equal(serializeWorkflow().graph.nodes.find(node=>node.id===liquid.id).properties.location,2);
""")


def test_failed_plate_change_clears_old_selection_and_blocks_apply(tmp_path):
    _run(tmp_path, """
response=resolved(16,24,[pattern('A1',0,0,16,24)]);
await openPlateSelectionPicker(liquid);
assert.equal(el('plate-selection-apply').disabled,false);
let fail;
response=()=>new Promise((resolve,reject)=>{fail=reject;});
const pending=choosePlateSelectionLocation('2');
assert.equal(cells().length,0);
assert.equal(plateSelectionDraft.selected,null);
assert.equal(el('plate-selection-apply').disabled,true);
applyPlateSelection();
assert.equal(dirty,0);
fail(Error('Connection unavailable'));await pending;
assert.match(el('plate-selection-status').textContent,/Connection unavailable/);
assert.equal(el('plate-selection-apply').disabled,true);
assert.equal(cells().length,0);
applyPlateSelection();
assert.equal(dirty,0);
assert.equal(liquid.properties.location,5);
assert.equal(liquid.properties.anchor,'A1');
""")


@pytest.mark.parametrize("late_failure", [False, True])
def test_stale_plate_lookup_cannot_replace_newer_plate_geometry(tmp_path, late_failure):
    _run(tmp_path, f"const lateFailure={str(late_failure).lower()};\n" + """
liquid.properties.location=1;
liquid.properties.anchor='B2';
response={status:'unresolved',message:'Position 1 contains a tip rack.',plate_options:[
  {location:2,name:'Source plate',labware_id:'source384'},
  {location:5,name:'Destination plate',labware_id:'destination'}]};
await openPlateSelectionPicker(liquid);
let release,fail;
response=()=>new Promise((resolve,reject)=>{release=resolve;fail=reject;});
const earlier=choosePlateSelectionLocation('2');
response=resolved(16,24,[pattern('A10',0,9,16,1)],'One mounted column',5);
await choosePlateSelectionLocation('5');
legal()[0].handlers.click();
if(lateFailure)fail(Error('Old plate lookup failed'));
else release(resolved(16,24,[pattern('A1',0,0,16,24)],'384 channels, all barrels',2));
await earlier;
assert.equal(calls.length,3);
assert.equal(plateSelectionDraft.location,5);
assert.equal(plateSelectionDraft.result.labware.location,5);
assert.equal(plateSelectionDraft.selected.anchor,'A10');
assert.equal(el('plate-selection-location').value,'5');
assert.match(el('plate-selection-context').textContent,/Position 5/);
assert.equal(legal().length,1);
assert.equal(covered().length,16);
assert.ok(covered().every(cell=>/^[A-P]10 /.test(cell.attrs['aria-label'])));
assert.equal(el('plate-selection-apply').disabled,false);
assert.equal(liquid.properties.location,1);
assert.equal(liquid.properties.anchor,'B2');
assert.equal(dirty,0);
""")
