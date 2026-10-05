"""Reproducible engineering acceptance benchmark, optionally using the local LLM.

Synthetic cases are not scientist-qualified laboratory methods. Live extraction
metrics measure numeric values and operation retention; they do not establish
experimental equivalence, hardware qualification or scientist editing time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from .compiler import compile_plan
from .validation import validate_plan

DEFAULT_SUITE = Path(__file__).resolve().parents[3] / "examples" / "protocols" / "benchmark.json"


def evaluate_suite(path: str | Path = DEFAULT_SUITE) -> dict[str, Any]:
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    results = []
    for case in suite["cases"]:
        started = perf_counter()
        report = validate_plan(case["plan"], case["setup"], case["context"], sources=case["source"])
        expected = case["expected"]
        codes = {issue["code"] for issue in report["issues"]}
        passed = report["valid"] == expected["valid"] and set(expected.get("issue_codes", [])) <= codes
        nodes = None
        if report["valid"]:
            workflow = compile_plan(case["plan"], case["setup"], case["context"], sources=case["source"])
            nodes = len(workflow["graph"]["nodes"])
        results.append({"id": case["id"], "passed": passed, "valid": report["valid"], "expected_valid": expected["valid"],
                        "issue_codes": sorted(codes), "question_count": len(report["questions"]),
                        "compiled_nodes": nodes, "elapsed_seconds": round(perf_counter() - started, 6)})
    return {"review_status": suite.get("review_status", "unreviewed"), "cases": results,
            "passed": sum(r["passed"] for r in results), "total": len(results),
            "scientist_edit_seconds": None, "hardware_qualified": False}


def _steps(steps: list[dict]) -> list[dict]:
    expanded: list[dict] = []
    for step in steps:
        count = min(1000, max(1, step.get("repeat") or 1))
        body = _steps(step.get("steps", [])) if step["kind"] == "repeat" else [step]
        expanded.extend(body * count)
        if len(expanded) > 1000:
            return expanded[:1000]
    return expanded


async def evaluate_extraction(path: str | Path = DEFAULT_SUITE, *, limit: int | None = None) -> list[dict]:
    from .ingest import IngestedProtocol, SourceParagraph
    from .llm import extract_protocol_plan

    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    # Invalid engineering plans often deliberately contradict their source.
    # Score extraction against source-aligned cases, including an incomplete one.
    cases = [c for c in suite["cases"] if c["expected"]["valid"] or c["id"] == "missing_volume"]
    results = []
    for case in cases[:limit]:
        started = perf_counter()
        source = IngestedProtocol(source_id="benchmark-" + case["id"], name=case["description"],
                                  paragraphs=[SourceParagraph.model_validate(p) for p in case["source"]])
        try:
            extracted = await extract_protocol_plan(source, context={**case["context"], "setup": case["setup"],
                "current_plan": {"name": case["plan"]["name"], "materials": case["plan"]["materials"], "steps": []}})
            actual_plan = extracted.plan.model_dump()
            expected_steps, actual_steps = _steps(case["plan"]["steps"]), _steps(actual_plan["steps"])
            matched, total = 0, 0
            for index, step in enumerate(expected_steps):
                for field in ("volume_ul", "cycles", "duration_s"):
                    if field in step:
                        total += 1
                        if index < len(actual_steps) and actual_steps[index]["kind"] == step["kind"] and actual_steps[index].get(field) == step[field]:
                            matched += 1
            report = validate_plan(actual_plan, case["setup"], case["context"], sources=case["source"])
            results.append({"id": case["id"], "status": "extracted", "model": extracted.metadata,
                "step_kind_sequence_match": [s["kind"] for s in actual_steps] == [s["kind"] for s in expected_steps],
                "omitted_step_count": max(0, len(expected_steps) - len(actual_steps)),
                "numeric_parameter_accuracy": matched / total if total else None,
                "numeric_parameters_checked": total, "valid_after_extraction": report["valid"],
                "question_count": len(report["questions"]), "issue_codes": sorted({i["code"] for i in report["issues"]}),
                "elapsed_seconds": round(perf_counter() - started, 3)})
        except (ValueError, RuntimeError) as error:
            results.append({"id": case["id"], "status": "failed", "error": str(error),
                            "elapsed_seconds": round(perf_counter() - started, 3)})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--extract", action="store_true", help="Also measure live local-model extraction (no hardware calls).")
    parser.add_argument("--limit", type=int, help="Limit live extraction cases.")
    parser.add_argument("--output", type=Path, help="Save benchmark evidence as JSON.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    result = evaluate_suite(args.suite)
    if args.extract:
        result["live_extraction"] = asyncio.run(evaluate_extraction(args.suite, limit=args.limit))
    serialized = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    raise SystemExit(0 if result["passed"] == result["total"] else 1)


if __name__ == "__main__":
    main()
