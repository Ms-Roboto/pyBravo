"""Exercise the editor's catalog metadata save boundary with Node, without HTTP."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for frontend checks")


def run_js(tmp_path, checks):
    source = (ROOT / "frontend/src/LabwareDashboard.jsx").read_text()
    functions = []
    for name in ("uniq", "supportsDeadVolume", "deadVolumePatch", "rackMetadataPatch", "tipCompatibilitySummary"):
        match = re.search(rf"function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", source)
        assert match, name
        functions.append(match[0])
    script = tmp_path / "labware-metadata.cjs"
    script.write_text("const assert = require('node:assert/strict');\n" + "\n".join(functions) + "\n" + checks)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_explicit_grid_and_catalog_links_roundtrip(tmp_path):
    run_js(tmp_path, """
const tips=[{tip_id:'st10',compatible_heads:['HT_384_D_70']},{tip_id:'st30',compatible_heads:[]}];
const entry={wells:384,tip_definition_id:'',supported_tip_ids:[]};
assert.deepEqual(rackMetadataPatch(entry,{rows:'16',cols:'24',tipId:'st10',supportedTipIds:['st10','st30']},tips,true),
    {rows:16,cols:24,tip_definition_id:'st10',supported_tip_ids:['st10','st30']});
assert.equal(tipCompatibilitySummary(tips[0]),'HT_384_D_70');
assert.equal(tipCompatibilitySummary(tips[1]),'No compatible head IDs recorded');
assert.deepEqual(entry,{wells:384,tip_definition_id:'',supported_tip_ids:[]});
""")


@pytest.mark.parametrize("rows,cols,reason", [
    ("16.5", "24", "positive whole number"),
    ("0", "24", "positive whole number"),
    ("-16", "24", "positive whole number"),
    ("Infinity", "24", "positive whole number"),
    ("16", "12", "saved well count"),
])
def test_invalid_rack_grid_is_blocked(tmp_path, rows, cols, reason):
    import json
    run_js(tmp_path, f"""
assert.throws(()=>rackMetadataPatch({{wells:384}},
    {{rows:{json.dumps(rows)},cols:{json.dumps(cols)},tipId:'',supportedTipIds:[]}},[],true),/{reason}/);
""")


def test_unknown_metadata_is_not_guessed_and_tip_links_are_validated(tmp_path):
    run_js(tmp_path, """
const entry={wells:384,well_dimensions_mm:{spacing_x_mm:4.5},name:'384 ST10 tip box'};
const values={rows:'',cols:'',tipId:'',supportedTipIds:[]};
const tips=[{tip_id:'st10'},{tip_id:'st30'}];
assert.deepEqual(rackMetadataPatch(entry,values,tips,true),{rows:null,cols:null,tip_definition_id:'',supported_tip_ids:[]});
assert.throws(()=>rackMetadataPatch(entry,{...values,tipId:'fabricated'},tips,true),/missing from the catalog/);
assert.throws(()=>rackMetadataPatch(entry,{...values,tipId:'st10',supportedTipIds:['st30']},tips,true),/primary tip definition/);
assert.throws(()=>rackMetadataPatch(entry,{...values,tipId:'st10'},[],false),/Load the tip catalog/);
const stored={...entry,tip_definition_id:'retired',supported_tip_ids:['retired']};
assert.equal(rackMetadataPatch(stored,{...values,tipId:'retired',supportedTipIds:['retired']},[],false).tip_definition_id,'retired');
assert.throws(()=>rackMetadataPatch(stored,{...values,tipId:'retired',supportedTipIds:['retired']},tips,true),/retired/);
assert.deepEqual(rackMetadataPatch(stored,values,tips,true).supported_tip_ids,[]);
""")


def test_dead_volume_requires_explicit_review_and_a_valid_value(tmp_path):
    run_js(tmp_path, """
assert.equal(supportsDeadVolume({base_class:'microplate'}),true);
assert.equal(supportsDeadVolume({base_class:'filter_plate'}),true);
assert.equal(supportsDeadVolume({base_class:'reservoir'}),true);
assert.equal(supportsDeadVolume({base_class:'tip_wash_station'}),true);
assert.equal(supportsDeadVolume({base_class:'Tip Wash Station'}),true);
assert.equal(supportsDeadVolume({base_class:'tip_box'}),false);
assert.deepEqual(deadVolumePatch('', 'placeholder'),{dead_volume_ul:null,dead_volume_status:'placeholder'});
assert.deepEqual(deadVolumePatch('12.5', 'placeholder'),{dead_volume_ul:12.5,dead_volume_status:'placeholder'});
assert.deepEqual(deadVolumePatch('0', 'reviewed'),{dead_volume_ul:0,dead_volume_status:'reviewed'});
assert.throws(()=>deadVolumePatch('', 'reviewed'),/Enter a dead volume/);
assert.throws(()=>deadVolumePatch('-1', 'placeholder'),/non-negative number/);
assert.throws(()=>deadVolumePatch('Infinity', 'reviewed'),/non-negative number/);
assert.throws(()=>deadVolumePatch('12', 'unknown'),/review status/);
""")


def test_save_handler_sends_top_level_links_and_nested_grid(tmp_path):
    source = (ROOT / "frontend/src/LabwareDashboard.jsx").read_text()
    handler = re.search(r"  const saveWellDefinition = async \(\) => \{[\s\S]*?\n  \}", source)
    assert handler
    run_js(tmp_path, """
const selectedType={labware_type_id:'rack',base_class:'microplate',wells:384,well_dimensions_mm:{custom_note:'preserve'},supported_tip_ids:[]};
const wdRows='16',wdCols='24',tipDefinitionId='st10',supportedTipIds=['st10'];
const tipDefinitions=[{tip_id:'st10'}],tipCatalogLoaded=true;
const tipSource='agilent',tipCapacityUl=10,thirdPartyTipCapacityUl='',disposableTipLengthMm='19.9';
const wdVolumeUl='',wdDeadVolumeUl='12.5',wdDeadVolumeStatus='reviewed',wdDepthMm='',wdDiameterMm='',wdOffsetX='2.25',wdOffsetY='2.25',wdPitchX='4.5',wdPitchY='4.5',wdGeometry=1,wdBottomShape=2;
const API_URL='http://mock.test';
let captured,refreshed=false,error='';
const setBusy=()=>{},setError=value=>{error=value;},fetchAll=async()=>{refreshed=true;};
const fetch=async(url,options)=>{captured={url,...options,body:JSON.parse(options.body)};return {ok:true,json:async()=>({})};};
""" + handler[0] + """
(async()=>{await saveWellDefinition();
assert.equal(error,'');assert.equal(refreshed,true);assert.equal(captured.method,'PATCH');
assert.equal(captured.body.tip_definition_id,'st10');assert.deepEqual(captured.body.supported_tip_ids,['st10']);
assert.equal(captured.body.well_dimensions_mm.rows,16);assert.equal(captured.body.well_dimensions_mm.cols,24);
assert.equal(captured.body.well_dimensions_mm.dead_volume_ul,12.5);
assert.equal(captured.body.well_dimensions_mm.dead_volume_status,'reviewed');
assert.equal(captured.body.well_dimensions_mm.custom_note,'preserve');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
