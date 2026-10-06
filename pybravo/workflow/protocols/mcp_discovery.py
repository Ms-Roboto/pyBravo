"""Read-only MCP stdio facade for the running Protocol Assistant API.

Start with ``python -m pybravo.workflow.protocols.mcp_discovery``. The process
does not construct a separate Bravo context; it queries the same active
profile that the web application uses. MCP tool calls cannot change methods,
protocols, deck state, or hardware.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "pybravo-method-discovery"
MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_TOOLS = [
    {
        "name": "bravo_get_capabilities",
        "description": "Read the active Bravo capability manifest. It describes planning options, not live deck contents or execution permission.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "bravo_get_methods",
        "description": "Read versioned method registry summaries and evidence status for the active Bravo profile.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "bravo_get_recipes",
        "description": "Read reusable protocol-planning patterns. Recipes are examples, not execution qualifications.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "bravo_lookup_methods",
        "description": "Rank compatible methods, report applicability mismatches, and indicate when distribution needs paired aspirations. Read-only.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {
                "type": "object",
                "description": "A method lookup for the active machine and head; tip ID must name a compatible local definition.",
                "required": ["tip_id"],
                "properties": {
                    "operation": {"enum": ["transfer", "mix", "distribute"]},
                    "machine_id": {"type": "string"},
                    "head_type": {"type": "string"},
                    "tip_id": {"type": "string", "minLength": 1},
                    "tipbox_id": {"type": "string"},
                    "source_labware_id": {"type": "string"},
                    "destination_labware_id": {"type": "string"},
                    "reagent_family": {"type": "string"},
                    "volume_ul": {"type": "number", "exclusiveMinimum": 0},
                    "dispense_volumes_ul": {"type": "array", "minItems": 1,
                                            "items": {"type": "number", "exclusiveMinimum": 0}},
                    "source_anchor": {"type": "string"},
                    "destination_anchor": {"type": "string"},
                },
                "additionalProperties": False,
            }},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
]


def _base_url() -> str:
    value = os.environ.get("PYBRAVO_MCP_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("PYBRAVO_MCP_BASE_URL must be an HTTP(S) origin without credentials")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("PYBRAVO_MCP_BASE_URL must be an origin without a path or query")
    return value


def _fetch(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(_base_url() + path, data=data, headers=headers,
                          method="GET" if data is None else "POST")
    with request.urlopen(req, timeout=10) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Discovery response exceeds the MCP size limit")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("Discovery endpoint returned a non-object JSON value")
    return parsed


def _result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(message: dict[str, Any], fetch: Callable[..., dict[str, Any]] = _fetch) -> dict[str, Any] | None:
    """Handle one MCP JSON-RPC message; notifications receive no response."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) else None,
                      -32600, "Invalid JSON-RPC request")
    if "id" not in message:
        return None
    request_id = message["id"]
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params must be an object")
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})
    if method != "tools/call":
        return _error(request_id, -32601, "Method not found")

    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "Tool arguments must be an object")
    if name == "bravo_get_capabilities" and not arguments:
        path, payload = "/api/protocols/capabilities", None
    elif name == "bravo_get_methods" and not arguments:
        path, payload = "/api/protocols/methods", None
    elif name == "bravo_get_recipes" and not arguments:
        path, payload = "/api/protocols/recipes", None
    elif name == "bravo_lookup_methods" and set(arguments) == {"query"} and isinstance(arguments["query"], dict):
        path, payload = "/api/protocols/methods/lookup", arguments["query"]
    else:
        return _error(request_id, -32602, "Unknown tool or invalid arguments")

    try:
        data = fetch(path, payload)
    except (OSError, ValueError, json.JSONDecodeError, error.HTTPError) as exc:
        return _result(request_id, {
            "isError": True,
            "content": [{"type": "text", "text": f"Bravo discovery failed: {exc}"}],
        })
    return _result(request_id, {
        "content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"), allow_nan=False)}],
    })


def main() -> None:
    for line in sys.stdin:
        try:
            parsed = json.loads(line)
            response = dispatch(parsed)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
