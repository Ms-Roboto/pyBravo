"""The simulated compiler graph is inspectable without an approval artifact."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _function(html: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
    assert match, name
    return match.group(0)


def test_compiled_preview_is_transient_read_only_and_rejects_unsafe_payloads(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    functions = "\n".join(
        _function(html, name)
        for name in (
            "isCompiledProtocolPreviewTab",
            "isProtocolDraftTab",
            "configureWorkflowGraph",
            "labelCompiledProtocolPreviewNodes",
            "displayCompiledSimulationPreview",
        )
    )
    script = tmp_path / "compiled-preview.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
class LGraph {
  constructor(){this._nodes=[];}
  configure(graph){this._nodes=graph.nodes.map(n=>({...n,properties:{constructor_default:'unsafe',...n.properties}}));}
  getNodeById(id){return this._nodes.find(n=>n.id===id);}
}
const openTabs=[{id:null,name:'Untitled',graph:new LGraph(),deckConfig:{},dirty:false}];
let activeTabIdx=0;
const getActiveTab=()=>openTabs[activeTabIdx];
function switchToTab(index){activeTabIdx=index;}
function requestAnimationFrame(){}
const designerState={graphCanvas:null};
const document={getElementById(){return {textContent:''};}};
"""
        + functions
        + """
const workflow={id:'stored-id-must-not-survive',name:'Four source transfer',
  protocol_compiled_preview:true,protocol_session_id:'session-1',protocol_revision:4,
  deck:{'1':[{material_id:'tips-4'}]},graph:{nodes:[
    {id:1,type:'flow/Start',properties:{},pos:[0,0],size:[100,50]},
    {id:2,type:'liquid/Aspirate',title:'Transfer every source well',properties:{volume:5,location:6,anchor:'A1',_source_citation:{paragraph_id:'p1'}},pos:[0,100],size:[200,100]},
    {id:3,type:'liquid/Dispense',title:'Transfer every source well',properties:{volume:5,location:5,anchor:'B2'},pos:[0,220],size:[200,100]},
  ]}};
const preview={status:'simulation_only',session_id:'session-1',revision:4,
  read_only:true,executable:false,approved:false};
displayCompiledSimulationPreview({workflow,preview});
const tab=getActiveTab();
assert.equal(tab.id,null,'Preview cannot become a stored workflow ID');
assert.equal(tab.dirty,false);
assert.equal(tab.protocolPreviewSessionId,'session-1');
assert.equal(tab.protocolMetadata.protocol_compiled_preview,true);
assert.equal(isProtocolDraftTab(tab),true,'Existing Designer action guards must apply');
assert.deepEqual(tab.deckConfig,workflow.deck);
assert.deepEqual(tab.graph.getNodeById(2).properties,workflow.graph.nodes[1].properties,
  'LiteGraph constructor defaults must not contaminate the compiled graph');
assert.equal(tab.graph.getNodeById(2).title,'Aspirate · 5 µL · slot 6 · A1');
assert.equal(tab.graph.getNodeById(3).title,'Dispense · 5 µL · slot 5 · B2');
assert.equal(tab.graph.getNodeById(2)._protocolPreviewOriginalTitle,'Transfer every source well');
assert.equal(workflow.graph.nodes[1].title,'Transfer every source well',
  'The backend compiler payload must retain its original title');
for (const bad of [
  {...preview,executable:true},
  {...preview,approved:true},
  {...preview,read_only:false},
  {...preview,revision:5},
]) assert.throws(()=>displayCompiledSimulationPreview({workflow,preview:bad}),/safe, compiled/);
assert.equal(openTabs.length,2,'Rejected payloads cannot create tabs');
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_preview_canvas_starts_with_readable_nodes_below_banner(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    script = tmp_path / "preview-framing.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
const banner={hidden:false,getBoundingClientRect(){return {bottom:180};}};
const document={getElementById(id){assert.equal(id,'protocol-draft-banner');return banner;}};
"""
        + _function(html, "frameProtocolPreviewGraph")
        + """
const canvas={canvas:{width:320,height:450,getBoundingClientRect(){return {top:70};}},
  ds:{},resize(){},setDirty(){this.redrawn=true;}};
const nodes=[
  {pos:[0,100],size:[230,130]},
  {pos:[9100,100],size:[230,130]},
];
frameProtocolPreviewGraph(canvas,nodes,.85);
const [x,y]=canvas.ds.offset;
assert.ok(canvas.ds.scale>=.85,'First nodes should open at readable zoom');
assert.equal(x + nodes[0].pos[0]*canvas.ds.scale,25);
assert.equal(y + nodes[0].pos[1]*canvas.ds.scale,180,
  'First node must be visible below the banner, not shifted off-screen');
assert.ok(x + (nodes[0].pos[0]+nodes[0].size[0])*canvas.ds.scale < canvas.canvas.width,
  'First complete operation should fit horizontally');
assert.equal(canvas.redrawn,true);
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_read_only_preview_nodes_remain_inspectable(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    script = tmp_path / "preview-selection.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
const node={id:2,type:'liquid/Aspirate',properties:{volume:5}};
let preview=true,rendered=null,query=null;
const designerState={graphCanvas:{convertEventToCanvasOffset(event){return [event.clientX-20,event.clientY-30];}},
  graph:{getNodeOnPos(x,y){query=[x,y];return x===100&&y===200?node:null;}}};
function isProtocolDraftTab(){return preview;}
function renderPropertiesPanel(value){rendered=value;}
"""
        + _function(html, "inspectReadonlyProtocolNode")
        + """
inspectReadonlyProtocolNode({clientX:120,clientY:230});
assert.deepEqual(query,[100,200]);
assert.equal(rendered,node);
preview=false;rendered='unchanged';
inspectReadonlyProtocolNode({clientX:120,clientY:230});
assert.equal(rendered,'unchanged','Ordinary Designer selection still uses LiteGraph');
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_preview_button_and_designer_actions_keep_release_separate():
    assistant = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    designer = (ROOT / "frontend" / "designer.html").read_text()
    assert 'id="preview-designer"' in assistant
    assert "s?.simulation?.status!=='passed'" in assistant
    assert "'/designer?protocol_preview='+encodeURIComponent(state.session.id)" in assistant
    assert "params.get('protocol_preview')" in designer
    assert "(!requestedWorkflow && !requestedPreview)" in designer
    assert "if (isProtocolDraftTab()) return;" in designer
    assert "if (isProtocolDraftTab()) { progress.textContent = 'This protocol preview cannot run" in designer


def test_compiled_preview_uses_normal_primitive_names_and_restores_3d():
    html = (ROOT / "frontend" / "designer.html").read_text()
    assert 'body.protocol-preview-active:not(.protocol-compiled-preview) #viewport-float { display:none; }' in html
    assert 'body.protocol-preview-active:not(.protocol-compiled-preview) #section-tasks { display:none; }' in html
    assert 'data-type="manual">Manual</div>' in html
    assert 'data-type="wait">Wait</div>' in html
    assert "manual: 'system/Manual'" in html
    assert "wait: 'system/Wait'" in html
    assert "'plate/Destack': 'Destack'" in html
    assert "'tips/TipsOn': 'Tips On'" in html
    assert "'plate/PickPlace': 'Pick/Place'" in html
    assert "renderCompiledPreviewProperties(body, node)" in html
    assert "if (isCompiledProtocolPreviewTab()) { await replayCompiledSimulationPreview(); return; }" in html
    assert "if (isCompiledProtocolPreviewTab()) {\n        stopCompiledPreviewPlayback();\n        return;\n    }" in html


def test_replay_requires_same_saved_revision_and_all_simulation_hashes(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    script = tmp_path / "replay-integrity.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
"""
        + _function(html, "isCompiledProtocolPreviewTab")
        + _function(html, "verifyCompiledPreviewReplay")
        + """
const hashes={context_hash:'c',record_hash:'r',workflow_hash:'w'};
const tab={protocolPreviewSessionId:'session-1',protocolMetadata:{protocol_compiled_preview:true,protocol_revision:4},protocolPreviewHashes:hashes};
const preview={status:'simulation_only',session_id:'session-1',revision:4,read_only:true,executable:false,approved:false,...hashes};
const fresh={preview,workflow:{protocol_compiled_preview:true,protocol_session_id:'session-1',protocol_revision:4}};
const events=[{type:'workflow:start',time:'2026-01-01T00:00:00Z'},{type:'workflow:complete',time:'2026-01-01T00:00:01Z'}];
const session={id:'session-1',revision:4,simulation:{status:'passed',events,...hashes}};
assert.equal(verifyCompiledPreviewReplay(tab,fresh,session),events);
for (const key of Object.keys(hashes)) {
  assert.throws(()=>verifyCompiledPreviewReplay(tab,fresh,{...session,simulation:{...session.simulation,[key]:'changed'}}),/no longer matches/);
  assert.throws(()=>verifyCompiledPreviewReplay(tab,{...fresh,preview:{...preview,[key]:'changed'}},session),/no longer matches/);
}
assert.throws(()=>verifyCompiledPreviewReplay(tab,fresh,{...session,revision:5}),/no longer matches/);
assert.throws(()=>verifyCompiledPreviewReplay(tab,fresh,{...session,simulation:{...session.simulation,status:'running'}}),/no longer matches/);
assert.throws(()=>verifyCompiledPreviewReplay(tab,fresh,{...session,simulation:{...session.simulation,events:[{type:'workflow:error',time:events[0].time}]}}),/no longer matches/);
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_replay_reconstructs_tip_pickup_and_spent_return_from_primitive_tasks(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    script = tmp_path / "replay-tips.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
const changes=[];
const scene={
  getTipboxTransientState(){return {};},
  setHeadTipState(state){changes.push(state);return Promise.resolve();},
};
const rack={labware_id:'rack',name:'Dedicated ST10 tips',tip_definition_id:'st_10ul'};
const designerState={deckConfig:{'1':[rack]},labwareCatalog:[{id:'rack',rows:16,cols:24,disposable_tip_capacity_ul:10}],robotScene:scene};
const deckUpdates=[];
function updateDeckCell(location){deckUpdates.push(location);}
const timelineFrames=[];
function timelineRecord(positions,nodeId,stepName){timelineFrames.push({nodeId,stepName});}
"""
        + _function(html, "synthesizeCompiledPreviewTaskState")
        + """
synthesizeCompiledPreviewTaskState({id:2,type:'tips/TipsOn',title:'Tips On · slot 1',properties:{location:1,head_mode:{subset_type:'all_barrels'}}});
assert.equal(changes[0].tips_on_head,true);
assert.equal(changes[0].tip_definition_id,'st_10ul');
assert.equal(changes[0].tipbox_removed_cells['1'].length,384);
assert.equal(timelineFrames.length,1);
synthesizeCompiledPreviewTaskState({id:3,type:'tips/TipsOff',title:'Tips Off · slot 1',properties:{location:1}});
assert.equal(changes[1].tips_on_head,false);
assert.deepEqual(changes[1].tipbox_removed_cells['1'],[]);
assert.equal(rack.tip_inventory_status,'spent');
assert.deepEqual(deckUpdates,[1]);
assert.equal(timelineFrames.length,2);
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
