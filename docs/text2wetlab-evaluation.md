# Text2WetLab evaluation boundary

Status checked on 2026-10-07 against
[Text2WetLab](https://huggingface.co/datasets/EvanOLeary/Text2WetLab) revision
d7c8a9b93428997447eeaf2ee9e27ac3ce026872 and the saved local reports below. The seven public
Harbor tasks require **Opentrons OT-2 Python API v2** protocols on task-specific fixed
decks. Their verifier applies anti-hack checks, separate static lint where supplied,
Opentrons 7.5.0 simulation, a run log, and a five-item rubric judged from the run. The RNA
task embeds its rubric in tests/grade.py and has no separate protocol_lint.py. The dataset's
six-check PyLabRobot gym is a different evaluation; its result cannot be added to the Harbor
score.

The requested 100% means **35/35 Harbor rubric items across all seven tasks** under the
dataset's actual verifier. The local runner has **no official Harbor score** (official_score
is null). Docker Desktop was started on 2026-10-07, and pinned container checks now run;
`ANTHROPIC_API_KEY` remains unset, so the rubric judge cannot run. A local simulator or
pre-judge container pass does not substitute for the complete official verifier. All protocol
candidates in this effort
are authored by the local Qwen model at http://sparky.local:8000/v1; pyBravo supplies source
material, prompts, validators, and storage, not hand-written protocol answers.

## Two distinct outputs

The OT-2 adapter in pybravo/evals/text2wetlab/ is a benchmark harness. It generates OT-2
Python candidates for the pinned tasks and checks them in an isolated Opentrons environment.
Those files are **not Bravo protocols**. A passing OT-2 simulation says the candidate is
API-compatible and survived the local mechanical checks; it does not establish scientific
fidelity or Bravo hardware readiness.

Separately, Qwen has authored **seven saved, loadable pyBravo Designer drafts**, one for
each task. scripts/generate_text2wetlab_designer.py saves the model's primitive-node DAG,
exact source digests, and review issues through /api/protocols/generated-drafts; each saved
workflow opens with /designer?workflow=<workflow_id>. The local generation report is
/tmp/pybravo-text2wetlab-designer/report.json. All seven are marked unreviewed; Designer
disables Simulate and Execute for them. They are inspectable starting points, not completed
or approved Bravo methods. Their current issues include unavailable liquid classes,
unverified tip-box and deck substitutions, omitted sample mappings, and, in the RNA draft,
unaccounted elution recovery. OT-2 deck slots and external modules cannot be copied onto
Bravo as hardware facts.

The earlier 3,107-check readiness screen showed how a compact intent can expand into
thousands of per-well questions. A saved DAG does not resolve those questions. Before a
Bravo run, the selected head, tips, rack inventory, labware, liquid method, heights, deck,
repeated-well mapping, external handoffs, strict simulation, and scientist approval still
need to agree. Draft review issues are grouped diagnostics, not a release decision.

## Local evidence snapshot

These are outcomes from **separate saved runs**, not one simultaneous seven-task evaluation
with the current code. Six distinct tasks reached the independent static/lint, Opentrons
simulation, run-log, and event-safety gates at least once. Provenance-checked replays of
four older Qwen-authored candidates plus the newer colony run give **5/7 tasks with complete
local mechanical evidence** under the current audit. The newer scientific-coverage gate
makes Golden Gate a known failure despite its older report's simulator_passed status. All
five local acceptances still have needs_review scientific or manual-stage checks; none has
an official score.

| Task | Saved local mechanical evidence | Outstanding scientific or generation issue |
| --- | --- | --- |
| A1–A12 100 µL transfer | Current pinned recheck passed | Task fidelity still needs review; Bravo draft remains unreviewed. |
| Split 200 µL between two wells | Current pinned recheck passed | Task fidelity still needs review; Bravo draft remains unreviewed. |
| AMPure cleanup | Current pinned recheck passed with sample-isolated fresh tips | Physical tip replenishment, binding/wash/drying handoffs, and contamination need review. |
| E. coli heat shock | Current pinned recheck passed after a Qwen line repair put the 37 °C transition before SOC additions | Paper fidelity and external-module stages still require review. |
| Golden Gate assembly | Older saved report passed mechanical gates | Its embedded local rubric audit **failed** required PCR setup and ordered DpnI/cleanup handoffs. Current adapter rejects observable audit failures; the saved report predates that gate. |
| Colony PCR | A newer Qwen draft passed current local lint, simulation, pinned run log, and event-safety gates | Primer-stock concentration was not specified, the 4 µL primer-pair choice needs scientific review, and tip replenishment requires an explicit operator handoff before physical use. |
| RNA extraction | No accepted local simulation | Fresh drafts exceeded the effective 200 µL filter-tip capacity or exhausted installed tips; 48-sample mapping and elution recovery remain unresolved. |

The provenance-checked current recheck reports are in
/tmp/pybravo-text2wetlab-a1-current-timed/,
/tmp/pybravo-text2wetlab-split-current-timed/,
/tmp/pybravo-text2wetlab-ampure-current-timed2/,
/tmp/pybravo-text2wetlab-ecoli-current-timed/, and
/tmp/pybravo-text2wetlab-colony-current-timed/. Their `generation` field is null: each
recheck verified the original Qwen trace and protocol digest, then applied the current
pinned gates without asking the model to regenerate a protocol. Other reports are in
/tmp/pybravo-text2wetlab-golden-planning/, /tmp/pybravo-text2wetlab-golden-direct5/,
/tmp/pybravo-text2wetlab-colony-current2/, and
/tmp/pybravo-text2wetlab-rna-current2/. Each report and task trace records its own code and
source hashes, attempts, and gate results. The Golden Gate report contains
local_rubric_audit.status: failed even though its top-level status is the older
simulator_passed; the latter must not be counted as current scientific acceptance. A later
evidence-planning rerun in /tmp/pybravo-text2wetlab-golden-science-gated/ timed out at the
local Qwen 300-second limit before saving a plan or reaching simulation. A direct-draft
science-gated rerun also failed: Qwen repaired tip isolation but overfilled a cited 25 µL
PCR and did not produce an accepted ordered handoff sequence.

The read-only aggregate audit is
`/tmp/pybravo-text2wetlab-current-timed-evidence-audit.json`. It confirms the five
mechanical-evidence tasks above; Golden Gate has failed observable science checks and RNA
has no accepted local report. It cannot run the official anti-hack gate or rubric judge.

The **unchanged pinned `tests/test.sh`** has now also run inside task-specific Docker images
for six saved Qwen candidates. A1–A12, split, AMPure, E. coli, colony PCR, and the older
Golden Gate candidate all passed that container's lint, anti-hack, and simulation gates.
Golden Gate remains rejected by the separate local scientific audit. In all six container
runs the judge returned an authentication error because no Anthropic key was supplied;
its zero `reward` is therefore **not** a judged rubric score. Candidate digests, pinned
test/Dockerfile hashes, verdicts, and verifier paths are recorded in
`/tmp/pybravo-text2wetlab-official-container-summary-all.json`. RNA has no accepted
candidate to check. No official 35-item claim follows from these pre-judge passes.

## How the current local harness stays grounded

When a paper is supplied, prepare_scientific_source() selects a **verbatim Methods passage
and up to two task-relevant Results subsections** if they fit the input budget; otherwise it
retains the full source. The trace records selection strategy, original and excerpt SHA-256
digests, and line spans. Excerpt selection reduces irrelevant context but can omit a
pertinent passage, so paper fidelity remains a review item. The heat-shock paper is absent
from the dataset checkout and must be supplied with its pinned digest
(768a5703630e593a3eb5be8cac6267af0cf26ba1b43dbc797d24b08cbaf3f18a).

Optional --evidence-planning first asks Qwen for a typed plan with citations to the task or
supplied source. Deterministic checks cover source inventory, intermediate production order,
final reaction volume, stock dilution, single-stroke pipette ranges, tip demand and refill
permission, multichannel geometry, and module state before dependent pipetting. Failed plans
receive bounded correction feedback and are retained as planning_attempt_*.json. If no plan
passes, generation falls back to the original source and the trace says so; fallback is not
evidence that a plan was validated. In the saved colony run, the second plan passed the
**older** checks but its code exhausted tips. The planner now also requires each robot
addition to name an executable pipette stage and each such stage to have tip demand. Both
Golden Gate planning attempts failed citation grounding and its saved code generation fell
back to source text.

The adapter applies static source limits, the independent Opentrons simulator, structured
tip/volume/event checks, and bounded Qwen repairs. A focused line patch may correct a
mechanical error only if its source guard preserves liquid actions, relevant locations, deck
bindings, timed commands, and tip safety; the patched candidate is checked again. Explicit
task permission is required for a tip-rack reset. This repair mechanism does not invent
missing chemistry or approve a liquid method.

The read-only local_rubric_audit inspects **observable** facts for the seven tasks and
retains supported, failed, or needs_review for each of the 35 rubric items. The current
adapter rejects observable failures and gives Qwen feedback grounded only in the task or
paper passages. It does not disclose judge wording as a generation instruction, certify
paper interpretation, grade manual stages, or compute an official score. Golden Gate
demonstrates why this extra gate matters: the simulator observed valid pipette events while
required scientific content was missing.

## Reproduce the local checks

Create the isolated simulator environment matching the pinned Dockerfile:

~~~sh
python3.10 -m venv /tmp/pybravo-text2wetlab-ot2-py310
/tmp/pybravo-text2wetlab-ot2-py310/bin/pip install \
  'opentrons==7.5.0' 'opentrons-shared-data==7.5.0' 'pydantic<2'
/tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate --version
~~~

Run selected pinned tasks; each output directory is a fresh local experiment. The runner
checks only selected tasks, records hashes, and leaves official_score null:

~~~sh
.venv/bin/python scripts/evaluate_text2wetlab.py \
  --task a1-a12-100ul --task split-200ul-two-wells \
  --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
  --output-dir /tmp/pybravo-text2wetlab-check
~~~

To recheck an existing Qwen-authored candidate without a new model call, pass the saved
run directory with `--recheck-from` and use a new output directory. For example:

~~~sh
.venv/bin/python scripts/evaluate_text2wetlab.py \
  --task split-200ul-two-wells \
  --recheck-from /tmp/pybravo-text2wetlab-split-baseline \
  --dataset-root /tmp/Text2WetLab-eval \
  --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
  --output-dir /tmp/pybravo-text2wetlab-split-recheck
~~~

For a task with a supplied paper, add --evidence-planning to inspect the typed plan and its
audit before code generation. For E. coli, also pass --paper-override
ecoli-heat-shock-transformation=/path/to/pinned-paper.txt; the runner rejects a digest
mismatch. The RNA runner supplies its pinned custom labware to both generation and
simulation, and marks separate lint not_applicable rather than treating the missing lint
file as a pass.

With pyBravo running locally, inspect one already saved Designer draft's review issues
without creating a new protocol or calling Qwen:

~~~sh
.venv/bin/python scripts/repair_text2wetlab_designer.py \
  --task golden-gate-assembly \
  --api-url http://127.0.0.1:8000 \
  --output-dir /tmp/pybravo-text2wetlab-designer-audit
~~~

--run invokes local Qwen for bounded graph repair and saves a **new unreviewed draft** only
when the repair passes its checks; it never approves a workflow. Routine regression checks
are:

~~~sh
.venv/bin/pytest -q tests/test_text2wetlab_runner.py \
  tests/test_text2wetlab_planning.py tests/test_text2wetlab_rubric_audit.py \
  tests/test_text2wetlab_designer_generator.py tests/test_scientific_patterns.py
~~~

An official 100% claim still needs seven accepted protocols run through the pinned Harbor
verifier and 35/35 judged rubric items. A separate Bravo claim needs seven scientifically
reviewed Designer graphs that validate and strictly simulate against a confirmed Bravo
profile, inventory, and liquid methods. Neither milestone has been reached.
