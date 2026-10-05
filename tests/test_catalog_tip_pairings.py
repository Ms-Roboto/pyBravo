"""Checked-in rack links keep fixture geometry separate from consumable data."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pybravo.deck.labware import LabwareDefinition, _read_labware_snapshot
from pybravo.workflow.protocols.tipbox_choices import compatible_tipbox_choices

CONFIG = Path(__file__).resolve().parents[1] / "config"
ST_RACK = "lw-4914769d0af7"
LT200_RACK = "lw-b0704e550d2a"
LT250_RACKS = {"lw-cadcb0f0f9c1", "lw-0c182cad7c78", "lw-006d2ad894c2"}
LT250_96LT_RACKS = {"lw-cadcb0f0f9c1", "lw-0c182cad7c78"}


def _catalog_rows(source: str) -> dict[str, LabwareDefinition]:
    if source == "snapshot":
        rows = _read_labware_snapshot(CONFIG / "labware_catalog.snapshot.yaml")
    else:
        if source == "editor":
            documents = yaml.safe_load((CONFIG / "labware_editor.yaml").read_text())["labware_types"]
        else:
            documents = json.loads((CONFIG / "labware_types.seed.json").read_text())
        rows = [LabwareDefinition.from_mongo(document) for document in documents]
    return {row.id: row for row in rows}


@pytest.mark.parametrize("source", ["snapshot", "editor", "seed"])
def test_st_rack_retains_independent_scientist_confirmed_tip_options(source: str) -> None:
    rack = _catalog_rows(source)[ST_RACK]
    assert rack.tip_definition_id == "st_10ul"
    assert rack.supported_tip_ids == ["st_10ul", "st_70ul"]
    assert (rack.rows, rack.cols, rack.spacing_x_mm, rack.spacing_y_mm) == (16, 24, 4.5, 4.5)
    # The imported rack's original advertised/default capacity is not a limit
    # on the independently selected consumable.
    assert rack.disposable_tip_capacity_ul == 10.0


@pytest.mark.parametrize("source", ["snapshot", "editor", "seed"])
def test_lt200_link_stays_distinct_from_lt250_variants(source: str) -> None:
    rack = _catalog_rows(source)[LT200_RACK]
    assert rack.tip_definition_id == "lt_200ul"
    assert rack.supported_tip_ids == ["lt_200ul"]
    assert (rack.rows, rack.cols, rack.spacing_x_mm, rack.spacing_y_mm) == (8, 12, 9.0, 9.0)
    assert rack.disposable_tip_capacity_ul == 200.0


def test_lt250_vendor_rows_link_to_their_documented_tip_length() -> None:
    rows = {row.id: row for row in _read_labware_snapshot(CONFIG / "labware_catalog.d" / "96am.yaml")}
    tips = {row["tip_id"]: row for row in yaml.safe_load((CONFIG / "tips.yaml").read_text())["tips"]}
    for rack_id in LT250_RACKS:
        rack = rows[rack_id]
        assert "tip length 55.5 mm" in rack.description
        assert rack.tip_definition_id == "am_lt250_teach"
        expected = ["am_lt250_teach", "lt_250ul"] if rack_id in LT250_96LT_RACKS else ["am_lt250_teach"]
        assert rack.supported_tip_ids == expected
        assert (rack.rows, rack.cols, rack.spacing_x_mm, rack.spacing_y_mm) == (8, 12, 9.0, 9.0)
        assert tips[rack.tip_definition_id]["length_mm"] == 55.5
    # The 96LT and AssayMAP records retain their distinct tip length settings.
    assert tips["lt_250ul"]["length_mm"] == 55.2


def test_linked_consumables_preserve_measured_or_unknown_lengths() -> None:
    tips = {row["tip_id"]: row for row in yaml.safe_load((CONFIG / "tips.yaml").read_text())["tips"]}
    assert tips["st_10ul"]["length_mm"] == 19.9
    assert tips["st_70ul"]["length_mm"] is None
    assert tips["lt_200ul"]["length_mm"] is None
    assert tips["st_70ul"]["capacity_ul"] == 70.0
    assert set(tips["st_10ul"]["compatible_heads"]) == set(tips["st_70ul"]["compatible_heads"])


@pytest.mark.parametrize("head", ["HT_384_D_70", "HT_96_D_70", "HT_16_D_ST"])
def test_checked_in_st_box_offers_both_independent_tips(head: str) -> None:
    racks = [row.to_summary() for row in _read_labware_snapshot(CONFIG / "labware_catalog.snapshot.yaml")]
    tips = yaml.safe_load((CONFIG / "tips.yaml").read_text())["tips"]
    choices = compatible_tipbox_choices(head, racks, tips)
    linked = {row["tip_definition_id"]: row for row in choices if row["labware_id"] == ST_RACK}
    assert set(linked) == {"st_10ul", "st_70ul"}
    assert linked["st_10ul"]["execution_ready"] is True
    assert linked["st_70ul"]["execution_ready"] is False


@pytest.mark.parametrize("head", ["HT_96_D_200", "HT_8_D_LT"])
def test_checked_in_lt_boxes_offer_200_and_250_choices(head: str) -> None:
    definitions = _read_labware_snapshot(CONFIG / "labware_catalog.snapshot.yaml")
    definitions += _read_labware_snapshot(CONFIG / "labware_catalog.d" / "96am.yaml")
    tips = yaml.safe_load((CONFIG / "tips.yaml").read_text())["tips"]
    choices = compatible_tipbox_choices(head, [row.to_summary() for row in definitions], tips)
    linked = {(row["labware_id"], row["tip_definition_id"]): row for row in choices}
    assert linked[(LT200_RACK, "lt_200ul")]["execution_ready"] is False
    for rack_id in LT250_96LT_RACKS:
        assert linked[(rack_id, "lt_250ul")]["execution_ready"] is True
