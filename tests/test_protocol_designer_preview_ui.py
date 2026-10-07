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
"""
        + functions
        + """
const workflow={id:'stored-id-must-not-survive',name:'Four source transfer',
  protocol_compiled_preview:true,protocol_session_id:'session-1',protocol_revision:4,
  deck:{'1':[{material_id:'tips-4'}]},graph:{nodes:[
    {id:1,type:'flow/Start',properties:{},pos:[0,0],size:[100,50]},
    {id:2,type:'liquid/Aspirate',properties:{volume:5,_source_citation:{paragraph_id:'p1'}},pos:[0,100],size:[200,100]},
    {id:3,type:'liquid/Dispense',properties:{volume:5},pos:[0,220],size:[200,100]},
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
