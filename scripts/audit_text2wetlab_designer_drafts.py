"""Refresh conservative review findings on saved local-model Designer drafts.

This does not change model-authored nodes, deck entries, or approval status.
It only records checks about what the current Bravo executor can actually do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evaluate_text2wetlab import REVISION, TASKS, _source_bytes
from generate_text2wetlab_designer import _active_context, _hardware_issues, _node_issues

from pybravo.workflow.drafter.scientific_patterns import audit_scientific_patterns
from pybravo.workflow.storage import WorkflowStorage


def audit_saved_drafts(*, api_url: str, apply: bool = False,
                       workflows_dir: Path | None = None) -> list[dict]:
    context = _active_context(api_url)
    storage = WorkflowStorage(workflows_dir)
    report: list[dict] = []
    for summary in storage.list_workflows():
        if summary.get("protocol_generated_draft") is not True:
            continue
        workflow = storage.get_workflow(summary["id"])
        if not workflow:
            continue
        provenance = workflow.get("protocol_generated_provenance") or {}
        task = provenance.get("source_id")
        if (provenance.get("source_kind") != "text2wetlab_pinned_task"
                or task not in TASKS or provenance.get("dataset_revision") != REVISION):
            continue
        instruction_bytes = _source_bytes(task, "instruction.md", None)
        if instruction_bytes is None or hashlib.sha256(instruction_bytes).hexdigest() != provenance.get("source_sha256"):
            raise RuntimeError(f"Pinned task source digest mismatch for {task}.")
        instruction = instruction_bytes.decode("utf-8")
        existing = workflow.get("protocol_draft_issues") or []
        issues = [*existing,
                  *_node_issues(workflow),
                  *_hardware_issues(workflow, context, source_instruction=instruction),
                  *audit_scientific_patterns(
                      workflow, source_instruction=instruction,
                      head_type=context.get("head_type"),
                  )]
        issues = list({(issue["code"], issue.get("path"), issue["message"]): issue
                       for issue in issues}.values())
        workflow["protocol_draft_issues"] = issues
        if apply:
            storage.update_workflow(summary["id"], {"protocol_draft_issues": issues})
        report.append({
            "task": task,
            "workflow_id": summary["id"],
            "issue_count": len(issues),
            "issue_codes": sorted({issue["code"] for issue in issues}),
            "applied": apply,
        })
    return sorted(report, key=lambda item: item["task"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workflows-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = audit_saved_drafts(api_url=args.api_url, apply=args.apply,
                                workflows_dir=args.workflows_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
