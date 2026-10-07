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


def _mapped_draft() -> dict:
    draft = _draft()
    draft["actions"] = [{
        "kind": "for_each",
        "bindings": [
            {"source_well": "A1", "target_well": "A1"},
            {"source_well": "A2", "target_well": "A2"},
        ],
        "actions": [
            {"kind": "pickup", "pipette": "small"},
            {"kind": "aspirate", "pipette": "small", "labware": "source",
             "well": "$source_well", "volume_ul": 5},
            {"kind": "dispense", "pipette": "small", "labware": "target",
             "well": "$target_well", "volume_ul": 5},
            {"kind": "drop", "pipette": "small"},
        ],
    }]
    return draft


def test_well_mapping_expands_only_explicit_model_supplied_pairs():
    code = _compile(_mapped_draft())
    ast.parse(code)
    assert code.count(".pick_up_tip()") == 2
    assert code.count(".aspirate(") == 2
    assert code.count(".dispense(") == 2
    assert code.count(".drop_tip()") == 2
    assert "lw_1.wells_by_name()['A1']" in code
    assert "lw_1.wells_by_name()['A2']" in code
    assert "lw_2.wells_by_name()['A1']" in code
    assert "lw_2.wells_by_name()['A2']" in code


def test_mapping_errors_retain_binding_index_for_model_repair():
    draft = _mapped_draft()
    draft["actions"][0]["bindings"][1]["target_well"] = "H12"
    with pytest.raises(ActionPlanError, match=r"actions\[0\]\.bindings\[1\]\.actions\[2\]"):
        _compile(draft)
    draft = _mapped_draft()
    draft["actions"][0]["actions"][1]["well"] = "$missing"
    with pytest.raises(ActionPlanError, match="unknown well binding"):
        _compile(draft)
    draft = _mapped_draft()
    draft["actions"][0]["bindings"][1].pop("target_well")
    with pytest.raises(ActionPlanError, match="must supply the same"):
        _compile(draft)


def test_mapping_rejects_unused_names_and_excessive_expansion():
    draft = _mapped_draft()
    draft["actions"][0]["actions"][2]["well"] = "A1"
    with pytest.raises(ActionPlanError, match="unused well bindings"):
        _compile(draft)
    draft = _mapped_draft()
    loop = draft["actions"][0]
    loop["bindings"] = [{"source_well": "A1", "target_well": "A1"}] * 384
    loop["actions"].extend({"kind": "delay", "seconds": 1} for _ in range(28))
    with pytest.raises(ActionPlanError, match="expansion limit"):
        _compile(draft)


def test_eight_channel_loop_accepts_only_trusted_full_column_anchors():
    draft = _mapped_draft()
    facts = {
        "synthetic_96_tiprack_20ul": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            is_tiprack=True, tip_capacity_ul=20, multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
        "synthetic_source": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
        "synthetic_target": LabwareFacts(
            frozenset(f"{row}{col}" for row in "ABCDEFGH" for col in (1, 2)),
            multichannel_compatible=True,
            multichannel_anchor_wells=frozenset({"A1", "A2"}),
        ),
    }
    multi = {"synthetic_p20": PipetteFacts(1, 20, 8)}
    code = _compile(draft, labware=facts, pipettes=multi)
    assert code.count(".pick_up_tip()") == 2
    draft["actions"][0]["bindings"][1]["target_well"] = "B2"
    with pytest.raises(ActionPlanError, match="invalid multichannel column anchor"):
        _compile(draft, labware=facts, pipettes=multi)
