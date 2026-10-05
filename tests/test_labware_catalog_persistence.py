"""Catalog editing and reimport must retain recorded tipbox metadata."""

import pytest
import yaml

from pybravo import labware_editor
from pybravo.deck.labware import LabwareDefinition
from scripts.import_labware_from_registry import import_labware_reg


@pytest.mark.parametrize(
    "geometry",
    [
        {"rows": 16, "cols": 24, "spacing_x_mm": 4.5, "spacing_y_mm": 4.5,
         "offset_x_mm": 11.25, "offset_y_mm": 12.75, "well_depth_mm": 20.5},
        {"rows": 0, "cols": 0, "spacing_x_mm": 0, "spacing_y_mm": 0,
         "offset_x_mm": 0, "offset_y_mm": 0, "well_depth_mm": 0},
    ],
    ids=["recorded_geometry", "unknown_geometry"],
)
def test_editor_seed_patch_and_reload_preserve_tipbox_metadata(monkeypatch, tmp_path, geometry):
    editor_path = tmp_path / "editor.yaml"
    snapshot_path = tmp_path / "snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_editor, "_mongo_enabled", lambda: False)
    definition = LabwareDefinition(
        id="synthetic-tipbox", name="Synthetic rack", kind="tip_box", wells=384,
        tip_definition_id="synthetic-tip", supported_tip_ids=["synthetic-tip", "second-tip"],
        **geometry,
    )
    snapshot_path.write_text(yaml.safe_dump({"labware": [definition.to_summary()]}))

    # The initial editor seed and subsequent unrelated edit both rewrite the snapshot.
    labware_editor.load_store()
    labware_editor.patch_type(definition.id, {"description": "Reviewed label"})
    reloaded = labware_editor.get_type(definition.id)
    saved = yaml.safe_load(snapshot_path.read_text())["labware"][0]

    assert saved["description"] == "Reviewed label"
    assert {key: saved[key] for key in geometry} == geometry
    assert saved["tip_definition_id"] == definition.tip_definition_id
    assert saved["supported_tip_ids"] == definition.supported_tip_ids
    assert reloaded["tip_definition_id"] == definition.tip_definition_id
    assert reloaded["supported_tip_ids"] == definition.supported_tip_ids
    assert reloaded["well_dimensions_mm"]["rows"] == geometry["rows"]
    assert reloaded["well_dimensions_mm"]["cols"] == geometry["cols"]


@pytest.mark.parametrize(
    "links",
    [
        {"tip_definition_id": "curated-tip", "supported_tip_ids": ["curated-tip", "other-tip"]},
        {"tip_definition_id": "curated-tip", "supported_tip_ids": []},
        {"tip_definition_id": "", "supported_tip_ids": ["curated-tip"]},
    ],
    ids=["both_links", "primary_only", "supported_only"],
)
def test_registry_reimport_keeps_curated_tip_links_while_updating_source_fields(tmp_path, links):
    registry_path = _write_registry(tmp_path)
    output_path = tmp_path / "snapshot.yaml"
    old = {"name": "Synthetic rack", "height_mm": 1, **links}
    output_path.write_text(yaml.safe_dump({"labware": [old]}))

    items, new_count, updated_count = import_labware_reg(registry_path, output_path=output_path)

    assert (new_count, updated_count) == (0, 1)
    assert {key: items[0][key] for key in links} == links
    assert items[0]["height_mm"] == 25.0
    assert items[0]["spacing_x_mm"] == 4.5
    assert yaml.safe_load(output_path.read_text())["labware"] == items


def test_registry_replace_does_not_copy_links_from_discarded_catalog(tmp_path):
    registry_path = _write_registry(tmp_path)
    output_path = tmp_path / "snapshot.yaml"
    output_path.write_text(yaml.safe_dump({"labware": [{
        "name": "Synthetic rack", "tip_definition_id": "old-tip", "supported_tip_ids": ["old-tip"],
    }]}))

    items, new_count, updated_count = import_labware_reg(registry_path, output_path=output_path, merge=False)

    assert (new_count, updated_count) == (1, 0)
    assert items[0]["tip_definition_id"] == ""
    assert items[0]["supported_tip_ids"] == []


def _write_registry(tmp_path):
    path = tmp_path / "synthetic.reg"
    path.write_text(
        r"[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Velocity11\shared\Labware\Labware_Entries\Synthetic rack]"
        '\n"NAME"="Synthetic rack"\n"BASE_CLASS"="6"\n"NUMBER_OF_WELLS"="384"'
        '\n"THICKNESS"="25"\n"X_WELL_TO_WELL"="4.5"\n"Y_WELL_TO_WELL"="4.5"\n'
    )
    return path
