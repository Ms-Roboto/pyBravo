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
    source = """
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
""" + selection.group(0) + "\n" + schedule.group(0) + """
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
    script = tmp_path / "source-selection.cjs"
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
    source = """
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
""" + "\n".join(functions) + """
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
    script = tmp_path / "designer-roundtrip.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
