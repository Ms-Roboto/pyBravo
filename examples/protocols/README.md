# Protocol assistant acceptance examples

`benchmark.json` contains 18 synthetic engineering cases with explicit simulated
catalogs. They cover transfers, mixing, serial dilution, repeated blocks, waiting,
manual handoffs, gripper moves, dense well footprints, missing information,
source grounding and inventory constraints. These are software test inputs, not
scientist-approved methods or hardware-qualified procedures.

Run deterministic validation and compilation checks:

```sh
uv run --extra dev python -m pybravo.workflow.protocols.evaluate --output /tmp/protocol-benchmark.json
```

Include local-model extraction of the source-aligned examples:

```sh
uv run --extra llm python -m pybravo.workflow.protocols.evaluate --extract --output /tmp/protocol-extraction-benchmark.json
```

`--limit 2` limits live model calls during a smoke check. The model connection uses
the Protocol Assistant environment configuration. Live results report numerical
parameter accuracy, operation sequence agreement, missing steps, validation
questions and elapsed time. They do not measure biological equivalence; a
scientist must review well mappings, setup choices and manual instructions.
`scientist_edit_seconds` remains null until an actual scientist measures review
time. Hardware qualification remains false.

For a laboratory evaluation, copy the suite and supply 10–20 scientist-reviewed
protocols and approved instrument setups. Keep deliberately incomplete examples.
Record the scientist's corrections and elapsed review time through Protocol
Assistant; compare these with extraction and strict simulation outcomes before
promoting a method into the approved protocol library.
