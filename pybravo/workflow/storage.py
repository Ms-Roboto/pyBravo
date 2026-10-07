"""
Workflow storage — JSON file persistence for workflow definitions.

Workflows are stored as individual JSON files in ~/.pybravo/workflows/.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_WORKFLOWS_DIR = Path.home() / ".pybravo" / "workflows"

# Model-generated protocols are editable diagrams, not execution grants. Keep
# scripts and hardware-control nodes out of this import path; unsupported
# scientific stages should be represented as explicit Manual checkpoints.
GENERATED_DRAFT_NODE_TYPES = frozenset({
    "flow/Start", "flow/End", "flow/Loop", "flow/IfElse", "flow/Frame",
    "plate/PickPlace", "plate/Stack", "plate/Destack", "plate/Mount",
    "plate/Unmount", "plate/Delid", "plate/Relid",
    "liquid/Aspirate", "liquid/Dispense", "liquid/Mix",
    "tips/TipsOn", "tips/TipsOff", "system/Initialize", "system/Manual", "system/Wait",
})
_GENERATED_DRAFT_FORBIDDEN_FIELDS = frozenset({
    "approval", "protocol_session_id", "protocol_compiled_preview", "protocol_chat_draft",
})
MAX_GENERATED_DRAFT_NODES = 5000
MAX_GENERATED_DRAFT_BYTES = 8_000_000


def assert_safe_generated_draft(data: dict[str, Any]) -> None:
    """Check a saved/generated draft cannot smuggle executable code or release metadata.

    Scientific and deck completeness are reviewed later; an operator must be
    able to save a partially edited draft. This gate only checks the import
    boundary and the diagram shape needed for Designer to load it.
    """
    if not isinstance(data, dict):
        raise ValueError("Generated draft must be a workflow object.")
    if any(key in data for key in _GENERATED_DRAFT_FORBIDDEN_FIELDS):
        raise ValueError("Generated drafts cannot carry approval, release, or preview markers.")
    if data.get("library"):
        raise ValueError("Generated drafts cannot contain workflow-level Python code.")
    graph = data.get("graph")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("links"), list):
        raise ValueError("Generated draft needs a Designer graph with nodes and links.")
    nodes = graph["nodes"]
    if not 2 <= len(nodes) <= MAX_GENERATED_DRAFT_NODES:
        raise ValueError(f"Generated draft must contain 2–{MAX_GENERATED_DRAFT_NODES} nodes.")
    if len(graph["links"]) > MAX_GENERATED_DRAFT_NODES * 3:
        raise ValueError("Generated draft has too many graph links.")
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in GENERATED_DRAFT_NODE_TYPES:
            raise ValueError(f"Generated draft contains an unsupported or executable node: {node.get('type') if isinstance(node, dict) else node!r}.")
        if not isinstance(node.get("properties", {}), dict):
            raise ValueError("Generated draft node properties must be objects.")
    deck = data.get("deck", {})
    if not isinstance(deck, dict) or any(
        str(slot) not in {str(index) for index in range(1, 10)} or not isinstance(stack, list)
        for slot, stack in deck.items()
    ):
        raise ValueError("Generated draft deck positions must be stacks in slots 1–9.")
    try:
        size = len(json.dumps(data, allow_nan=False, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Generated draft must be finite JSON data.") from exc
    if size > MAX_GENERATED_DRAFT_BYTES:
        raise ValueError("Generated draft is too large for Designer.")


class WorkflowStorage:
    """CRUD operations for workflow JSON files on disk."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._dir = Path(directory) if directory else DEFAULT_WORKFLOWS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── List ──────────────────────────────────────────────────────────

    def list_workflows(self) -> list[dict[str, Any]]:
        """Return summary metadata for every saved workflow."""
        results = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                results.append({
                    "id": data.get("id", path.stem),
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "modified": data.get("modified", ""),
                    "created": data.get("created", ""),
                    "protocol_generated_draft": data.get("protocol_generated_draft") is True,
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping malformed workflow file", path=str(path), error=str(exc))
        return results

    # ── Get ───────────────────────────────────────────────────────────

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Load a single workflow by ID."""
        path = self._resolve_path(workflow_id)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ── Create ────────────────────────────────────────────────────────

    def create_workflow(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new workflow.  Assigns an ID if not present."""
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        data.setdefault("created", now)
        data["modified"] = now
        self._write(data)
        logger.info("Workflow created", id=data["id"], name=data.get("name"))
        return data

    def create_generated_draft(
        self, workflow: dict[str, Any], *, provenance: dict[str, Any], issues: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist a model-produced, editable, non-executable Designer draft."""
        data = copy.deepcopy(workflow)
        assert_safe_generated_draft(data)
        identity = str(uuid.uuid4())
        data.pop("created", None)
        data.pop("modified", None)
        data["id"] = identity
        data["protocol_generated_draft"] = True
        data["protocol_draft_status"] = "unreviewed"
        data["protocol_generated_root_id"] = identity
        data["protocol_generated_provenance"] = copy.deepcopy(provenance)
        data["protocol_draft_issues"] = copy.deepcopy(issues or [])
        return self.create_workflow(data)

    def create_generated_copy(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """Save As/Duplicate a generated draft without laundering its provenance."""
        data = copy.deepcopy(workflow)
        if data.pop("protocol_generated_draft", None) is not True:
            raise ValueError("Only marked generated drafts can use this copy path.")
        root_id = data.pop("protocol_generated_root_id", None)
        root = self.get_workflow(root_id) if isinstance(root_id, str) else None
        if not root or root.get("protocol_generated_draft") is not True or root.get("protocol_generated_root_id") != root_id:
            raise ValueError("Generated draft origin is missing or invalid.")
        if data.pop("protocol_generated_provenance", None) != root.get("protocol_generated_provenance"):
            raise ValueError("Generated draft provenance cannot be changed.")
        data.pop("protocol_draft_status", None)
        data.pop("protocol_draft_issues", None)
        data.pop("protocol_draft_validation_stale", None)
        data.pop("id", None)
        assert_safe_generated_draft(data)
        identity = str(uuid.uuid4())
        data.pop("created", None)
        data.pop("modified", None)
        data["id"] = identity
        data["protocol_generated_draft"] = True
        data["protocol_draft_status"] = "unreviewed"
        data["protocol_generated_root_id"] = root_id
        data["protocol_generated_provenance"] = copy.deepcopy(root["protocol_generated_provenance"])
        data["protocol_draft_issues"] = copy.deepcopy(workflow.get("protocol_draft_issues") or [])
        data["protocol_draft_validation_stale"] = True
        return self.create_workflow(data)

    def update_generated_draft(self, workflow_id: str, workflow: dict[str, Any]) -> dict[str, Any] | None:
        """Keep a generated draft's unreviewed status and original source on Save."""
        existing = self.get_workflow(workflow_id)
        if existing is None:
            return None
        if existing.get("protocol_generated_draft") is not True:
            raise ValueError("This workflow is not a generated protocol draft.")
        data = copy.deepcopy(workflow)
        if data.pop("protocol_generated_draft", None) is not True:
            raise ValueError("Generated draft status cannot be removed.")
        if data.pop("protocol_generated_root_id", None) != existing.get("protocol_generated_root_id"):
            raise ValueError("Generated draft origin cannot be changed.")
        if data.pop("protocol_generated_provenance", None) != existing.get("protocol_generated_provenance"):
            raise ValueError("Generated draft provenance cannot be changed.")
        data.pop("protocol_draft_status", None)
        data.pop("protocol_draft_issues", None)
        data.pop("protocol_draft_validation_stale", None)
        assert_safe_generated_draft(data)
        data["protocol_generated_draft"] = True
        data["protocol_draft_status"] = "unreviewed"
        data["protocol_generated_root_id"] = existing["protocol_generated_root_id"]
        data["protocol_generated_provenance"] = copy.deepcopy(existing["protocol_generated_provenance"])
        # Saving is not validation. Keep the prior findings visible while
        # marking them stale, rather than making an edited draft show zero
        # unresolved checks before any check has actually run.
        data["protocol_draft_issues"] = copy.deepcopy(existing.get("protocol_draft_issues") or [])
        data["protocol_draft_validation_stale"] = True
        return self.update_workflow(workflow_id, data)

    # ── Update ────────────────────────────────────────────────────────

    def update_workflow(self, workflow_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update an existing workflow by ID."""
        existing = self.get_workflow(workflow_id)
        if existing is None:
            return None
        existing.update(data)
        existing["id"] = workflow_id
        existing["modified"] = datetime.now(timezone.utc).isoformat()
        self._write(existing)
        logger.info("Workflow updated", id=workflow_id)
        return existing

    # ── Delete ────────────────────────────────────────────────────────

    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow by ID.  Returns True if deleted."""
        path = self._resolve_path(workflow_id)
        if not path or not path.exists():
            return False
        path.unlink()
        logger.info("Workflow deleted", id=workflow_id)
        return True

    # ── Import / Export ───────────────────────────────────────────────

    def import_workflow(self, raw_json: str | bytes) -> dict[str, Any]:
        """Import a workflow from raw JSON content."""
        data = json.loads(raw_json)
        if not isinstance(data, dict):
            raise ValueError("Workflow JSON must be an object")
        # Assign a new ID on import to avoid collisions
        data["id"] = str(uuid.uuid4())
        data["modified"] = datetime.now(timezone.utc).isoformat()
        self._write(data)
        logger.info("Workflow imported", id=data["id"], name=data.get("name"))
        return data

    def export_workflow(self, workflow_id: str) -> str | None:
        """Export a workflow as a formatted JSON string."""
        data = self.get_workflow(workflow_id)
        if data is None:
            return None
        return json.dumps(data, indent=2, ensure_ascii=False)

    # ── Internal ──────────────────────────────────────────────────────

    def _resolve_path(self, workflow_id: str) -> Path | None:
        """Find the file for a given workflow ID."""
        # First try direct filename match
        direct = self._dir / f"{workflow_id}.json"
        if direct.exists():
            return direct
        # Search by ID inside files
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("id") == workflow_id:
                    return path
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _write(self, data: dict[str, Any]) -> None:
        """Write workflow data to disk."""
        workflow_id = data["id"]
        # Use a safe filename derived from the ID
        safe_name = workflow_id.replace("/", "_").replace("\\", "_")
        path = self._dir / f"{safe_name}.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
