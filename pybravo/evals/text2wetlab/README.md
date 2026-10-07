# Text2WetLab OT-2 benchmark adapter

This package generates an Opentrons OT-2 Python API protocol from each task's
written instruction. It uses the configured OpenAI-compatible **local** model
server, validates the Python entry point, and runs `opentrons_simulate` when
that command is available. It never invokes Bravo or Opentrons hardware.

```sh
.venv/bin/python -m pybravo.evals.text2wetlab \
  --task-dir /path/to/task \
  --instruction-file /path/to/task/instruction.md \
  --simulator-command /path/to/opentrons_simulate \
  --event-logger /path/to/pinned/runlog.py
```

An optional `--paper-file` adds task-supplied scientific source text to the
model prompt. For a paper with clear Methods and Results headings, the adapter
passes the complete, verbatim Methods section instead of unrelated article
sections; its trace records the full-source and excerpt digests and line span.
If the section cannot be identified completely, the full supplied text is
preserved. The adapter also reads the installed simulator's labware definitions
and any task-supplied custom JSON to give the model exact well counts and
column layouts. These catalog facts do not supply experimental volumes or
reagents.

Model endpoint and alias default to the existing
`PYBRAVO_DRAFTER_BASE_URL` and `PYBRAVO_DRAFTER_MODEL` settings; the CLI also
accepts `--model-base-url` and `--model`. The benchmark CLI defaults to a
300-second model timeout, 16,000 output tokens, and no automatic repeat of a
timed-out HTTP request. `--model-timeout` and `--max-output-tokens` can adjust
those bounds.
When a task supplies custom Opentrons labware JSON, place the pinned files in
a directory and pass `--labware-dir /path/to/labware`. The adapter passes that
directory to both the CLI simulator and structured logger; the generated
protocol only names the labware in `load_labware()`.

The task directory receives `protocol.py` only after static validation and,
when the simulator is installed, a passing simulation. It always receives
`generation_trace.json` after an attempted generation. Statically valid but
rejected sources remain as `candidate_attempt_N.py` files, with exact paths in
the trace; unchanged repeats skip another simulator run. A trace marked
`static_validated_only` means no simulator was found; it is not a simulation
pass. A `simulated` trace also requires structured action-event validation:
each tip may touch one specimen well before disposal, while a reservoir may
feed multiple wells of an otherwise destination-only plate. A tip may not
aspirate from two distinct reagent-stock wells, and P20/P300/P1000 liquid
strokes must meet the documented OT-2 GEN2 working ranges. Simulation does
not establish that the protocol satisfies a scientific rubric or is safe to
run on physical hardware.
