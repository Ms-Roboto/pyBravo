/** Designer chat UI test. Requires an isolated simulation server and Playwright.
 * PYBRAVO_BROWSER_TEST_URL=http://127.0.0.1:8771 node tests/browser/protocol_designer_chat.cjs
 * Chat responses are mocked; existing Designer APIs and LiteGraph are real.
 * Tests cannot invoke a physical workflow or the LLM.
 */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.PYBRAVO_BROWSER_TEST_URL;
if (!base) throw new Error('Set PYBRAVO_BROWSER_TEST_URL to an isolated simulation server.');
async function api(path, body) {
    const response = await fetch(base + path, {method: body ? 'POST' : 'GET', headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
    const value = await response.json(); if (!response.ok) throw new Error(JSON.stringify(value)); return value;
}
function envelope(revision) {
    const messages=[{role:'user',content:'Wait before inspecting the plate.'},{role:'assistant',content:'How many seconds should the wait last?'}];
    if(revision>1)messages.push({role:'user',content:`Wait ${revision} seconds.`},{role:'assistant',content:`The wait is now ${revision} seconds.`});
    const questions=revision===1?[{id:'duration',path:'/steps/0/duration_s',prompt:'How many seconds should the wait last?'}]:[];
    const properties={step_id:'wait',kind:'wait',summary:revision===1?'Wait an unspecified duration.':`Wait ${revision} seconds.`,parameters:revision===1?{}:{duration_s:revision},missing_fields:revision===1?['duration_s']:[],citations:[],questions};
    const nodes=[{id:1,type:'flow/Start',title:'Protocol draft',properties:{},pos:[80,80],size:[180,70],inputs:[],outputs:[{name:'flow',type:-1,links:[1]}]},{id:2,type:'review/ProtocolStep',title:'1. Wait',properties,pos:[80,210],size:[420,170],inputs:[{name:'flow',type:-1,link:1}],outputs:[{name:'flow',type:-1,links:[2]}]},{id:3,type:'flow/End',title:'End of draft',properties:{},pos:[80,440],size:[180,70],inputs:[{name:'flow',type:-1,link:2}],outputs:[]}];
    const tipbox={labware_id:'verified-rack',labware_name:'Shared ST tipbox',tip_definition_id:'st10',tip_name:'ST 10 µL',rows:8,cols:12,wells:96,spacing_x_mm:9,spacing_y_mm:9,tip_capacity_ul:10,tip_length_mm:20};
    const tip70={...tipbox,tip_definition_id:'st70',tip_name:'ST 70 µL',tip_capacity_ul:70,tip_length_mm:null,execution_ready:false,missing_metadata:['tip_length']};
    const tipboxChoices=revision===1?[tipbox,tip70,{...tipbox,labware_id:'unknown-rack',spacing_x_mm:0}]:revision===2?[tip70,tipbox]:[];
    const materials=[{id:'tips',role:'tips',labware_id:'verified-rack',tip_definition_id:revision>1?'st70':null}];
    const candidates=[{labware_id:'incomplete-rack',labware_name:'Incomplete catalog tipbox',rows:0,cols:0,wells:96,spacing_x_mm:9,spacing_y_mm:9,verified:false,missing_metadata:['rows_cols','tip_link']}];
    const deck={'1':[{material_id:'tips',labware_id:'verified-rack',name:'Shared ST tipbox',kind:'tip_box',tip_inventory_status:'unconfirmed',proposed:true}],
        '9':[1,2,3,4].map(index=>({material_id:'source'+index,labware_id:'plate-384',name:'Source '+index,kind:'sbs_plate',proposed:true}))};
    const preview={protocol_materials:materials,tipbox_catalog_candidates:candidates,tipbox_choices:tipboxChoices,tipbox_choices_reason:revision<3?'':'No verified compatible tipbox remains in this test catalog.',head_type:'ST',name:'Incubation chat',description:'',deck,protocol_chat_draft:true,protocol_chat_session_id:'chat1',protocol_revision:revision,protocol_questions:questions,questions,graph:{nodes,links:[[1,1,0,2,0,-1],[2,2,0,3,0,-1]],last_node_id:3,last_link_id:2,version:.4}};
    return {session:{id:'chat1',revision,name:preview.name,chat_messages:messages,plan:{materials,questions}},reply:messages.at(-1).content,preview};
}
(async()=>{
    assert.equal((await api('/api/protocols/context')).controller_type,'simulation');
    const existing=await api('/api/workflows',{name:'Existing scientist workflow',deck:{},graph:{nodes:[{id:1,type:'flow/Start',properties:{},pos:[120,200],inputs:[],outputs:[{name:'flow',type:-1,links:[1]}]},{id:2,type:'flow/End',properties:{},pos:[520,200],inputs:[{name:'flow',type:-1,link:1}],outputs:[]}],links:[[1,1,0,2,0,-1]],last_node_id:2,last_link_id:1}});
    const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1600,height:1000},ignoreHTTPSErrors:true});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    // Inspect the live module's serializer without exposing a production debug API.
    await page.route(base+'/designer**',async route=>{
        const response=await route.fetch();
        const body=(await response.text()).replace('function serializeWorkflow() {','window.__protocolWorkflowForTest = serializeWorkflow;\nfunction serializeWorkflow() {');
        return route.fulfill({response,body});
    });
    let revision=1, requests=[], fail=null;
    await page.route(base+'/api/protocols/chat',async route=>{
        const body=route.request().postDataJSON();requests.push(body);
        await new Promise(resolve=>setTimeout(resolve,250));
        if(fail)return route.fulfill({status:fail,json:{detail:fail===409?'Protocol changed in another request.':'Local model temporarily unavailable.'}});
        if(requests.length===1)assert.deepEqual(Object.keys(body),['message']);
        else {assert.equal(body.session_id,'chat1');assert.equal(body.revision,revision);revision+=1;}
        return route.fulfill({json:envelope(revision)});
    });
    await page.route(base+'/api/protocols/chat1/chat-preview',route=>route.fulfill({json:envelope(revision)}));
    try {
        await page.goto(base+'/designer?workflow='+existing.id);
        await page.waitForFunction(()=>document.querySelector('.wf-tab-name')?.textContent==='Existing scientist workflow');
        const original=await page.evaluate(()=>window.designerState.graph.serialize());
        await page.locator('#btn-protocol-chat').click();
        await page.locator('#protocol-chat-capabilities summary').waitFor({state:'visible'});
        await page.locator('#protocol-chat-capabilities summary').click();
        assert.match(await page.locator('#protocol-chat-capabilities').innerText(),/transfer/);
        assert.match(await page.locator('#protocol-chat-capabilities').innerText(),/Aspirate → Dispense/);
        await page.locator('#protocol-chat-message').fill('Wait before inspecting the plate.');
        await page.locator('#protocol-chat-send').click();
        assert.equal(await page.locator('#protocol-chat-message').isDisabled(),true);
        await page.locator('#protocol-draft-banner').waitFor({state:'visible'});
        await page.waitForFunction(()=>!document.querySelector('#protocol-chat-message').disabled);
        assert.equal(await page.locator('.wf-tab').count(),2,'Chat must preserve the existing workflow tab');
        assert.equal(await page.locator('#btn-simulate').isDisabled(),true);
        assert.equal(await page.locator('#btn-execute').isDisabled(),true);
        assert.equal(await page.locator('#btn-save').isDisabled(),true);
        assert.deepEqual(await page.evaluate(()=>window.designerState.deckConfig),envelope(1).preview.deck,'Chat draft should display proposed deck stacks');
        assert.match(await page.locator('.deck-cell[data-loc="1"] .deck-label').innerText(),/tips unconfirmed/);
        assert.match(await page.locator('.deck-cell[data-loc="9"] .deck-label').innerText(),/Source 4 \(4\)/);
        assert.match(await page.locator('#protocol-draft-summary').innerText(),/Proposed deck layout/);
        assert.equal(await page.locator('#protocol-chat-reload').isVisible(),false);
        assert.match(await page.locator('#protocol-chat-questions').innerText(),/How many seconds/);
        assert.equal(await page.locator('#protocol-chat-review').getAttribute('href'),'/protocol-assistant?session=chat1');
        assert.equal(await page.evaluate(()=>window.designerState.graph._nodes.filter(n=>n.type==='review/ProtocolStep').length),1);
        assert.equal(await page.evaluate(()=>window.designerState.graphCanvas.read_only),true);
        await page.locator('#protocol-chat-message').fill('Wait 2 seconds.');
        assert.equal(await page.locator('#protocol-chat-tipbox-select option').count(),3,'Both tip definitions share one rack; do not show options with missing geometry');
        assert.equal(await page.locator('#protocol-chat-tipbox-select').inputValue(),'','Multiple compatible tips require an explicit choice');
        assert.match(await page.locator('#protocol-chat-tipbox-select').innerText(),/ST 10 µL/);
        assert.match(await page.locator('#protocol-chat-tipbox-select').innerText(),/ST 70 µL/);
        assert.match(await page.locator('#protocol-chat-tipbox-select').innerText(),/Planning only: tip length missing/);
        await page.locator('#protocol-chat-tipbox-candidates summary').click();
        assert.match(await page.locator('#protocol-chat-tipbox-candidates').innerText(),/Incomplete catalog tipbox/);
        assert.match(await page.locator('#protocol-chat-tipbox-candidates').innerText(),/rack rows and columns, linked tip definition/);
        assert.equal(await page.locator('#protocol-chat-tipbox-candidates button, #protocol-chat-tipbox-candidates select, #protocol-chat-tipbox-candidates input').count(),0,'Candidates have no selection action');

        const selectedPair=JSON.stringify(['verified-rack','st70']);
        await page.locator('#protocol-chat-tipbox-select').selectOption(selectedPair);
        await page.screenshot({path:'/tmp/protocol-designer-tipbox-choices.png'});
        const requestsBeforeChoice=requests.length;
        await page.locator('#protocol-chat-tipbox-use').click();
        assert.match(await page.locator('#protocol-chat-message').inputValue(),/^Wait 2 seconds\./);
        assert.match(await page.locator('#protocol-chat-message').inputValue(),/labware_id: verified-rack/);
        assert.match(await page.locator('#protocol-chat-message').inputValue(),/tip_definition_id: st70/);
        assert.match(await page.locator('#protocol-chat-message').inputValue(),/tip length is missing/);
        assert.equal(requests.length,requestsBeforeChoice,'Choice populates composer; explicit Send is required');
        await page.locator('#protocol-chat-send').click();
        await page.waitForFunction(()=>document.querySelector('#protocol-chat-status').textContent.includes('revision 2'));
        assert.equal(await page.locator('.wf-tab').count(),2,'Followup must update its draft tab');
        assert.match(requests.at(-1).message,/tip_definition_id: st70/);
        assert.equal(await page.locator('#protocol-chat-tipbox-select').inputValue(),selectedPair,'Catalog reorder must not switch the selected tip');
        assert.deepEqual(await page.evaluate(()=>window.__protocolWorkflowForTest().protocol_materials),envelope(2).preview.protocol_materials,'Selected pair survives the generated draft workflow');
        assert.equal(await page.evaluate(()=>window.designerState.graph._nodes.find(n=>n.type==='review/ProtocolStep').properties.parameters.duration_s),2);
        await page.locator('.wf-tab-name').filter({hasText:'Existing scientist workflow'}).click();
        assert.equal(await page.locator('#btn-simulate').isDisabled(),false);
        assert.equal(await page.evaluate(()=>window.designerState.graphCanvas.read_only),false);
        const preserved=await page.evaluate(()=>window.designerState.graph.serialize());
        assert.deepEqual(preserved.nodes,original.nodes,'Existing graph must remain untouched');
        assert.deepEqual(preserved.links,original.links);
        await page.locator('.wf-tab-name').filter({hasText:'Draft · Incubation chat'}).click();
        assert.deepEqual(await page.evaluate(()=>window.__protocolWorkflowForTest().protocol_materials),envelope(2).preview.protocol_materials,'Tab changes retain the exact box and tip');
        fail=422;
        await page.locator('#protocol-chat-message').fill('Add another wait.');
        await page.locator('#protocol-chat-send').click();
        await page.waitForFunction(()=>document.querySelector('#protocol-chat-status').classList.contains('error'));
        assert.equal(await page.locator('#protocol-chat-message').inputValue(),'Add another wait.');
        assert.equal(await page.evaluate(()=>window.designerState.graph._nodes.find(n=>n.type==='review/ProtocolStep').properties.parameters.duration_s),2);
        fail=409;
        await page.locator('#protocol-chat-send').click();
        await page.locator('#protocol-chat-reload').waitFor({state:'visible'});
        revision=3;
        await page.locator('#protocol-chat-reload').click();
        await page.waitForFunction(()=>document.querySelector('#protocol-chat-status').textContent.includes('restored'));
        assert.match(await page.locator('#protocol-chat-tipboxes').innerText(),/No verified compatible tipbox remains/);
        assert.equal(await page.locator('#protocol-chat-tipbox-select').count(),0);
        assert.equal(await page.locator('#protocol-chat-tipbox-candidates').getAttribute('open'),'');
        assert.equal(await page.locator('#protocol-chat-message').inputValue(),'Add another wait.');
        assert.equal(await page.evaluate(()=>window.designerState.graph._nodes.find(n=>n.type==='review/ProtocolStep').properties.parameters.duration_s),3);
        fail=null;
        await page.locator('#protocol-chat-send').click();
        await page.waitForFunction(()=>document.querySelector('#protocol-chat-status').textContent.includes('revision 4'));
        assert.equal(await page.locator('#protocol-chat-reload').isVisible(),false);
        await page.screenshot({path:'/tmp/protocol-designer-chat.png'});
        await page.reload();
        await page.waitForFunction(()=>document.querySelectorAll('.wf-tab').length===2);
        assert.equal(await page.locator('.wf-tab.active .wf-tab-name').innerText(),'Existing scientist workflow','Saved chat must not replace an explicitly requested workflow on reload');
        await page.locator('#btn-protocol-chat').click();
        await page.locator('.wf-tab-name').filter({hasText:'Draft · Incubation chat'}).click();
        assert.deepEqual(await page.evaluate(()=>window.__protocolWorkflowForTest().protocol_materials),envelope(4).preview.protocol_materials,'Resumed preview retains the selected pair');
        assert.equal(await page.evaluate(()=>window.designerState.graph._nodes.find(n=>n.type==='review/ProtocolStep').properties.parameters.duration_s),4);
        await page.locator('#protocol-chat-new').click();
        assert.equal(await page.locator('#protocol-chat-messages .protocol-chat-message').count(),0);
        assert.equal(await page.locator('.wf-tab').count(),2,'New chat retains previous drafts');
        assert.deepEqual(errors,[]);
        console.log('Designer chat first turn/followup/error/conflict/resume/tab-preservation tests passed.');
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
