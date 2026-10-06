"""Frontend checks for the reviewed protocol boundary (Node, no hardware)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed to exercise frontend JavaScript")


def test_protocol_assistant_and_designer_javascript_parse(tmp_path):
    for name in ("protocol_assistant", "designer"):
        html = (ROOT / "frontend" / f"{name}.html").read_text()
        scripts = re.findall(r'<script(?: type="module")?>([\s\S]*?)</script>', html)
        assert scripts
        source = tmp_path / f"{name}.mjs"
        source.write_text("\n".join(scripts))
        subprocess.run([NODE, "--check", str(source)], check=True, capture_output=True, text=True)


def test_source_selection_invalidates_setup_recommendations(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    selection = re.search(r"function sourceSelectionChanged\(\)\{[^\n]*\}", html)
    schedule = re.search(r"function scheduleSetupRecommendations\(\)\{[\s\S]*?\n\}", html)
    assert selection and schedule
    source = (
        """
const assert = require('node:assert/strict');
const state={session:{id:'session',plan:{steps:[]},setup:{},selected_paragraph_ids:['p1']},
  capabilities:{context_hash:'catalog'},setupRecommendationKey:null,setupRecommendations:null,
  setupRecommendationRequest:0,setupRecommendationTimer:null};
const pending=[],requests=[];
const reviewed={checked:true};
let dirtyCalls=0;
function $(id){assert.equal(id,'source-reviewed');return reviewed;}
function dirty(){dirtyCalls++;}
function renderSetupRecommendations(){}
function renderPlan(){}
function renderSetup(){}
function notify(){}
function clearTimeout(){}
function setTimeout(callback){pending.push(callback);return pending.length;}
async function api(path,options){assert.equal(path,'/setup-recommendations');
  requests.push(JSON.parse(options.body));return {context_hash:'catalog',recommendations:[]};}
"""
        + selection.group(0)
        + "\n"
        + schedule.group(0)
        + """
(async()=>{
  sourceSelectionChanged();
  assert.equal(reviewed.checked,false);
  assert.equal(dirtyCalls,1);
  await pending[0]();
  assert.deepEqual(requests[0].selected_paragraph_ids,['p1']);
  assert.ok(state.setupRecommendations);
  state.session.selected_paragraph_ids=['p2'];reviewed.checked=true;
  sourceSelectionChanged();
  assert.equal(reviewed.checked,false);
  assert.equal(dirtyCalls,2);
  assert.equal(state.setupRecommendations,null);
  await pending[1]();
  assert.deepEqual(requests[1].selected_paragraph_ids,['p2']);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "source-selection.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_method_lookup_uses_distribute_aliquots_and_records_offered_methods(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("methodQuery", "recordMethodSelection", "safeMethodSourceUrl"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const clone=value=>JSON.parse(JSON.stringify(value));
const list=value=>Array.isArray(value)?value:[];
const recorded=[];
function decide(path,value,reason){recorded.push({path,value,reason});}
const state={session:{setup:{head_mode:{subset_type:'all_barrels'}},plan:{materials:[
  {id:'source',labware_id:'384-plate',reagent_family:'aqueous'},
  {id:'target-a',labware_id:'1536-plate'},
  {id:'target-b',labware_id:'1536-plate'},
  {id:'tips',labware_id:'384-st-box',tip_definition_id:'st10'}
]}}};
"""
        + "\n".join(functions)
        + """
const query=methodQuery({kind:'distribute',source:'source',source_anchor:'A1',
  dispenses:[{destination:'target-a',volume_ul:5},{destination:'target-b',volume_ul:5}]},'tips');
assert.equal(query.volume_ul,10);
assert.deepEqual(query.dispense_volumes_ul,[5,5]);
assert.equal(query.tip_id,'st10');
assert.equal(query.tipbox_id,'384-st-box');
const chosen={method_id:'aqueous-st10',revision:'sha-selected'};
const offered=[{...chosen,rank:99,match_kind:'exact',execution_ready:true,mismatches:[]},
  {method_id:'aqueous-st10-near',revision:'sha-near',match_kind:'closest',
   execution_ready:false,mismatches:[{field:'reagent_families',reason:'different'}]}];
recordMethodSelection('/steps/0',chosen,offered,'Scientist reviewed reagent difference.');
assert.equal(recorded[0].path,'/steps/0/method_selection');
assert.deepEqual(recorded[0].value.selected,chosen);
assert.deepEqual(recorded[0].value.offered.map(item=>item.rank),[1,2]);
assert.equal(recorded[0].value.offered[1].mismatches[0].field,'reagent_families');
assert.equal(recorded[0].reason,'Scientist reviewed reagent difference.');
assert.equal(safeMethodSourceUrl('javascript:alert(1)'),null);
assert.equal(safeMethodSourceUrl('https://example.org/method'),'https://example.org/method');
"""
    )
    script = tmp_path / "method-selection.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_bulk_method_suggestions_record_proposals_at_nested_step_paths(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("recordMethodSelection", "suggestMethods"):
        match = re.search(rf"(?:async )?function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const clone=value=>JSON.parse(JSON.stringify(value));
const list=value=>Array.isArray(value)?value:[];
const decisions=[];
function decide(path,value,reason){decisions.push({path,value,reason});}
function methodQuery(){return {volume_ul:5};}
function suggestedMethodRack(){return 'tips';}
let dirtied=0;
function dirty(){dirtied++;}
function renderPlan(){}
function notify(){}
const state={session:{plan:{steps:[
  {kind:'transfer'},
  {kind:'repeat',steps:[{kind:'distribute'}]}
]}}};
async function api(){return {candidates:[{method_id:'reviewed-water',revision:'pin-1',
  method_ref:{method_id:'reviewed-water',revision:'pin-1'},execution_ready:true,
  match_kind:'exact',mismatches:[]}]};}
"""
        + "\n".join(functions)
        + """
(async()=>{
  await suggestMethods();
  assert.equal(dirtied,1);
  assert.deepEqual(decisions.filter(item=>item.path.endsWith('/method_selection'))
    .map(item=>item.path),['/steps/0/method_selection','/steps/1/steps/0/method_selection']);
  assert.ok(decisions.filter(item=>item.path.endsWith('/method_selection'))
    .every(item=>item.value.selection_mode==='bulk_proposal'&&item.reason.includes('resolver proposed')));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "bulk-method-suggestions.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_review_shows_method_differences_fallback_strokes_and_software_deck_state(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    assert "list(issues).map(renderValidationIssue)" in html
    assert "panel.append(renderRuntimeSnapshot(data.runtime_snapshot))" in html
    functions = []
    for name in ("reviewMethodValue", "renderValidationIssue", "renderRuntimeSnapshot"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const textValue=value=>value==null?'':typeof value==='object'?JSON.stringify(value):String(value);
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity),
  append(...more){this.children.push(...more.flat(Infinity));}};}
function text(node){return node==null?'':typeof node==='string'||typeof node==='number'?String(node):
  node.children.map(text).join(' ');}
"""
        + "\n".join(functions)
        + """
const mismatch=renderValidationIssue({code:'method_mismatch',severity:'warning',path:'/steps/0/method_ref',
  message:'Method differs',differences:[
    {field:'reagent_families',actual:'viscous',method:['aqueous']},
    {field:'volume_ul',phase:'dispense',target_index:1,actual:12,
     method:{min_volume_ul:1,max_volume_ul:10}}
  ]});
assert.match(text(mismatch),/Reagent family.*viscous.*aqueous/s);
assert.match(text(mismatch),/Volume.*dispense, target 2.*12.*1–10 µL per stroke/s);
const fallback=renderValidationIssue({code:'distribute_calibration_fallback',severity:'warning',
  message:'Paired strokes required',reasons:['split_calibration','commanded_tip_capacity'],
  commanded_total_ul:11,commanded_dispenses_ul:[5,5]});
assert.match(text(fallback),/Combined aspiration correction differs/);
assert.match(text(fallback),/Corrected combined aspiration plus air gaps exceeds tip capacity/);
assert.match(text(fallback),/Commanded combined aspiration: 11 µL/);
assert.match(text(fallback),/ordered commanded dispenses: 5, 5 µL/);
const snapshot=renderRuntimeSnapshot({status:'software_known_unverified',physically_verified:false,
  occupied_slots:[{slot:5,labware_names:['ST tip box']}],
  tipbox_inventory:[{slot:5,labware_name:'ST tip box',tip_id:'st_10ul',
    software_occupied_count:2,well_count:384}]});
assert.match(text(snapshot),/not a physically verified load/);
assert.match(text(snapshot),/position 5.*2 occupied of 384 wells/s);
"""
    )
    script = tmp_path / "review-method-details.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_saved_plan_method_preview_is_read_only_and_deduplicates_lookups(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("renderMethodPreview", "refreshMethodPreview"):
        match = re.search(
            rf"async function {name}\([^\n]*\)\{{[\s\S]*?\n\}}|function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html
        )
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity)};}
const panel={hidden:true,children:[],replaceChildren(...items){this.children=items;},
  append(...items){this.children.push(...items);}};
function $(id){assert.equal(id,'method-preview');return panel;}
let lookups=0;
async function api(path){assert.equal(path,'/methods/lookup');lookups++;
  return {issues:[],candidates:[{method_id:'water-st10',match_kind:'exact',execution_ready:true,
    mismatches:[]}]};}
function methodQuery(){return {operation:'transfer',tip_id:'st10',volume_ul:5};}
function suggestedMethodRack(){return 'tips';}
const state={session:{id:'s1',setup:{},plan:{materials:[],steps:[
  {id:'a',kind:'transfer',description:'Copy A'},
  {id:'b',kind:'transfer',description:'Copy B'}]}},
  capabilities:{context_hash:'catalog'},methodsRegistry:{digest:'registry'},
  methodPreview:null,methodPreviewKey:null,methodPreviewRequest:0,dirty:false};
"""
        + "\n".join(functions)
        + """
(async()=>{
  await refreshMethodPreview();
  assert.equal(lookups,1,'Identical queries should be shared');
  assert.equal(state.methodPreview.items.length,2);
  assert.ok(state.methodPreview.items.every(item=>item.status==='candidate'));
  assert.ok(state.session.plan.steps.every(step=>!step.method_ref),'Read-only preview must never pin');
  await refreshMethodPreview();
  assert.equal(lookups,1,'Unchanged saved plan should not requery');
  state.session.plan.steps[0].description='Changed saved plan';
  await refreshMethodPreview();
  assert.equal(lookups,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "method-preview.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_designer_preserves_reviewed_properties_and_metadata(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    functions = []
    for name in ("serializeWorkflow", "configureWorkflowGraph", "deserializeWorkflow"):
        match = re.search(rf"function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    # Emulate the significant LiteGraph behavior: configure merges constructor
    # defaults into the supplied properties. The real helper must remove only
    # the injected defaults for a reviewed workflow, keeping explicit edits.
    source = (
        """
const assert = require('node:assert/strict');
class LGraph {
    constructor(){this.nodes=[];}
    configure(graph){this.nodes=graph.nodes.map(n=>({...n,properties:{volume:50,tip_touch:true,pipette_technique:'',...n.properties}}));}
    getNodeById(id){return this.nodes.find(n=>n.id===id);}
    serialize(){return {nodes:this.nodes};}
}
const openTabs=[];
let activeTabIdx=-1, currentWorkflowId=null, currentWorkflowName='';
const designerState={graph:null,deckConfig:{}};
const getActiveTab=()=>openTabs[activeTabIdx];
const tabIndexForId=id=>openTabs.findIndex(tab=>tab.id===id);
function switchToTab(i){activeTabIdx=i;const tab=getActiveTab();designerState.graph=tab.graph;designerState.deckConfig=tab.deckConfig;currentWorkflowId=tab.id;currentWorkflowName=tab.name;}
function attachDirtyHooks(){}
function updateDeckCell(){}
function syncDeckTo3D(){}
function renderTabStrip(){}
"""
        + "\n".join(functions)
        + """
const reviewed={id:'wf',name:'Reviewed',description:'A protocol',protocol_session_id:'session',protocol_revision:3,protocol:{run_sheet:{tips_required:1}},deck:{'1':[{labware_id:'tips',tip_definition_id:'tip70',tipbox_fill_state:{available_wells:['A1']}}]},graph:{nodes:[{id:1,type:'liquid/Aspirate',properties:{location:1,volume:20,tip_touch:false,_source_citation:{paragraph_id:'p1'}}}]}};
deserializeWorkflow(reviewed);
let result=serializeWorkflow();
assert.deepEqual(result.graph.nodes,reviewed.graph.nodes);
assert.deepEqual(result.deck,reviewed.deck);
for(const key of ['protocol_session_id','protocol_revision','protocol'])assert.deepEqual(result[key],reviewed[key]);
getActiveTab().graph.getNodeById(1).properties.volume=21;
assert.equal(serializeWorkflow().graph.nodes[0].properties.volume,21);
// Refreshing an existing tab, and loading a pristine initial tab, use the same helper.
deserializeWorkflow(reviewed);
assert.deepEqual(serializeWorkflow().graph.nodes,reviewed.graph.nodes);
const ordinary=new LGraph();configureWorkflowGraph(ordinary,{graph:reviewed.graph});
assert.equal(ordinary.getNodeById(1).properties.pipette_technique,'');
const preview={...reviewed,id:'chat-preview',protocol_chat_draft:true,protocol_chat_session_id:'chat1',protocol_materials:[{id:'tips',labware_id:'shared-st-box',tip_definition_id:'st70'}]};
delete preview.protocol_session_id;
deserializeWorkflow(preview);
assert.equal(serializeWorkflow().protocol_chat_draft,true);
assert.equal(serializeWorkflow().protocol_chat_session_id,'chat1');
assert.deepEqual(serializeWorkflow().protocol_materials,preview.protocol_materials);
"""
    )
    script = tmp_path / "designer-roundtrip.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
