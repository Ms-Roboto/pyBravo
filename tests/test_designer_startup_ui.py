"""Opening Designer requires an explicit choice before recovering earlier work."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _functions(*names: str) -> str:
    html = (ROOT / "frontend" / "designer.html").read_text()
    sources = []
    for name in names:
        match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        sources.append(match.group(0))
    return "\n".join(sources)


def _run(tmp_path: Path, source: str) -> None:
    script = tmp_path / "designer-startup.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("search", "expected_request", "expected_name", "chat_open"),
    [
        ("", None, "Untitled Workflow", False),
        ("?workflow=saved-workflow", "/api/workflows/saved-workflow", "Chosen workflow", False),
        ("?protocol_preview=chosen-preview", "/api/protocols/chosen-preview/designer-preview", "Chosen preview", False),
        ("?protocol-chat=1", None, "Untitled Workflow", True),
    ],
)
def test_startup_does_not_adopt_stored_work(
    tmp_path, search, expected_request, expected_name, chat_open
):
    _run(
        tmp_path,
        """
const assert = require('node:assert/strict');
const PROTOCOL_CHAT_STORAGE = 'pybravo-designer-protocol-chat';
const protocolChat = {sessionId:null,envelope:null,pending:false};
const oldDraft = {name:'Old four-source protocol',deck:{'1':[{}]},graph:{nodes:[{id:1,type:'flow/Start'}]}};
const storage = new Map([
  [PROTOCOL_CHAT_STORAGE,'old-chat'],
  ['pybravo-designer-draft',JSON.stringify(oldDraft)],
]);
const storedBefore = [...storage];
const localStorage = {
  getItem(key){return storage.get(key) ?? null;},
  setItem(key,value){storage.set(key,value);},
};
const elements = new Map();
const document = {getElementById(id){
  if(!elements.has(id)) elements.set(id,{
    hidden:true,textContent:'',value:'',style:{},
    addEventListener(){},focus(){this.focused=true;},
  });
  return elements.get(id);
}};
const requests = [], tabs = [];
let activeName = null;
function initGraph(){tabs.push({name:'Untitled Workflow'});activeName=tabs[0].name;}
function deserializeWorkflow(workflow){tabs[0]=workflow;activeName=workflow.name;}
async function apiCall(path){requests.push(path);return {name:'Chosen workflow'};}
async function loadCompiledSimulationPreview(id){
  requests.push('/api/protocols/'+id+'/designer-preview');
  tabs.push({name:'Chosen preview'});activeName='Chosen preview';
}
async function protocolChatRequest(path){
  requests.push('/api/protocols'+path);
  return {session:{id:'old-chat'},preview:{}};
}
function adoptProtocolChat(envelope){protocolChat.envelope=envelope;protocolChat.sessionId=envelope.session.id;}
function displayProtocolPreview(){tabs.push({name:oldDraft.name});activeName=oldDraft.name;}
function setProtocolChatOpen(open){document.getElementById('protocol-chat-panel').hidden=!open;}
function protocolChatStatus(message){document.getElementById('protocol-chat-status').textContent=message;}
function renderProtocolConversation(){}
function markActiveDirty(){}
async function loadLabwareCatalog(){}
async function loadTipDefinitions(){}
async function loadLiquidClasses(){}
async function loadPipetteTechniques(){}
async function loadActiveProfile(){}
async function initRobotScene(){}
function wireHeadModeModal(){}
function wireTipboxModal(){}
function wirePlateSelectionModal(){}
function setupTaskChipClicks(){}
function hookWorkflowEvents(){}
function renderVarsPanel(){}
function scheduleAutoSave(){}
function setInterval(){}
"""
        + "const window = {location:{search:" + json.dumps(search) + "}};\n"
        + _functions("init", "offerProtocolChatRecovery", "readAutosavedDraft", "restoreDraft", "resumeProtocolChat")
        + "\nconst expectedRequest = " + json.dumps(expected_request) + ";\n"
        + "const expectedName = " + json.dumps(expected_name) + ";\n"
        + "const expectedChatOpen = " + json.dumps(chat_open) + ";\n"
        + """
(async()=>{
  await init();
  assert.deepEqual(requests,expectedRequest ? [expectedRequest] : [],
    'Only explicitly selected work may be requested during startup');
  assert.equal(activeName,expectedName);
  assert.equal(tabs.length,expectedName==='Chosen preview' ? 2 : 1,
    'The stored protocol must not be opened as a background tab');
  assert.equal(protocolChat.sessionId,null,'Fresh messages must not continue the stored conversation');
  assert.equal(protocolChat.envelope,null);
  assert.equal(document.getElementById('protocol-chat-panel').hidden,!expectedChatOpen);
  assert.equal(document.getElementById('protocol-chat-message').value,'');
  assert.equal(Boolean(document.getElementById('protocol-chat-message').focused),expectedChatOpen);
  assert.equal(document.getElementById('protocol-chat-reload').hidden,false);
  assert.equal(document.getElementById('protocol-chat-reload').textContent,'Resume previous chat');
  assert.deepEqual([...storage],storedBefore,'Offering recovery must preserve both stored records');
})().catch(error=>{console.error(error);process.exitCode=1;});
""",
    )


def test_saved_work_is_recovered_only_by_explicit_actions(tmp_path):
    _run(
        tmp_path,
        """
const assert = require('node:assert/strict');
const PROTOCOL_CHAT_STORAGE='pybravo-designer-protocol-chat';
const protocolChat={sessionId:null,envelope:null,pending:false};
const draft={name:'Recoverable draft',deck:{},graph:{nodes:[{id:1,type:'system/Wait'}]}};
const storage=new Map([[PROTOCOL_CHAT_STORAGE,'stored-chat'],['pybravo-designer-draft',JSON.stringify(draft)]]);
const localStorage={getItem(key){return storage.get(key) ?? null;}};
const button={hidden:true,textContent:''};
const document={getElementById(){return button;}};
const requests=[],loaded=[];
let dirty=false,chatOpen=false,preview=null;
function deserializeWorkflow(value){loaded.push(value);}
function markActiveDirty(){dirty=true;}
function renderProtocolConversation(){}
function protocolChatStatus(){}
function setProtocolChatOpen(open){chatOpen=open;}
function adoptProtocolChat(envelope){protocolChat.envelope=envelope;protocolChat.sessionId=envelope.session.id;}
function displayProtocolPreview(envelope){preview=envelope;}
async function protocolChatRequest(path){requests.push(path);return {session:{id:'stored-chat'}};}
"""
        + _functions("offerProtocolChatRecovery", "readAutosavedDraft", "restoreDraft", "resumeProtocolChat")
        + """
(async()=>{
  offerProtocolChatRecovery();
  assert.deepEqual(readAutosavedDraft(),draft,'Drafts without deck entries remain recoverable');
  assert.deepEqual(requests,[]);
  assert.deepEqual(loaded,[]);
  assert.equal(protocolChat.sessionId,null);
  assert.equal(restoreDraft(),true);
  assert.deepEqual(loaded,[draft]);
  assert.equal(dirty,true,'Recovered work must remain eligible for autosave');
  await resumeProtocolChat();
  assert.deepEqual(requests,['/stored-chat/chat-preview']);
  assert.equal(protocolChat.sessionId,'stored-chat');
  assert.equal(preview,protocolChat.envelope);
  assert.equal(chatOpen,true);
  assert.equal(button.hidden,true);
  assert.equal(protocolChat.pending,false);
  storage.set('pybravo-designer-draft','{invalid');
  assert.equal(restoreDraft(),false);
  assert.equal(loaded.length,1,'Malformed recovery data must not replace the current workflow');
})().catch(error=>{console.error(error);process.exitCode=1;});
""",
    )


def test_pristine_tabs_do_not_overwrite_autosaved_work(tmp_path):
    _run(
        tmp_path,
        """
const assert = require('node:assert/strict');
let autoSaveTimer=null,activeTab={dirty:false},protocolDraft=false;
const scheduled=new Map(),writes=[];
let nextTimer=1;
function getActiveTab(){return activeTab;}
function isProtocolDraftTab(){return protocolDraft;}
function serializeWorkflow(){return {name:'Edited workflow',graph:{nodes:[]}};}
function setTimeout(callback){const id=nextTimer++;scheduled.set(id,callback);return id;}
function clearTimeout(id){scheduled.delete(id);}
const localStorage={setItem(key,value){writes.push([key,JSON.parse(value)]);}};
"""
        + _functions("hasAutosaveContent", "scheduleAutoSave")
        + """
scheduleAutoSave();
assert.equal(scheduled.size,0,'A fresh blank tab must preserve older recovery data');
activeTab.dirty=true;
scheduleAutoSave();
assert.equal(scheduled.size,1);
activeTab={dirty:false};
scheduled.get(autoSaveTimer)();
assert.deepEqual(writes,[],'A timer from an edited tab must not save a newly selected blank tab');
activeTab.dirty=true;
scheduleAutoSave();
scheduled.get(autoSaveTimer)();
assert.deepEqual(writes,[['pybravo-designer-draft',{name:'Edited workflow',graph:{nodes:[]}}]]);
protocolDraft=true;
scheduled.get(autoSaveTimer)();
assert.equal(writes.length,1,'Review-only protocol tabs cannot replace workflow recovery data');
protocolDraft=false;
for (const content of [
  {id:'saved-workflow'},
  {library:'def helper(): pass'},
  {deckConfig:{'1':[{}]}},
  {graph:{_nodes:[{type:'system/Wait'}]}},
]) {
  activeTab={dirty:false,...content};
  assert.equal(hasAutosaveContent(),true,'Existing content stays recoverable even without a dirty flag');
}
activeTab={graph:{_nodes:[{type:'flow/Start'},{type:'flow/End'}]}};
assert.equal(hasAutosaveContent(),false,'Default Start and End nodes do not count as user work');
""",
    )
