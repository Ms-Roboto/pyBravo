"""Tip setup edits persist reachable anchors rather than stale R1/C1 defaults."""
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
    geometry = re.search(r"const HEAD_GEOMETRY = \{[\s\S]*?\n\};", html).group(0)
    names = ["getHeadGeometry", "normalizeHeadModeForUi", "describeHeadMode", "getTipboxDetailAtLocation",
             "getTipboxGeometryFromDetail", "tipboxCellsForAnchor", "tipsOpFootprintCells",
             "findUpstreamFlowNode", "simulateTipboxOccupancy", "resolveNodeTipAnchor", "resolveTipSetupChange",
             "renderPropertiesPanel", "serializeWorkflow", "validateWorkflow", "runWorkflow"]
    functions = []
    for name in names:
        match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    script = tmp_path / "tip-anchor-update.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
class Element {
    constructor(tag='div'){this.tag=tag;this.children=[];this.style={};this.handlers={};this.text='';}
    appendChild(child){this.children.push(child);return child;}
    append(...children){this.children.push(...children);}
    setAttribute(){}
    addEventListener(event,fn){this.handlers[event]=fn;}
    querySelector(){return null;}
    set textContent(text){this.text=text;this.children=[];}
    get textContent(){return this.text+this.children.map(child=>child.textContent||'').join('');}
}
const body=new Element();
const document={getElementById(){return body;},createElement(tag){return new Element(tag);},createTextNode(text){const n=new Element();n.textContent=text;return n;}};
function flatten(element){return [element,...element.children.flatMap(flatten)];}
function button(label){return flatten(body).find(el=>el.tag==='button'&&el.textContent===label);}
function fieldInput(label){return body.children.find(el=>el.children[0]?.textContent===label)?.children.find(el=>el.tag==='input');}
const TIPBOX_BASE_CLASSES=new Set(['tip_box','tip_trash']);
const TRASH_BASE_CLASSES=new Set(['tip_trash']);
const start={id:1,type:'flow/Start',properties:{},outputs:[{links:[1]}]};
const tip={id:3,title:'Tips On',type:'tips/TipsOn',properties:{location:1,head_mode:{subset_type:'all_barrels',subset_config:'back_left'},tip_anchor_row:0,tip_anchor_col:0},inputs:[{link:1}],outputs:[{links:[2]}],setDirtyCanvas(){}};
const off={id:6,title:'Tips Off',type:'tips/TipsOff',properties:{location:3,tip_anchor_row:0,tip_anchor_col:0},inputs:[{link:2}],outputs:[{links:[3]}],setDirtyCanvas(){}};
const end={id:2,type:'flow/End',properties:{},inputs:[{link:3}]};
const nodes=[start,tip,off,end];
const graph={_nodes:nodes,links:{1:{origin_id:1,target_id:3},2:{origin_id:3,target_id:6},3:{origin_id:6,target_id:2}},getNodeById(id){return nodes.find(n=>n.id===id);},serialize(){return {nodes:nodes.map(n=>({id:n.id,type:n.type,properties:structuredClone(n.properties)})),links:[]};}};
const tab={dirty:false,protocolMetadata:{}};
function getActiveTab(){return tab;}
function markActiveDirty(){tab.dirty=true;}
const designerState={headType:'HT_384_D_70',graph,graphCanvas:{selected_nodes:{3:tip}},
  deckConfig:{'1':[{labware_id:'rack',tipbox_fill_state:'full'}],'2':[{labware_id:'rack',tipbox_fill_state:'full'}],'3':[{labware_id:'rack',tipbox_fill_state:'empty'}]},
  labwareCatalog:[{id:'rack',base_class:'tip_box',rows:16,cols:24,wells:384}]};
let currentWorkflowId='test',currentWorkflowName='Superdex Test';
function isCompiledProtocolPreviewTab(){return false;}
function isProtocolDraftTab(){return false;}
function isGeneratedProtocolDraftTab(){return false;}
function clearActiveNodeHighlight(){}
function timelineReset(){}
let physicalObstructionRun=null;
function resetPhysicalObstructionPreview(){physicalObstructionRun=null;}
function physicalObstructionDeckDetails(){return {};}
function recordDrafterPatch(){}
function syncActiveTabMeta(){}
const validationErrors=[];
function showValidationErrors(errors){validationErrors.push(...errors);}
let executionMode='simulate';
const API_BASE='';
const writes=[],launches=[];
async function fetch(path,options){launches.push({path,options});return {ok:true,json:async()=>({physical_engine:'SuperDex'})};}
function appendVarHint(){}
let headSave=null;
function openHeadModePicker(mode,save){headSave=save;}
const pickerCalls=[];
function openTipboxPicker(location,mode,row,col,save){pickerCalls.push({location,mode,row,col,save});}
const requests=[];
let reply=url=>({reachability:{assessed:true},legal_anchors:[{row:0,col:url.searchParams.get('purpose')==='return'?0:23}]});
async function apiCall(path,method,value){if(method){writes.push(structuredClone(value));return {id:currentWorkflowId};}const url=new URL(path,'http://test');requests.push(url);return reply(url);}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
""" + geometry + "\n" + "\n".join(functions) + "\n(async()=>{\n" + assertions + "\n})().catch(error=>{console.error(error);process.exitCode=1;});")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_head_mode_save_resolves_persists_and_reopens_verified_column(tmp_path):
    _run(tmp_path, """
renderPropertiesPanel(tip);
button('Configure…').handlers.click();
headSave({subset_type:'column',subset_config:'back_left',column_count:1});
await flush();
assert.equal(tip.properties.tip_anchor_col,23);
assert.equal(tip.properties.tip_anchor_row,0);
assert.equal(tip._tipAnchorResolution.status,'ready');
assert.equal(tab.dirty,true);
assert.match(body.textContent,/Anchor R1 C24/);
assert.equal(requests[0].searchParams.get('location'),'1');
assert.equal(requests[0].searchParams.get('column_count'),'1');
assert.equal(requests[0].searchParams.get('row_count'),'16');
assert.equal(requests[0].searchParams.get('fresh_cells').split(',').length,384);
assert.equal(off.properties.tip_anchor_col,0,'Existing legal return anchor is preserved');
const serialized=JSON.parse(JSON.stringify(serializeWorkflow()));
assert.equal(serialized.graph.nodes.find(n=>n.id===3).properties.tip_anchor_col,23);
button('Pick…').handlers.click();
assert.equal(pickerCalls.at(-1).col,23);
pickerCalls.at(-1).save(0,22);
assert.equal(tip.properties.tip_anchor_col,22);
button('Pick…').handlers.click();
assert.equal(pickerCalls.at(-1).col,22,'Reopen must read current properties, not captured original values');
""")


def test_location_change_resolves_new_position_and_lookup_failures_stay_visible(tmp_path):
    _run(tmp_path, """
tip.properties.head_mode={subset_type:'column',subset_config:'back_left',column_count:1};
tip.properties.tip_anchor_col=23;
renderPropertiesPanel(tip);
reply=url=>({reachability:{assessed:true},legal_anchors:[{row:0,col:url.searchParams.get('purpose')==='return'?0:22}]});
const location=fieldInput('location');
location.value='2';location.handlers.change();
await flush();
assert.equal(requests[0].searchParams.get('location'),'2');
assert.equal(tip.properties.tip_anchor_col,22);
assert.equal(tab.dirty,true);
reply=()=>({reachability:{assessed:true},legal_anchors:[],unreachable_anchors:[{row:0,col:23}]});
const old=structuredClone(tip.properties);
assert.equal(await resolveNodeTipAnchor(tip),false);
assert.deepEqual(tip.properties,old);
assert.equal(tip._tipAnchorResolution.status,'error');
assert.match(body.textContent,/No legal fresh-tip region is reachable at position 2/);
assert.ok(validateWorkflow().some(message=>message.includes('No legal fresh-tip region')));
reply=()=>{throw Error('Catalog offline');};
assert.equal(await resolveNodeTipAnchor(tip),false);
assert.deepEqual(tip.properties,old);
assert.match(body.textContent,/Catalog offline/);
reply=()=>({reachability:{assessed:false},legal_anchors:[{row:0,col:23}]});
assert.equal(await resolveNodeTipAnchor(tip),false);
assert.deepEqual(tip.properties,old);
assert.match(body.textContent,/Deck reachability could not be checked/);
""")


def test_late_lookup_never_overwrites_a_newer_manual_selection(tmp_path):
    _run(tmp_path, """
let deliver;
reply=()=>new Promise(resolve=>{deliver=resolve;});
const pending=resolveNodeTipAnchor(tip);
tip.properties.tip_anchor_col=21;
deliver({reachability:{assessed:true},legal_anchors:[{row:0,col:23}]});
assert.equal(await pending,false);
assert.equal(tip.properties.tip_anchor_col,21);
assert.equal(tab.dirty,false);
assert.equal(tip._tipAnchorResolution,null);
const count=requests.length;
tip.properties.location='iter:1,2';
assert.equal(await resolveNodeTipAnchor(tip),false);
assert.equal(requests.length,count,'Dynamic positions must not be guessed at as a single fixed position');
assert.equal(tip._tipAnchorResolution,null);
""")


def test_simulate_waits_for_setup_resolution_before_saving_and_blocks_lookup_failure(tmp_path):
    _run(tmp_path, """
let deliver;
reply=url=>url.searchParams.get('purpose')==='pickup'?new Promise(resolve=>{deliver=resolve;}):{reachability:{assessed:true},legal_anchors:[{row:0,col:0}]};
renderPropertiesPanel(tip);
button('Configure…').handlers.click();
headSave({subset_type:'column',subset_config:'back_left',column_count:1});
const launching=runWorkflow();
assert.equal(writes.length,0,'Cannot save old anchor while lookup is pending');
assert.equal(launches.length,0);
deliver({reachability:{assessed:true},legal_anchors:[{row:0,col:23}]});
await launching;
assert.equal(writes.length,1);
assert.equal(writes[0].graph.nodes.find(node=>node.id===3).properties.tip_anchor_col,23);
assert.equal(launches[0].path,'/api/workflows/test/simulate');
reply=()=>{throw Error('Catalog offline');};
void resolveTipSetupChange(tip);
await runWorkflow();
assert.equal(writes.length,1,'A failed placement lookup must not launch or save an invalid setup');
assert.equal(launches.length,1);
assert.ok(validationErrors.some(message=>message.includes('Catalog offline')));
""")
