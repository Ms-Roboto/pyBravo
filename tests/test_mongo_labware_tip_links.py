"""Mongo catalog reads and snapshot refreshes preserve explicit tip links."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from pybravo.deck import labware


@pytest.fixture
def mongo_catalog(monkeypatch, tmp_path):
    document = {
        "labware_type_id": "synthetic-tipbox", "name": "Synthetic tipbox", "kind": "tip_box",
        "base_class": "tip_box", "wells": 384,
        "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 50.0},
        "well_dimensions_mm": {"rows": 16, "cols": 24, "spacing_x_mm": 4.5, "spacing_y_mm": 4.5},
        "tip_definition_id": "st_10ul", "supported_tip_ids": ["st_10ul", "st_30ul"],
    }

    class Collection:
        def find(self, query, projection):
            # Apply the actual inclusion projection. A fake that returned every
            # document field would hide the original missing-projection bug.
            return [{key: deepcopy(value) for key, value in document.items() if projection.get(key) == 1}]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __getitem__(self, database):
            return {"types": Collection()}

    monkeypatch.setattr(labware, "MongoClient", Client)
    monkeypatch.setattr(labware, "_load_labware_catalog_config", lambda: ("mongodb://synthetic", "labdb", "types"))
    snapshot = tmp_path / "catalog.snapshot.yaml"
    monkeypatch.setattr(labware, "_load_labware_snapshot_path", lambda: snapshot)
    monkeypatch.setattr(labware, "_read_labware_overlays", lambda: [])
    return document, snapshot


def test_mongo_inclusion_projection_preserves_tip_links(mongo_catalog):
    document, _ = mongo_catalog
    catalog = labware.MongoLabwareCatalog("mongodb://synthetic", "labdb", "types")
    definition = catalog.get_definition(document["labware_type_id"])
    assert definition.tip_definition_id == "st_10ul"
    assert definition.supported_tip_ids == ["st_10ul", "st_30ul"]


def test_mongo_inclusion_projection_preserves_provisional_head_restrictions(mongo_catalog):
    document, _ = mongo_catalog
    document["provisional"] = True
    document["compatible_head_types"] = ["HT_96_D_70", "HT_96_D_70_S2"]

    catalog = labware.MongoLabwareCatalog("mongodb://synthetic", "labdb", "types")
    definition = catalog.get_definition(document["labware_type_id"])

    assert definition.provisional is True
    assert definition.compatible_head_types == ["HT_96_D_70", "HT_96_D_70_S2"]
    assert definition.to_summary()["provisional"] is True


def test_mongo_snapshot_refresh_and_offline_reload_preserve_tip_links(mongo_catalog, monkeypatch):
    document, snapshot = mongo_catalog
    snapshot.write_text("version: 1\nsource: old\nlabware: []\n", encoding="utf-8")
    catalog = labware.build_labware_catalog()
    assert catalog.get_definition(document["labware_type_id"]).tip_definition_id == "st_10ul"
    saved = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    assert saved["source"] == "mongo"
    assert saved["labware"][0]["tip_definition_id"] == "st_10ul"
    assert saved["labware"][0]["supported_tip_ids"] == ["st_10ul", "st_30ul"]

    monkeypatch.setattr(labware, "_load_labware_catalog_config", lambda: ("", "", ""))
    offline = labware.build_labware_catalog().get_definition(document["labware_type_id"])
    assert offline.tip_definition_id == "st_10ul"
    assert offline.supported_tip_ids == ["st_10ul", "st_30ul"]
