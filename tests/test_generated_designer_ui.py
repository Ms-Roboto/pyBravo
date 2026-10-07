"""Locally generated drafts support software rehearsal without hardware release."""

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


def test_generated_draft_is_editable_and_can_simulate_without_hardware_release(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "generated-draft.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const elements = new Map();
for (const id of ['btn-save','btn-save-as','btn-library','btn-draft','btn-import',
  'btn-export','btn-simulate','btn-execute','btn-play','btn-pause','btn-stop',
  'btn-step','protocol-draft-banner','protocol-draft-review','protocol-draft-summary']) {
  elements.set(id,{disabled:false,hidden:false,dataset:{},textContent:'',title:''});
}
const label={textContent:''};
const document={body:{classList:{toggle(){}}},getElementById(id){
  if(id==='viewport-header') return {querySelector(){return {textContent:''}}};
  return elements.get(id);
},querySelector(selector){
  if(selector==='#viewport-header span') return {textContent:''};
  if(selector==='#protocol-draft-banner strong') return label;
  throw Error(selector);
}};
function requestAnimationFrame(){}
const tab={id:'generated-1',description:'Model draft',library:'',
  graph:{_nodes:[{type:'flow/Start'},{type:'liquid/Aspirate'},{type:'flow/End'}],serialize(){return {nodes:this._nodes,links:[]};}},
  deckConfig:{},protocolMetadata:{protocol_generated_draft:true,
    protocol_draft_status:'unreviewed',protocol_generated_root_id:'generated-1',
    protocol_generated_provenance:{source_kind:'text2wetlab',source_id:'task',model:'qwen'}}};
const getActiveTab=()=>tab;
let currentWorkflowId=tab.id,currentWorkflowName='Generated protocol';
const designerState={graph:tab.graph,deckConfig:{},graphCanvas:{read_only:true}};
"""
        + "\n".join(_function(html, name) for name in (
            "isCompiledProtocolPreviewTab", "isProtocolDraftTab",
            "isGeneratedProtocolDraftTab", "updateProtocolDraftControls",
            "serializeWorkflow",
        ))
        + """
assert.equal(isProtocolDraftTab(),false);
assert.equal(isGeneratedProtocolDraftTab(),true);
updateProtocolDraftControls();
assert.equal(elements.get('protocol-draft-banner').hidden,false);
assert.equal(elements.get('btn-save').disabled,false);
assert.equal(elements.get('btn-save-as').disabled,false);
assert.equal(elements.get('btn-simulate').disabled,false);
assert.equal(elements.get('btn-play').disabled,false);
assert.equal(elements.get('btn-stop').disabled,false);
assert.equal(elements.get('btn-execute').disabled,true);
assert.equal(designerState.graphCanvas.read_only,false);
assert.equal(label.textContent,'Locally generated protocol · unreviewed');
const saved=serializeWorkflow();
assert.equal(saved.protocol_generated_draft,true);
assert.equal(saved.protocol_generated_root_id,'generated-1');
assert.equal(saved.protocol_generated_provenance.model,'qwen');
assert.match(elements.get('protocol-draft-summary').textContent,/stops on task errors/);
tab.protocolMetadata = {};
updateProtocolDraftControls();
assert.equal(elements.get('btn-execute').disabled,false);
assert.equal(elements.get('protocol-draft-banner').hidden,true);
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_deserialize_preserves_generated_draft_metadata():
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    assert "'protocol_generated_draft', 'protocol_draft_status', 'protocol_generated_provenance', 'protocol_generated_root_id', 'protocol_draft_issues'" in html
    assert "if (!data.protocol_session_id && !data.protocol_generated_draft) return;" in html
    assert "if (isGeneratedProtocolDraftTab() && executionMode !== 'simulate')" in html
    assert "if (isGeneratedProtocolDraftTab()) executionMode = 'simulate';" in html


def test_generated_draft_simulation_saves_provenance_and_uses_only_simulate_route(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "generated-draft-routing.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const progress = {textContent:''};
const document = {getElementById(id) {return id === 'playback-progress' ? progress : {style:{}};}};
let executionMode = 'execute', currentWorkflowId = 'draft-1';
const API_BASE = '';
const calls = [];
function isCompiledProtocolPreviewTab(){return false;}
function isProtocolDraftTab(){return false;}
function isGeneratedProtocolDraftTab(){return true;}
function validateWorkflow(){return [];}
function clearActiveNodeHighlight(){}
function timelineReset(){}
function recordDrafterPatch(){}
function syncActiveTabMeta(){}
function serializeWorkflow(){return {name:'Draft',protocol_generated_draft:true,
  protocol_generated_provenance:{model:'qwen'},graph:{nodes:[],links:[]}};}
async function apiCall(path,method,body){calls.push({path,method,body});return {id:currentWorkflowId};}
async function fetch(path,options){calls.push({path,method:options.method});return {ok:true,json:async()=>({status:'started',mode:'simulate',simulation_kind:'draft_rehearsal',qualification_granted:false})};}
"""
        + _function(html, "runWorkflow")
        + """
(async () => {
  await runWorkflow();
  assert.equal(calls.length,0);
  assert.match(progress.textContent,/Hardware execution requires/);
  executionMode = 'simulate';
  await runWorkflow();
  assert.equal(calls.length,2);
  assert.equal(calls[0].body.protocol_generated_draft,true);
  assert.equal(calls[0].body.protocol_generated_provenance.model,'qwen');
  assert.equal(calls[1].path,'/api/workflows/draft-1/simulate');
  assert.equal(progress.textContent,'Rehearsing draft in software...');
})().catch(error => {console.error(error);process.exitCode=1;});
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_missing_liquid_class_is_visible_and_selection_marks_draft_dirty(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "missing-class.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const selects=[];
class Element {
  constructor(tag){this.tag=tag;this.style={};this.children=[];this.handlers={};}
  appendChild(child){this.children.push(child);}
  append(...children){this.children.push(...children);}
  setAttribute(){}
  addEventListener(event,callback){this.handlers[event]=callback;}
  get options(){return this.children;}
}
const body=new Element('div');
const document={getElementById(){return body;},createElement(tag){
  const element=new Element(tag);if(tag==='select')selects.push(element);return element;
}};
function isCompiledProtocolPreviewTab(){return false;}
let dirty=0;
function markActiveDirty(){dirty++;}
const designerState={liquidClasses:[{name:'EtOH'}]};
const node={type:'liquid/Aspirate',title:'Aspirate Isopropanol',
  properties:{liquid_class:'Isopropanol'},setDirtyCanvas(){}};
"""
        + _function(html, "renderPropertiesPanel")
        + """
renderPropertiesPanel(node);
assert.equal(selects.length,1);
const missing=selects[0].options.find(option=>option.selected);
assert.equal(missing.textContent,'Unavailable: Isopropanol');
assert.equal(missing.disabled,true);
assert.equal(node.properties.liquid_class,'Isopropanol');
assert.equal(dirty,0);
selects[0].value='EtOH';
selects[0].handlers.change();
assert.equal(node.properties.liquid_class,'EtOH');
assert.equal(dirty,1);
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
