"""A model-authored action sequence must pass catalog and state checks."""

from __future__ import annotations

import ast
from copy import deepcopy

import pytest

from pybravo.evals.text2wetlab.action_ir import (
    ActionPlan,
    ActionPlanError,
    LabwareFacts,
    PipetteFacts,
    compile_actions,
)

LABWARE = {
    "synthetic_96_tiprack_20ul": LabwareFacts(
        frozenset({"A1", "B1"}), is_tiprack=True, tip_capacity_ul=20,
    ),
    "synthetic_source": LabwareFacts(frozenset({"A1", "A2"})),
    "synthetic_target": LabwareFacts(frozenset({"A1", "A2"})),
}
PIPETTES = {"synthetic_p20": PipetteFacts(1, 20, 1)}


def _draft() -> dict:
    return {
        "labware": [
            {"id": "tips", "load_name": "synthetic_96_tiprack_20ul", "slot": 1},
            {"id": "source", "load_name": "synthetic_source", "slot": 2},
            {"id": "target", "load_name": "synthetic_target", "slot": 3},
        ],
        "pipettes": [{
            "id": "small", "model": "synthetic_p20", "mount": "left",
            "tip_rack_ids": ["tips"],
        }],
        "actions": [
            {"kind": "pickup", "pipette": "small"},
            {"kind": "aspirate", "pipette": "small", "labware": "source",
             "well": "A1", "volume_ul": 5},
            {"kind": "dispense", "pipette": "small", "labware": "target",
             "well": "A1", "volume_ul": 5},
            {"kind": "drop", "pipette": "small"},
        ],
    }


def _compile(draft: dict, *, labware=LABWARE, pipettes=PIPETTES) -> str:
    return compile_actions(draft, labware_catalog=labware, pipette_catalog=pipettes)


def test_explicit_actions_lower_to_fixed_primitives_without_inferred_steps():
    draft = _draft()
    code = _compile(draft)
    ast.parse(code)
    assert code.count(".pick_up_tip()") == 1
    assert code.count(".aspirate(") == 1
    assert code.count(".dispense(") == 1
    assert code.count(".drop_tip()") == 1
    assert code.index(".aspirate(") < code.index(".dispense(")
    assert ActionPlan.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize("volume,diagnostic", [
    (0.5, "outside"),
    (21, "outside"),
])
def test_rejects_strokes_outside_trusted_pipette_and_tip_range(volume, diagnostic):
    draft = _draft()
    draft["actions"][1]["volume_ul"] = volume
    with pytest.raises(ActionPlanError, match=diagnostic):
        _compile(draft)


def test_tracks_tip_contents_across_distinct_aspirate_and_dispense_actions():
    draft = _draft()
    draft["actions"][2]["volume_ul"] = 6
    with pytest.raises(ActionPlanError, match="dispenses more"):
        _compile(draft)
    draft = _draft()
    draft["actions"].insert(2, deepcopy(draft["actions"][1]))
    draft["actions"][2]["volume_ul"] = 16
    with pytest.raises(ActionPlanError, match="tip capacity"):
        _compile(draft)


def test_requires_tip_and_checks_tip_supply_before_compilation():
    draft = _draft()
    draft["actions"].pop(0)
    with pytest.raises(ActionPlanError, match="without an attached tip"):
        _compile(draft)
    draft = _draft()
    draft["actions"].extend(deepcopy(draft["actions"]))
    draft["actions"].extend(deepcopy(draft["actions"][:4]))
    with pytest.raises(ActionPlanError, match="fresh-tip supply"):
        _compile(draft)


def test_rejects_unloaded_well_and_unknown_catalog_entries():
    draft = _draft()
    draft["actions"][1]["well"] = "H12"
    with pytest.raises(ActionPlanError, match="invalid liquid well"):
        _compile(draft)
    draft = _draft()
    draft["labware"][1]["load_name"] = "invented_source"
    with pytest.raises(ActionPlanError, match="absent from the trusted catalog"):
        _compile(draft)


def test_rejects_multichannel_geometry_and_shared_tiprack():
    draft = _draft()
    multi = {"synthetic_p20": PipetteFacts(1, 20, 8)}
    with pytest.raises(ActionPlanError, match="multichannel compatible"):
        _compile(draft, pipettes=multi)
    draft = _draft()
    draft["pipettes"].append({
        "id": "other", "model": "synthetic_p20", "mount": "right",
        "tip_rack_ids": ["tips"],
    })
    with pytest.raises(ActionPlanError, match="multiple pipettes"):
        _compile(draft)


def test_rejects_model_supplied_extra_code_or_motion_fields():
    draft = _draft()
    draft["actions"][1]["python"] = "import os"
    with pytest.raises(ActionPlanError, match="Extra inputs are not permitted"):
        _compile(draft)
