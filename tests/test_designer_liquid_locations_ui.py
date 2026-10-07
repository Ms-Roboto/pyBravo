"""Liquid task defaults never silently point at the first loaded tip rack."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer checks")


def _run(tmp_path, assertions):
    html = (ROOT / "frontend" / "designer.html").read_text()
    functions = []
    for name in ("liquidLocationInfo", "setNewLiquidTaskLocation", "renderLiquidLocationField",
                 "setupTaskChipClicks", "renderPropertiesPanel", "validateWorkflow"):
        match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    script = tmp_path / "liquid-locations.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
class Element {
  constructor(tag='div'){this.tag=tag;this.style={};this.children=[];this.handlers={};this.text='';this.attrs={};}
  appendChild(child){this.children.push(child);return child;}
  append(...children){this.children.push(...children);}
  setAttribute(key,value){this.attrs[key]=value;}
  getAttribute(key){return this.attrs[key];}
  addEventListener(event,callback){this.handlers[event]=callback;}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text+this.children.map(child=>child.textContent||'').join('');}
}
const body=new Element(), chips=['aspirate','dispense','mix'].map(type=>{const chip=new Element();chip.attrs['data-type']=type;return chip;});
const document={getElementById(){return body;},createElement(tag){return new Element(tag);},querySelectorAll(){return chips;}};
const CHIP_TO_NODE_TYPE={aspirate:'liquid/Aspirate',dispense:'liquid/Dispense',mix:'liquid/Mix'};
const SCRIPT_CHIP_PRESETS={};
function isProtocolDraftTab(){return false;}
function isCompiledProtocolPreviewTab(){return false;}
function appendVarHint(){}
const tab={library:''};
function getActiveTab(){return tab;}
let dirty=0;
function markActiveDirty(){dirty++;}
function node(type,location=1){return {id:3,type,title:type.split('/')[1],properties:{location},inputs:[{link:1}],outputs:[{links:[2]}],setDirtyCanvas(){}};}
const LiteGraph={createNode:node};
const start={id:1,type:'flow/Start',outputs:[{links:[1]}]};
const end={id:2,type:'flow/End',inputs:[{link:2}]};
const graph={_nodes:[],links:{1:{origin_id:1,target_id:3},2:{origin_id:3,target_id:2}},add(n){this._nodes.push(n);},getNodeById(id){return this._nodes.find(n=>n.id===id);}};
const designerState={graph,deckConfig:{'1':[{labware_id:'rack'}],'2':[{labware_id:'plate'}],'3':[{labware_id:'rack'}]},
  labwareCatalog:[{id:'rack',name:'384 ST10 Tip Box',base_class:'tip_box',kind:'sbs_plate',wells:384},
  {id:'plate',name:'384 CellVis 1.5H',base_class:'microplate',kind:'sbs_plate',wells:384},
  {id:'reservoir',name:'Buffer reservoir',base_class:'reservoir',wells:1}]};
function flatten(element){return [element,...element.children.flatMap(flatten)];}
function labeled(label){return flatten(body).find(el=>el.attrs['aria-label']===label);}
""" + "\n".join(functions) + "\n" + assertions)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_new_liquid_tasks_use_the_only_plate_but_never_redirect_existing_tasks(tmp_path):
    _run(tmp_path, """
setupTaskChipClicks();
for(const chip of chips){chip.handlers.click();assert.equal(graph._nodes.at(-1).properties.location,2);}
const imported=node('liquid/Dispense',1);
renderPropertiesPanel(imported);
assert.equal(imported.properties.location,1,'Opening existing/imported nodes must preserve the target');
assert.match(body.textContent,/Position 1.*384 ST10 Tip Box.*tip box/);
assert.match(body.textContent,/liquid tasks require a plate or reservoir/);
assert.equal(dirty,0);
const select=labeled('Loaded liquid labware');
assert.equal(select.children.length,2,'Prompt and one plate, no tip racks');
select.value='2';select.handlers.change();
assert.equal(imported.properties.location,2);
assert.equal(dirty,1);
assert.match(body.textContent,/384 CellVis 1.5H.*microplate/);
""")


def test_multiple_liquid_targets_require_choice_unless_default_is_already_eligible(tmp_path):
    _run(tmp_path, """
designerState.deckConfig['4']=[{labware_id:'reservoir'}];
const ambiguous=node('liquid/Dispense');
setNewLiquidTaskLocation(ambiguous);
assert.equal(ambiguous.properties.location,'');
const validDefault=node('liquid/Aspirate',2);
setNewLiquidTaskLocation(validDefault);
assert.equal(validDefault.properties.location,2);
delete designerState.deckConfig['2'];delete designerState.deckConfig['4'];
const noPlate=node('liquid/Mix');setNewLiquidTaskLocation(noPlate);
assert.equal(noPlate.properties.location,'');
""")


def test_preflight_names_invalid_rack_but_defers_dynamic_locations_and_deck_changes(tmp_path):
    _run(tmp_path, """
const liquid=node('liquid/Dispense',1);liquid.title='Dispense sample';
graph._nodes=[start,liquid,end];
let errors=validateWorkflow();
assert.equal(errors.length,1);
assert.match(errors[0],/Dispense sample: Position 1.*384 ST10 Tip Box.*tip box/);
assert.equal(liquid.properties.location,1);
liquid.properties.location=2;assert.deepEqual(validateWorkflow(),[]);
for(const expression of ['iter:2,4','var:destination']){
  renderPropertiesPanel(liquid);
  const input=labeled('Liquid location');input.value=expression;input.handlers.change();
  assert.equal(liquid.properties.location,expression);
  assert.deepEqual(validateWorkflow(),[]);
  assert.match(body.textContent,/Resolved during the workflow/);
}
liquid.properties.location='';assert.match(validateWorkflow()[0],/Choose a liquid location/);
liquid.properties.location=1;
for(const type of ['plate/PickPlace','logic/Script','system/Manual']){
  graph._nodes=[start,liquid,end,{...node(type),id:4}];
  assert.deepEqual(validateWorkflow(),[],'Deck-changing tasks defer to native task validation');
}
""")


def test_catalog_roles_override_stale_deck_tags_and_library_defers_static_preflight(tmp_path):
    _run(tmp_path, """
designerState.deckConfig['1'][0].base_class='microplate';
designerState.deckConfig['1'][0].kind='sbs_plate';
const liquid=node('liquid/Dispense',1);
graph._nodes=[start,liquid,end];
assert.equal(liquidLocationInfo(1).eligible,false);
assert.match(validateWorkflow()[0],/384 ST10 Tip Box.*tip box/);
setNewLiquidTaskLocation(liquid);
assert.equal(liquid.properties.location,2,'Stale deck metadata cannot make a catalog tip box eligible');
designerState.labwareCatalog[0].base_class='sbs_plate';
designerState.labwareCatalog[0].kind='tip_box';
assert.equal(liquidLocationInfo(1).eligible,false,'Catalog kind also takes precedence');
liquid.properties.location=1;
tab.library='bravo.deck.add(1, replacement_plate)';
assert.deepEqual(validateWorkflow(),[],'The workflow library can change initial occupancy');
tab.library='';
assert.match(validateWorkflow()[0],/384 ST10 Tip Box.*tip box/,'Clearing the library restores static checking');
""")
