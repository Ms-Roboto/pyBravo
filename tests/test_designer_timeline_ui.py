"""Designer replay preserves the simulator's initial pose and native tip exchanges."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _run(tmp_path: Path, body: str) -> None:
    html = (ROOT / "frontend" / "designer.html").read_text()
    function_names = (
        "timelineReset", "timelineStartRecording", "snapshotDeckConfig",
        "restoreDeckConfig", "timelineRecord", "timelineStopRecording",
        "updateScrubberLabels", "formatTime", "scrubTo",
        "resetAnimationTransients", "handleWorkflowEvent",
    )
    functions = []
    for name in function_names:
        match = re.search(rf"function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match.group(0))
    timeline_match = re.search(r"const timeline = \{[\s\S]*?\n\};", html)
    assert timeline_match

    # Use the real scene state methods; only WebGL drawing and telemetry are
    # replaced. This exercises snake/camel case event and snapshot boundaries.
    scene_source = (ROOT / "frontend" / "src" / "robot-scene.js").read_text()
    methods = []
    for name in (
        "setPositions", "snapRenderPositions", "setMotionTargets", "setTaskStatus",
        "getHeadTipState", "getTipboxTransientState", "setTipboxTransientState",
        "setHeadTipState",
    ):
        match = re.search(rf"    (?:async )?{name}\([^\n]*\) \{{[\s\S]*?\n    \}}", scene_source)
        assert match, name
        methods.append(match.group(0))

    script = tmp_path / "designer-timeline.cjs"
    script.write_text("""
const assert = require('node:assert/strict');
const copy = value => JSON.parse(JSON.stringify(value));
const elements = new Map();
const document = {
    getElementById(id) {
        if (!elements.has(id)) elements.set(id, {style:{}, textContent:'', value:'0'});
        return elements.get(id);
    },
    querySelector(selector) { return this.getElementById(selector); },
};
function renderVarsPanel() {}
function refreshVarHints() {}
function highlightNode() {}
function updateDeckCell() {}
function syncDeckTo3D() {}
function isCompiledProtocolPreviewTab() { return false; }
function forceHideTaskPromptModal() {}
function hideScriptErrorModal() {}
function hideUserPromptModal() {}
class SceneState {
    constructor() {
        this.positions = {X:320,Y:180,Z:100,W:0,G:90,Zg:120};
        this.renderPositions = {...this.positions};
        this.motionTargets = {...this.positions};
        this.headType = 'HT_384_D_70';
        this.headMode = {subset_type:'all_barrels',subset_config:'back_left'};
        this.tipSelection = null;
        this.tipsOnHead = false;
        this.tipsOnHeadMode = null;
        this.tipsOnHeadSelection = null;
        this.tipLabwareName = '';
        this.tipDefinitionId = '';
        this.attachedTipLengthMm = null;
        this.activeTipCapacityUl = null;
        this.tipboxTransientState = {};
        this._headTipStateSignature = '';
        this._tipboxTransientSignature = '';
        this.taskStatus = {};
        this.simulationMode = false;
    }
    setSimulationMode(enabled) { this.simulationMode = !!enabled; }
    async _renderHeadTipsFromState() {}
    async refreshDeckLabwareScene() {}
""" + "\n".join(methods) + """
}
const scene = new SceneState();
const designerState = {
    robotScene:scene, vars:{previous:true},
    deckConfig:{'2':[{labware_id:'source-rack'}], '4':[{labware_id:'return-rack'}]},
};
const initialPose = {X:0,Y:0,Z:0,W:0,G:0,Zg:-20};
const initialRuntime = {
    head_type:'HT_384_D_70',
    head_mode:{subset_type:'single_barrel',subset_config:'back_left'},
    tip_selection:{location:2,row:15,col:0},
    tips_on_head:false, tips_on_head_mode:null, tips_on_head_selection:null,
    tip_labware_name:'', tip_definition_id:'', attached_tip_length_mm:null,
    active_tip_capacity_ul:10,
    tipbox_removed_cells:{'2':[], '4':['15:0','15:1']},
};
""" + timeline_match.group(0) + "\n" + "\n".join(functions) + "\n" + body)
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("initial_zg", [-20, -13.25])
def test_start_and_scrub_restore_authoritative_initial_pose_and_state(tmp_path, initial_zg):
    _run(tmp_path, f"initialPose.Zg = {initial_zg};\n" + """
// The previous replay ended with an extended gripper, attached tips, and a
// carried plate. None of that belongs to the next simulation's first frame.
scene.setHeadTipState({tips_on_head:true, tipbox_removed_cells:{'2':['15:0']}});
timeline._pickPlaceStatus = {task:'pick_place', step:'grip_plate', from_location:2};
timeline._carriedPlate = {from:2};
timeline.frames = [{positions:{...scene.positions}, stepName:'old run'}];
timeline.scrubbing = true;
handleWorkflowEvent({type:'workflow:start', positions:initialPose, runtime_state:initialRuntime});

assert.equal(timeline.frames.length,1);
assert.equal(timeline.scrubbing,false);
assert.equal(scene.simulationMode,true);
assert.deepEqual(scene.positions,initialPose);
assert.deepEqual(scene.renderPositions,initialPose,'the initial pose must snap before native motion');
const first = copy(timeline.frames[0]);
assert.deepEqual(first.positions,initialPose,'frame zero must own a complete initial pose');
assert.equal(first.headTipState.tipsOnHead,false);
assert.deepEqual(first.tipboxTransientState,initialRuntime.tipbox_removed_cells);
assert.equal(first.taskStatus,null,'old carry state must be cleared before recording');
assert.equal(timeline._carriedPlate,null);

// Move well away from home, then return to frame zero without relying on a
// preceding motion frame or live hardware positions.
handleWorkflowEvent({type:'workflow:positions',positions:{X:80,Y:40,Z:95,W:0,G:86,Zg:70}});
scene.setHeadTipState({tips_on_head:true,tipbox_removed_cells:{'2':['15:0'],'4':[]}});
scrubTo(0);
assert.deepEqual(scene.positions,initialPose);
assert.deepEqual(scene.renderPositions,initialPose);
assert.deepEqual(scene.getHeadTipState(),first.headTipState);
assert.deepEqual(scene.getTipboxTransientState(),first.tipboxTransientState);
assert.deepEqual(timeline.frames[0],first,'later scene changes must not mutate the initial snapshot');
""")


def test_tip_exchange_frames_restore_attachment_and_returned_rack_inventory(tmp_path):
    _run(tmp_path, """
handleWorkflowEvent({type:'workflow:start',positions:initialPose,runtime_state:initialRuntime});
handleWorkflowEvent({type:'workflow:node_start',node_id:3,task_name:'Tips On'});
const pickupPose = {...initialPose,X:80,Y:40,Z:105};
handleWorkflowEvent({type:'workflow:positions',positions:pickupPose});
handleWorkflowEvent({type:'workflow:tips_change',tips_on:true,tips_on_head:true,
    tips_on_head_mode:initialRuntime.head_mode,
    tips_on_head_selection:{location:2,row:15,col:0},
    tip_definition_id:'st_10ul',attached_tip_length_mm:19.9,active_tip_capacity_ul:10,
    tipbox_removed_cells:{'2':['15:0'],'4':['15:0','15:1']}});
const attachedIndex = timeline.frames.length - 1;
assert.equal(scene.tipsOnHead,true,'native pickup events must immediately attach tips');
assert.deepEqual(scene.getTipboxTransientState()['2'],['15:0']);
assert.equal(timeline.frames[attachedIndex].headTipState.tipsOnHead,true);

handleWorkflowEvent({type:'workflow:positions',positions:{...pickupPose,Z:0}});
handleWorkflowEvent({type:'workflow:node_start',node_id:4,task_name:'Tips Off'});
const returnPose = {...initialPose,X:150,Y:70,Z:106};
handleWorkflowEvent({type:'workflow:positions',positions:returnPose});
handleWorkflowEvent({type:'workflow:tips_change',tips_on:false,tips_on_head:false,
    tips_on_head_mode:null,tips_on_head_selection:null,
    tipbox_removed_cells:{'2':['15:0'],'4':[]}});
const returnedIndex = timeline.frames.length - 1;
assert.equal(scene.tipsOnHead,false,'native eject events must immediately detach tips');
assert.deepEqual(scene.getTipboxTransientState()['4'],[],
    'an explicit empty removed-cell list must show returned tips in a rack that started empty');
handleWorkflowEvent({type:'workflow:positions',positions:{...returnPose,Z:0}});
timelineStopRecording();

for (const index of [attachedIndex,returnedIndex,attachedIndex,0,returnedIndex]) {
    scrubTo(index);
    const expected = timeline.frames[index];
    assert.deepEqual(scene.getHeadTipState(),expected.headTipState);
    assert.deepEqual(scene.getTipboxTransientState(),expected.tipboxTransientState);
    assert.deepEqual(scene.positions,index===attachedIndex ? pickupPose : index===0 ? initialPose : returnPose);
    assert.deepEqual(scene.renderPositions,scene.positions);
}
assert.equal(scene.tipsOnHead,false);
assert.deepEqual(scene.getTipboxTransientState()['4'],[]);
""")
