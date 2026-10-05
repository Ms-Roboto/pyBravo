/** Isolated editor interaction check. All application HTTP is mocked.
 * NODE_PATH=/path/to/node_modules node tests/browser/labware_catalog_metadata.cjs
 * Uses the app's React/Babel CDN dependencies, cached in the OS temporary directory.
 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require('playwright');
const cache=path.join(os.tmpdir(),'pybravo-labware-browser-deps');
async function dependency(name,url){
  fs.mkdirSync(cache,{recursive:true});const file=path.join(cache,name);
  if(!fs.existsSync(file)){const res=await fetch(url);assert.ok(res.ok,`Dependency ${name}: ${res.status}`);fs.writeFileSync(file,await res.text());}
  return fs.readFileSync(file,'utf8');
}
async function run(){
  const deps=await Promise.all([
    dependency('react-18.3.1.js','https://unpkg.com/react@18.3.1/umd/react.development.js'),
    dependency('react-dom-18.3.1.js','https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js'),
    dependency('babel.js','https://unpkg.com/@babel/standalone/babel.min.js'),
  ]);
  let source=fs.readFileSync(path.join(__dirname,'../../frontend/src/LabwareDashboard.jsx'),'utf8')
    .replace(/^import .*\n/gm,'').replace(/^const DEFAULT_API_URL.*\n/gm,'')
    .replace(/^const API_URL.*$/m,"const API_URL=window.location.origin")
    .replace('export default LabwareDashboard','');
  source='const {useEffect,useMemo,useState,Suspense}=React;\n'+source+'\nReactDOM.createRoot(document.getElementById("root")).render(<LabwareDashboard />);';
  const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[],patches=[];page.on('pageerror',error=>errors.push(error.message));
  let failTips=false;
  let entry={labware_type_id:'rack',name:'384 ST documented rack',wells:384,base_class:'tip_box',tip_definition_id:'',supported_tip_ids:[],well_dimensions_mm:{rows:0,cols:0,spacing_x_mm:4.5,spacing_y_mm:4.5,disposable_tip_capacity_ul:10,custom_note:'keep'}};
  const tips=[{tip_id:'st10',label:'Short 10',compatible_heads:['HT_384_D_70','HT_16_D_ST']},{tip_id:'st30',label:'Short 30',compatible_heads:[]},{tip_id:'lt250',label:'Long 250',compatible_heads:['HT_96_D_200']}];
  await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());assert.equal(url.origin,'http://labware.test');
    if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:'<html><body style="background:#0b0b12;font-family:Arial"><div id="root"></div></body></html>'});
    if(url.pathname==='/api/tips')return route.fulfill({status:failTips?503:200,json:failTips?{detail:'Tip catalog unavailable'}:{tips}});
    if(url.pathname==='/labware/classes')return route.fulfill({json:{labware_classes:[]}});
    if(url.pathname==='/labware/types')return route.fulfill({json:{labware_types:[entry]}});
    if(url.pathname==='/labware/types/rack'&&req.method()==='PATCH'){
      const payload=req.postDataJSON();patches.push(payload);entry={...entry,...payload};return route.fulfill({json:{labware_type:entry}});
    }
    throw new Error('Unhandled mock request '+req.method()+' '+url.pathname);
  });
  try{
    await page.goto('http://labware.test/');for(const content of deps)await page.addScriptTag({content});
    const compiled=await page.evaluate(source=>Babel.transform(source,{presets:['react']}).code,source);
    await page.addScriptTag({content:compiled});
    await page.getByRole('button',{name:'Pipette/Well Definition'}).click();
    const primary=page.getByLabel('Primary tip definition'),rows=page.getByLabel('Rows',{exact:true}),cols=page.getByLabel('Columns',{exact:true});
    const save=page.getByRole('button',{name:'Save changes',exact:true});
    await page.waitForFunction(()=>document.querySelector('#rack-tip-definition')?.disabled===false);
    assert.equal(await primary.inputValue(),'');assert.equal(await rows.inputValue(),'');assert.equal(await cols.inputValue(),'');
    assert.equal(await page.getByRole('checkbox',{checked:true}).count(),0,'No links inferred from rack name or capacity');
    await rows.fill('16');await cols.fill('12');await save.click();
    await page.getByText('Rows × columns must equal the saved well count (384).',{exact:true}).waitFor();assert.equal(patches.length,0);
    await cols.fill('24');await primary.selectOption('st10');
    await page.getByRole('checkbox',{name:'st30 — Short 30',exact:true}).check();await save.click();
    await page.getByText('The primary tip definition must also be selected in supported tip IDs, or leave the supported list empty.',{exact:true}).waitFor();assert.equal(patches.length,0);
    await page.getByRole('checkbox',{name:'st10 — Short 10',exact:true}).check();await save.click();
    await page.waitForFunction(()=>document.querySelector('#rack-tip-definition')?.disabled===false);
    assert.equal(patches.length,1);assert.equal(patches[0].tip_definition_id,'st10');assert.deepEqual(patches[0].supported_tip_ids,['st30','st10']);
    assert.equal(patches[0].well_dimensions_mm.rows,16);assert.equal(patches[0].well_dimensions_mm.cols,24);assert.equal(patches[0].well_dimensions_mm.custom_note,'keep');
    const summary=await page.getByLabel('Compatible head IDs',{exact:true}).innerText();assert.match(summary,/HT_384_D_70, HT_16_D_ST/);assert.match(summary,/No compatible head IDs recorded/);assert.doesNotMatch(summary,/HT_96_D_200/);
    await page.getByRole('button',{name:'Refresh',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#rack-tip-definition')?.disabled===false);assert.equal(await primary.inputValue(),'st10');assert.equal(await cols.inputValue(),'24');
    await page.screenshot({path:'/tmp/labware-catalog-metadata.png',fullPage:true});
    failTips=true;await page.getByRole('button',{name:'Refresh',exact:true}).click();await page.getByRole('status').filter({hasText:'Tip catalog unavailable'}).waitFor();
    assert.equal(await primary.isDisabled(),true);await save.click();await page.waitForFunction(()=>Array.from(document.querySelectorAll('button')).find(button=>button.textContent==='Save changes')?.disabled===false);
    assert.equal(patches.length,2);assert.equal(patches[1].tip_definition_id,'st10');assert.deepEqual(patches[1].supported_tip_ids,['st30','st10']);
    failTips=false;entry={...entry,tip_definition_id:'removed',supported_tip_ids:['removed']};await page.getByRole('button',{name:'Refresh',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#rack-tip-definition')?.value==='removed'&&document.querySelector('#rack-tip-definition')?.disabled===false);
    assert.match(await page.getByLabel('Compatible head IDs',{exact:true}).innerText(),/removed: compatibility unavailable/);
    await save.click();await page.getByText('Choose or remove tip IDs missing from the catalog: removed.',{exact:true}).waitFor();assert.equal(patches.length,2);
    await primary.selectOption('');await page.getByRole('checkbox',{name:'removed — missing from catalog',exact:true}).click();
    assert.equal(await page.getByRole('checkbox',{name:'removed — missing from catalog',exact:true}).count(),0);await save.click();
    await page.waitForFunction(()=>document.querySelector('#rack-tip-definition')?.disabled===false);assert.equal(patches.length,3);assert.equal(patches[2].tip_definition_id,'');assert.deepEqual(patches[2].supported_tip_ids,[]);
    assert.deepEqual(errors,[]);console.log('Labware metadata browser flow passed (3 mocked saves).');
  }finally{await browser.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
