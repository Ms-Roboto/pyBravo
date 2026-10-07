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
strokes must meet the documented OT-2 GEN2 working ranges. For an on-deck
heat-shock/recovery instruction, a timed hot-block pulse must end before
serial recovery pipetting begins. Simulation does
not establish that the protocol satisfies a scientific rubric or is safe to
run on physical hardware.

## Optional phased ActionPlan experiment

`scripts/experiment_text2wetlab_phased_action_ir.py` is an isolated alternative
for protocols too long to fit in one local-model response. The first Qwen call
authors the fixed OT-2 setup, cited initial-supply claims, and a short ordered
stage outline. A separate call authors the actions for each stage. Every
action cites an exact pinned task or paper line. The merger only concatenates
accepted stage actions; it inserts no experimental steps.

Each prefix is compiled against the installed OT-2 catalog, statically checked,
simulated, passed through the pinned event logger and event-safety check, and
audited for tip use and per-well material balance before the next Qwen call.
Unknown starting volumes stay unknown and are reported for review. A failed
prefix stops the experiment; the raw Qwen response and failure remain in the
output directory. The complete merged plan then runs the existing pinned lint,
simulator, event, and local science gates. `official_score` is always null;
Harbor's private scoring is not run. This path never controls hardware or
changes the production generator. The setup trace also lists procedural-looking
source lines omitted from stage citations for review; citations alone never
certify scientific completeness.

```sh
.venv/bin/python scripts/experiment_text2wetlab_phased_action_ir.py \
  --task opentrons-rna-extraction \
  --simulator /path/to/opentrons_simulate \
  --output-dir /tmp/text2wetlab-rna-phased
```

An exact earlier model setup can be resumed with `--saved-setup
/path/to/qwen_setup.json` only when its sibling trace matches the pinned task,
source digests, and raw response digest. `--max-stages` caps further local calls.

## Optional per-well material ledger

`material_ledger.audit_material_flow()` can inspect the trusted logger's
ordered `events` array without changing the generator or benchmark runner.
Pass `WellRef` → `InitialWell` entries for known source volumes and confirmed
empty destinations, plus `WellLimit` entries for catalog capacity or a
scientist-specified final reaction volume. `StageBoundary` and
`stage_targets_ul` let a reviewer check a prepared mixture *before* it is
consumed; this catches a duplicated preparation that a final-volume-only check
would miss. The result retains source-labelled component amounts, final
per-well volumes, ordered snapshots, and separate error/warning codes.

Missing starting volumes remain unknown, and missing destinations are not
silently assumed empty. The logger records only a multichannel anchor well;
callers must provide a geometry-backed `well_resolver` that returns one
`WellRef` per physical channel before multichannel flow is counted. Exact
component amounts for a partial withdrawal from mixed material require an
explicit mix event; a full withdrawal still conserves its total components.
This ledger does not establish reagent identity,
mixing quality, concentrations, or a protocol's scientific correctness. It is
currently a standalone audit utility; it does not change official scoring.

`material_plan_bridge.targets_from_accepted_plan()` can supply ledger targets
from an accepted local-model `PlanningResult`. Because `OT2Plan` has no
destination-well field, each `TargetBinding` must cite the exact vessel/well
and the corresponding reaction volume in the task or paper. For a generated
intermediate, it also needs a matching generated `DeckSource` and a trusted
stage-to-event boundary. Unlinked quotes, ambiguous reactions, and missing
boundaries become `review_gaps`; they never create a target by inference.
Callers can pass the resulting limits, stage targets and boundaries into the
ledger, along with independently confirmed initial volumes and geometry.
