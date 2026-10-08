"""Designer must distinguish collision evidence from task completion or animation."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _run(tmp_path, body):
    html = (ROOT / "frontend" / "designer.html").read_text()
    names = ("hasCurrentPhysicalSimulationEvidence", "physicalSimulationSummary", "renderPhysicalSimulationReport", "renderSimulationBlockers", "workflowCompletionMessage")
    functions = []
    for name in names:
        match = re.search(rf"function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    script = tmp_path / "physical-report.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
const elements=new Map();
const document={getElementById(id){
    if(!elements.has(id))elements.set(id,{hidden:true,open:false,textContent:''});
    return elements.get(id);
}};
const designerState={graph:{getNodeById(id){return id===7?{title:'Move across stack'}:null;}}};
function updatePhysicalObstructionPreview(){}
function resetPhysicalObstructionPreview(){}
""" + "\n".join(functions) + body)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_collision_success_requires_sampled_native_motion(tmp_path):
    _run(tmp_path, """
const success={contract_version:'pybravo.superdex-rehearsal.v1',engine:'SuperDex',engine_version:'1.0.0',status:'checked',moves_checked:12,samples_checked:1024,contact_queries:0};
assert.match(workflowCompletionMessage({status:'ok',physical_simulation:success}),/modeled collision checks passed/);
for(const report of [undefined,{...success,contract_version:undefined},{...success,engine:'Other'}, {...success,engine_version:undefined},{...success,status:'not_checked'}, {...success,status:'failed'},
    {...success,moves_checked:0,samples_checked:0}, {...success,moves_checked:1,samples_checked:0},
    {...success,last_error:{message:'Catalog geometry is missing'}}]) {
    assert.doesNotMatch(workflowCompletionMessage({status:'ok',physical_simulation:report}),/checks passed/);
}
for(const field of ['moves_checked','samples_checked','contact_queries']) {
    for(const value of [undefined,null,true,false,'12',-1,0.5,NaN,Infinity,-Infinity]) {
        const malformed={...success,[field]:value};
        assert.equal(hasCurrentPhysicalSimulationEvidence(malformed),false,`${field}: ${value}`);
        assert.doesNotMatch(workflowCompletionMessage({status:'ok',physical_simulation:malformed}),/checks passed/);
        assert.doesNotMatch(physicalSimulationSummary(malformed,'workflow:complete'),/checks passed/);
    }
}
for(const engine_version of ['', '  ', true, 1]) {
    assert.equal(hasCurrentPhysicalSimulationEvidence({...success,engine_version}),false);
}
assert.equal(hasCurrentPhysicalSimulationEvidence({...success,moves_checked:1,samples_checked:0}),false);
assert.equal(hasCurrentPhysicalSimulationEvidence({...success,moves_checked:0,samples_checked:0}),true);
assert.match(workflowCompletionMessage({status:'aborted',physical_simulation:success}),/checks incomplete/);
assert.match(workflowCompletionMessage({status:'ok',simulation_kind:'visual_walkthrough',physical_simulation:success}),/collision checks were not run/);
assert.match(physicalSimulationSummary({...success,moves_checked:0,samples_checked:0},'workflow:start'),/awaiting native motion/);
assert.match(workflowCompletionMessage({status:'ok'},{hardware:true}),/Hardware execution complete/);
""")


def test_report_shows_failed_task_bodies_and_rejected_pose(tmp_path):
    _run(tmp_path, """
renderPhysicalSimulationReport({engine:'SuperDex',engine_version:'1.0.0',status:'failed',
    scope:'catalog_envelopes',head_geometry:'URDF_collision_mesh',moves_checked:3,samples_checked:95,
    last_error:{node_id:7,node_type:'plate/PickPlace',message:'Physical collision with stack',
        bodies:['robot/head','labware/2/3/Stack plate 4'],pose:{X:76.768,Y:9.98,Z:115}},
    limitations:['Sampled commanded geometry only.']},'workflow:error');
assert.equal(elements.get('physical-report').hidden,false);
assert.equal(elements.get('physical-report').open,true);
assert.match(elements.get('physical-report-summary').textContent,/stopped/);
const text=elements.get('physical-report-body').textContent;
for(const expected of ['Move across stack (node 7)','robot/head ↔ labware/2/3/Stack plate 4',
    'Rejected pose (mm): X 76.77 · Y 9.98 · Z 115.00','95 sampled poses','Sampled commanded geometry only.']) {
    assert.ok(text.includes(expected),expected);
}
assert.doesNotMatch(text,/checks passed/);
""")


def test_missing_geometry_is_visible_without_fake_collision_result(tmp_path):
    _run(tmp_path, """
renderSimulationBlockers('Workflow cannot run',[
    {node_id:4,node_title:'Tips Off',reason:'Return rack lacks a well-hole diameter'},
    {node_id:5,node_title:'Pick tips',reason:'P24 requires Y293.22 beyond Y231'},
]);
assert.equal(elements.get('physical-report').hidden,false);
assert.equal(elements.get('physical-report').open,true);
assert.equal(elements.get('physical-report-summary').textContent,'Simulation blocked before motion');
assert.match(elements.get('physical-report-body').textContent,/Tips Off: Return rack lacks/);
assert.match(elements.get('physical-report-body').textContent,/Pick tips: P24/);
assert.doesNotMatch(elements.get('physical-report-body').textContent,/checks passed/);
""")
