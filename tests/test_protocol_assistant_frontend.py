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
const preview={...reviewed,id:'chat-preview',protocol_chat_draft:true,protocol_chat_session_id:'chat1'};
delete preview.protocol_session_id;
deserializeWorkflow(preview);
assert.equal(serializeWorkflow().protocol_chat_draft,true);
assert.equal(serializeWorkflow().protocol_chat_session_id,'chat1');
"""
    script = tmp_path / "designer-roundtrip.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
