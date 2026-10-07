"""CLI for generating an OT-2 benchmark protocol in one task directory."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from pybravo.workflow.protocols.llm import LocalLLMConfig

from .adapter import generate_ot2_protocol


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True, help="Sole directory for protocol.py and generation_trace.json")
    parser.add_argument("--instruction-file", type=Path, help="Task instruction; defaults to TASK_DIR/instruction.md")
    parser.add_argument("--paper-file", type=Path, help="Optional additional scientific source supplied with the task")
    parser.add_argument("--simulator-command", help="Path or executable name for opentrons_simulate")
    parser.add_argument("--event-logger", type=Path,
                        help="Task's pinned structured runlog.py; required when simulation runs")
    parser.add_argument("--labware-dir", type=Path,
                        help="Optional directory of task-supplied custom Opentrons labware definitions")
    parser.add_argument("--simulation-timeout", type=float, default=180)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--patch-attempts", type=int, default=1,
                        help="Bounded local line-edit repairs after an event-safety failure (0–3)")
    parser.add_argument("--evidence-planning", action="store_true",
                        help="Ask the same local model for a cited plan and audit it before code generation")
    parser.add_argument("--planning-attempts", type=int, default=2,
                        help="Maximum local-model attempts to ground and audit the optional plan (1–3)")
    parser.add_argument("--model-base-url", help="OpenAI-compatible local model server URL")
    parser.add_argument("--model", help="Model alias on the local server")
    parser.add_argument("--model-timeout", type=float, default=300,
                        help="Per-request local model timeout in seconds (default: 300)")
    parser.add_argument("--max-output-tokens", type=int, default=16_000,
                        help="Maximum local model response tokens (default: 16000)")
    args = parser.parse_args(argv)

    instruction_path = args.instruction_file or args.task_dir / "instruction.md"
    try:
        instruction = instruction_path.read_text(encoding="utf-8")
        source = args.paper_file.read_text(encoding="utf-8") if args.paper_file else None
        config = LocalLLMConfig.from_env()
        if args.model_base_url is not None:
            config = replace(config, base_url=args.model_base_url)
        if args.model is not None:
            config = replace(config, model=args.model)
        # Transport retries on a timed-out generation repeat the same expensive
        # call without any new diagnostic. Repair attempts follow model output.
        config = replace(config, timeout_s=args.model_timeout,
                         max_tokens=args.max_output_tokens, retries=0)
        result = asyncio.run(generate_ot2_protocol(
            instruction,
            args.task_dir,
            scientific_source=source,
            config=config,
            simulator_command=args.simulator_command,
            event_logger_path=args.event_logger,
            labware_dir=args.labware_dir,
            repair_attempts=args.repair_attempts,
            patch_attempts=args.patch_attempts,
            evidence_planning=args.evidence_planning,
            planning_attempts=args.planning_attempts,
            simulation_timeout_s=args.simulation_timeout,
        ))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"OT-2 benchmark generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "protocol_path": str(result.protocol_path),
        "trace_path": str(result.trace_path),
        "attempts": result.attempts,
        "simulation": result.simulation.status,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
