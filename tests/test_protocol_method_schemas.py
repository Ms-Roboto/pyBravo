"""Published method schemas match the read-only registry and lookup contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pybravo.bravo import Bravo
from pybravo.workflow.protocols.capabilities import build_capability_manifest
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.methods import MethodQuery, MethodRecord, lookup_methods, method_registry


def test_published_method_schemas_validate_live_registry_and_manifest_pointer():
    jsonschema = pytest.importorskip("jsonschema")
    root = Path(__file__).resolve().parents[1] / "schemas"
    schemas = {}
    for name in ("bravo-method-record-v1", "bravo-method-query-v1",
                 "bravo-method-registry-v1", "bravo-method-lookup-v1"):
        schema = json.loads((root / f"{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    assert set(schemas["bravo-method-record-v1"]["properties"]) == set(MethodRecord.model_json_schema()["properties"])
    assert set(schemas["bravo-method-query-v1"]["properties"]) == set(MethodQuery.model_json_schema()["properties"])

    example = json.loads((root / "examples/bravo-method-registry-v1.example.json").read_text())
    validator = jsonschema.Draft202012Validator(schemas["bravo-method-registry-v1"])
    assert not list(validator.iter_errors(example))
    bravo = Bravo(mode="simulation")
    try:
        context = machine_context(bravo)
        registry = method_registry(context)
        manifest = build_capability_manifest(context)
    finally:
        bravo.disconnect()
    assert not list(validator.iter_errors(registry))
    assert manifest["method_registry"]["digest"] == registry["digest"]
    assert manifest["method_registry"]["method_count"] == len(registry["methods"])
    record_validator = jsonschema.Draft202012Validator(schemas["bravo-method-record-v1"])
    for row in registry["methods"]:
        assert not list(record_validator.iter_errors(row))
    query_validator = jsonschema.Draft202012Validator(schemas["bravo-method-query-v1"])
    query_example = json.loads((root / "examples/bravo-method-query-v1.example.json").read_text())
    assert query_example["dispense_volumes_ul"] == [5, 5]
    assert not list(query_validator.iter_errors(query_example))
    lookup = lookup_methods(context, query_example)
    assert lookup["issues"] == []
    lookup_validator = jsonschema.Draft202012Validator(schemas["bravo-method-lookup-v1"])
    lookup_example = json.loads((root / "examples/bravo-method-lookup-v1.example.json").read_text())
    assert not list(lookup_validator.iter_errors(lookup_example))
    assert not list(lookup_validator.iter_errors(lookup))
    assert lookup["shared_aspiration_feasible"] is True
    fallback_query = {**query_example, "volume_ul": 15, "dispense_volumes_ul": [5, 5, 5]}
    fallback = lookup_methods(context, fallback_query)
    assert fallback["issues"] == []
    assert fallback["shared_aspiration_feasible"] is False
    assert any(mismatch["field"] == "shared_aspiration_capacity"
               for candidate in fallback["candidates"] for mismatch in candidate["mismatches"])
    assert not list(lookup_validator.iter_errors(fallback))
