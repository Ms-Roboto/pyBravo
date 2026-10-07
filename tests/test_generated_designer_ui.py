"""Locally generated protocols load as editable, non-runnable Designer drafts."""

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


def test_generated_draft_is_editable_but_run_controls_are_disabled(tmp_path):
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
assert.equal(elements.get('btn-simulate').disabled,true);
assert.equal(elements.get('btn-execute').disabled,true);
assert.equal(designerState.graphCanvas.read_only,false);
assert.equal(label.textContent,'Locally generated protocol · unreviewed');
const saved=serializeWorkflow();
assert.equal(saved.protocol_generated_draft,true);
assert.equal(saved.protocol_generated_root_id,'generated-1');
assert.equal(saved.protocol_generated_provenance.model,'qwen');
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_deserialize_preserves_generated_draft_metadata():
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    assert "'protocol_generated_draft', 'protocol_draft_status', 'protocol_generated_provenance', 'protocol_generated_root_id', 'protocol_draft_issues'" in html
    assert "if (!data.protocol_session_id && !data.protocol_generated_draft) return;" in html
    assert "if (isGeneratedProtocolDraftTab()) { progress.textContent" in html
