"""The MCP facade exposes discovery only and proxies the active web context."""

from __future__ import annotations

import json
import subprocess
import sys

from pybravo.workflow.protocols.mcp_discovery import dispatch


def _rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def test_mcp_tools_are_read_only_and_proxy_correct_routes():
    listed = dispatch(_rpc("tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in listed} == {
        "bravo_get_capabilities", "bravo_get_methods", "bravo_get_recipes", "bravo_lookup_methods",
    }
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in listed)
    assert all(tool["annotations"]["destructiveHint"] is False for tool in listed)
    lookup = next(tool for tool in listed if tool["name"] == "bravo_lookup_methods")
    assert "dispense_volumes_ul" in lookup["inputSchema"]["properties"]["query"]["properties"]

    calls = []

    def fetch(path, payload):
        calls.append((path, payload))
        return {"path": path, "payload": payload}

    for name, arguments in [
        ("bravo_get_capabilities", {}),
        ("bravo_get_methods", {}),
        ("bravo_get_recipes", {}),
        ("bravo_lookup_methods", {"query": {"tip_id": "st_10ul", "operation": "distribute",
                                            "volume_ul": 10, "dispense_volumes_ul": [5, 5]}}),
    ]:
        result = dispatch(_rpc("tools/call", {"name": name, "arguments": arguments}), fetch)
        assert result["result"]["content"][0]["type"] == "text"
        assert json.loads(result["result"]["content"][0]["text"])["path"] == calls[-1][0]
    assert calls == [
        ("/api/protocols/capabilities", None),
        ("/api/protocols/methods", None),
        ("/api/protocols/recipes", None),
        ("/api/protocols/methods/lookup", {"tip_id": "st_10ul", "operation": "distribute",
                                            "volume_ul": 10, "dispense_volumes_ul": [5, 5]}),
    ]
    assert dispatch(_rpc("tools/call", {"name": "bravo_get_methods", "arguments": {"write": True}}),
                    fetch)["error"]["code"] == -32602
    assert len(calls) == 4


def test_stdio_initialization_and_notifications_are_protocol_clean():
    messages = [
        _rpc("initialize", {"protocolVersion": "2025-06-18"}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _rpc("tools/list"),
    ]
    process = subprocess.run(
        [sys.executable, "-m", "pybravo.workflow.protocols.mcp_discovery"],
        input="".join(json.dumps(item) + "\n" for item in messages),
        text=True, capture_output=True, check=True,
    )
    output = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(output) == 2
    assert output[0]["result"]["capabilities"]["tools"]["listChanged"] is False
    assert "tools" in output[1]["result"]
    assert process.stderr == ""
