"""Manufacturer dimensions must survive catalog, task, render, and check paths."""

import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from pybravo import labware_editor
from pybravo.deck.labware import Labware, LabwareDefinition, synthesize_lid_labware
from pybravo.deck.lid_geometry import lid_envelope_geometry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cellvis():
    catalog = yaml.safe_load((ROOT / "config/labware_catalog.snapshot.yaml").read_text())
    return LabwareDefinition(**next(row for row in catalog["labware"] if row["id"] == "lw-34358f93e2a0"))


def test_cellvis_drawing_dimensions_and_provenance(cellvis):
    geometry = lid_envelope_geometry(cellvis.to_summary())
    assert geometry is not None
    assert geometry["lower_local_mm"] == pytest.approx([-63.575, -42.525, 6.8])
    assert geometry["upper_local_mm"] == pytest.approx([63.575, 42.525, 16.8])
    assert cellvis.lidded_height_mm == 16.8
    assert cellvis.lid_resting_height_mm == 6.8
    source = ROOT / geometry["source_document"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == geometry["source_sha256"]
    assert "not dimensioned" in " ".join(geometry["limitations"])


def test_lid_geometry_survives_editor_and_mongo_roundtrips(cellvis):
    editor = yaml.safe_load((ROOT / "config/labware_editor.yaml").read_text())
    row = next(row for row in editor["labware_types"] if row["labware_type_id"] == cellvis.id)
    for definition in (
        LabwareDefinition.from_mongo(row),
        labware_editor._editor_type_to_definition(row),
        labware_editor._editor_type_to_definition(labware_editor._definition_to_editor_type(cellvis)),
    ):
        assert definition.lid_geometry == cellvis.lid_geometry
        assert lid_envelope_geometry(definition.to_summary()) is not None


def test_lidded_plate_and_standalone_lid_share_envelope(cellvis):
    plate = Labware.from_definition(cellvis, is_lidded=True)
    lid = synthesize_lid_labware(plate)
    assert plate.height == pytest.approx(16.8)
    assert (lid.length, lid.width, lid.height) == pytest.approx((127.15, 85.05, 10.0))
    assert lid.metadata == plate.metadata["generated_lid"]
    assert lid.metadata["lid_geometry"]["model"] == "manufacturer_exterior_envelope"
    assert "lid_geometry" not in Labware.from_definition(cellvis).metadata.get("generated_lid", {})


@pytest.mark.parametrize("key,value", [
    ("height_mm", None), ("length_mm", -1), ("width_mm", 0),
    ("seated_bottom_mm", -1), ("height_mm", float("nan")),
    ("height_mm", float("inf")), ("width_mm", True),
    ("model", "illustrative"), ("source", ""),
])
def test_incomplete_envelopes_do_not_enable_collision_support(cellvis, key, value):
    metadata = cellvis.to_summary()
    metadata["lid_geometry"][key] = value
    assert lid_envelope_geometry(metadata) is None


def test_inconsistent_seated_height_stays_unsupported_and_metadata_is_immutable(cellvis):
    metadata = cellvis.to_summary()
    before = deepcopy(metadata)
    derived = lid_envelope_geometry(metadata)
    derived["limitations"].append("changed caller result")
    assert metadata == before
    metadata["lidded_height_mm"] = 16.4
    assert lid_envelope_geometry(metadata) is None
    assert lid_envelope_geometry({}) is None
    assert lid_envelope_geometry("malformed") is None


@pytest.mark.parametrize("key,value", [
    ("height_mm", 6.0), ("height_mm", 20.0),
    ("height_mm", float("nan")), ("height_mm", True),
    ("lidded_height_mm", True), ("lidded_height_mm", float("inf")),
    ("lidded_height_mm", 10**400),
])
def test_invalid_plate_relation_keeps_envelope_unsupported(cellvis, key, value):
    metadata = cellvis.to_summary()
    metadata[key] = value
    assert lid_envelope_geometry(metadata) is None


def test_three_lid_bounds_equal_collision_envelope(cellvis, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to check the actual Three mesh")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    vendor = tmp_path / "node_modules/three"
    shutil.copytree(ROOT / "frontend/vendor/three/0.162.0", vendor)
    (vendor / "package.json").write_text(json.dumps({
        "name": "three", "type": "module",
        "exports": {".": "./build/three.module.js", "./addons/*": "./examples/jsm/*"},
    }))
    for filename in ("robot-scene.js", "robot-appearance.js"):
        shutil.copyfile(ROOT / "frontend/src" / filename, tmp_path / filename)
    metadata = Labware.from_definition(cellvis, is_lidded=True).metadata
    (tmp_path / "exercise.mjs").write_text("""
import assert from 'node:assert/strict';
import * as THREE from 'three';
import {RobotScene} from './robot-scene.js';
globalThis.location = {hostname:'test'};
globalThis.fetch = () => {throw Error('Unexpected live profile lookup');};
const detail = """ + json.dumps(metadata) + """;
const scene = new RobotScene({}, {autoConnect:false, teachTipLengthMm:19.9});
assert.equal(scene.teachTipLengthMm,19.9);
assert.ok(scene._teachTipFetch instanceof Promise);
await scene._teachTipFetch;
const wrapper = new THREE.Group();
scene._attachGeneratedLidMesh(wrapper, detail);
const bounds = new THREE.Box3().setFromObject(wrapper);
const envelope = detail.generated_lid.lid_geometry;
for (const [actual, expected] of [
    [bounds.min.toArray(), envelope.lower_local_mm],
    [bounds.max.toArray(), envelope.upper_local_mm],
]) actual.forEach((value,index)=>assert.ok(Math.abs(value*1000-expected[index])<1e-5));
const lid = wrapper.children[0];
assert.equal(lid.userData.geometryModel, 'manufacturer_exterior_envelope');
assert.equal(lid.children.filter(child=>child.isMesh).length,1, 'No invented interior shell');
""")
    result = subprocess.run([node, str(tmp_path / "exercise.mjs")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
