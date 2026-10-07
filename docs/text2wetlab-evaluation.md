# Text2WetLab evaluation boundary

The [Text2WetLab dataset](https://huggingface.co/datasets/EvanOLeary/Text2WetLab) revision
`d7c8a9b93428997447eeaf2ee9e27ac3ce026872` contains seven public Harbor tasks. Every
Harbor task asks for an **Opentrons OT-2 Python API v2** file at `/app/protocol.py`, with a
specified OT-2 deck. The graders apply anti-hack checks (and, in six tasks, a separate
static lint), then Opentrons 7.5.0 simulation, then a five-item Sonnet 5.5 rubric judge
using the simulator run log. The verifier configuration requires `ANTHROPIC_API_KEY`. A simulator pass is
necessary but is **not an official Text2WetLab score**.

The dataset also has a small [six-check PyLabRobot gym](https://huggingface.co/datasets/EvanOLeary/Text2WetLab/blob/main/eval/wetlab_gym.py)
for tip pickup, aspiration and dispensing, tip drop, and volume/capacity accounting.
Its [L1 example](https://huggingface.co/datasets/EvanOLeary/Text2WetLab/blob/main/tasks/L1/serial-dilution-200ul/assumptions.md)
explicitly records that the two destination wells and 100/100 µL split were assumptions
not specified by its one-sentence input. These checks are separate from the seven Harbor
task rubrics. This revision exposes no distinct public train/test split; the Harbor task
instructions and grader files are present in the dataset repository.

The seven Harbor tasks are two reservoir-to-plate transfers, AMPure cleanup, colony PCR,
heat-shock transformation, Golden Gate assembly, and RNA extraction. Several require
OT-2 thermocycler, magnetic, or temperature modules, plus paper-specific steps. An OT-2
benchmark file must never be treated as a Bravo executable workflow or as evidence that
the Bravo has those modules. pyBravo's benchmark adapter is confined to
`pybravo/evals/text2wetlab/`; Bravo execution still requires its own catalog checks,
validation, strict simulation, and scientist review.

| Task | Latest local outcome | Scientific or scoring limitation |
| --- | --- | --- |
| A1–A12 100 µL; split 200 µL | Pinned lint, simulator, runlog, and event-safety gates pass | Official rubric judge not run. |
| AMPure cleanup | Same local gates pass; 96 matched wells and source-isolated tips observed | Task-specific comments record non-pipetting stages; generic judge wording creates uncertainty about comment-only actions. |
| *E. coli* heat shock | Pinned lint/simulator/runlog pass; new process-order gate rejects | Candidate leaves block at 42 °C while eight SOC additions occur after the 30-second hold. |
| Colony PCR | Rejected before an accepted simulation | Candidate reaction volume and 2× dilution were inconsistent; loaded deck has no separate water source. |
| Golden Gate assembly | Rejected by static/simulator checks | Candidate assigns 4 µL to P300 and has tip/mix issues. |
| RNA extraction | Rejected by simulator | Exact geometry fixed mapping, but candidate exhausts P1000 tips and has invalid multichannel locations. |

**No task has an official Harbor rubric score from this local run.**

| Remaining complex task | Paper supplied by dataset? | OT-2 hardware or method dependency | Main rubric risks | Bravo treatment |
| --- | --- | --- | --- | --- |
| Colony PCR screening | Yes, `paper.txt` | 96 paired colonies/primers; thermocycling recorded outside pipetting simulation | Matching all 96 wells, 2× mix dilution, fresh tips, cycle program | Pipetting may be planned; sealing and thermocycling need an external handoff and review. |
| *E. coli* heat-shock transformation | No; build fetches pinned bioRxiv JATS | On-deck thermocycler block at 4, 42, then 37 °C | Eight plasmid matches, 1 µL DNA, exact heat-shock and recovery times, fresh tips | The Bravo has no compiled thermocycler primitive; require external equipment and an explicit handoff. This task cannot run as the specified OT-2 deck on Bravo. |
| Golden Gate assembly | Yes, `paper.txt` | Seven PCRs, DpnI cleanup, four assemblies, cycling, transformation, plating; eight OT-2 labware slots plus tip racks | Fragment/design mapping, calculated reagent volumes, ordered manual stages, tip isolation | Multiple external handoffs and a new Bravo deck plan are needed; the OT-2 slot map is not portable. |
| RNA extraction | Yes, `paper.txt` and custom elution plate geometry | Magnetic and temperature modules, 48 samples, 12 OT-2 slots, 1000 µL and multichannel pipettes | Sample traceability, wash order and volumes, 4 °C elution, filter-tip use | External magnet/temperature handoffs, custom labware verification, and a separate Bravo deck/tip-capacity plan are required. |

The RNA task's grader differs from the other six: its five rubric items are embedded
in `tests/grade.py`, and it does not ship `tests/protocol_lint.py`. Its published
anti-hack and simulator gates still apply. This difference must be represented in a
runner rather than reported as a missing or failed lint file. The runner marks its
separate lint as `not_applicable` and passes its pinned custom elution-plate JSON to
both the adapter and independent simulator/runlog via the labware directory.

## Local, reproducible checks

Create an isolated Python 3.10 environment matching the dataset's Dockerfile:

```sh
python3.10 -m venv /tmp/pybravo-text2wetlab-ot2-py310
/tmp/pybravo-text2wetlab-ot2-py310/bin/pip install \
  'opentrons==7.5.0' 'opentrons-shared-data==7.5.0' 'pydantic<2'
/tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate --version
```

Run the pinned task instructions through the local adapter and then independently
through the OT-2 simulator. The runner requires the adapter's explicit static-safety
and structured-event confirmations and applies each available task's pinned
`protocol_lint.py` before its independent simulator run. It downloads only selected
benchmark instructions, source papers, event loggers, lint, and custom labware,
writes output outside the repository, and records SHA-256 hashes plus the simulator
result. It never reports an official score:

```sh
.venv/bin/python scripts/evaluate_text2wetlab.py \
  --task a1-a12-100ul --task split-200ul-two-wells \
  --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
  --output-dir /tmp/pybravo-text2wetlab-baseline
```

The heat-shock task's build fetches a pinned bioRxiv paper that is absent from the
dataset checkout. If that text has been recovered by the dataset's fetch script,
pass it without copying it into the result directory:

```sh
.venv/bin/python scripts/evaluate_text2wetlab.py \
  --task ecoli-heat-shock-transformation \
  --paper-override ecoli-heat-shock-transformation=/private/tmp/pybravo-text2wetlab-ecoli-paper.txt \
  --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
  --output-dir /tmp/pybravo-text2wetlab-ecoli
```

The runner checks SHA-256 `768a5703630e593a3eb5be8cac6267af0cf26ba1b43dbc797d24b08cbaf3f18a`
before passing that paper to the model. A missing or mismatched paper blocks the
task rather than allowing the model to guess the method. The runner defaults to two
repair attempts after the first draft; `--repair-attempts 5` allows a bounded longer
run for a complex case without changing the baseline setting.
After a draft passes static validation and Opentrons simulation but fails a
structured event-safety check, the adapter now attempts **one focused Qwen line
patch** by default (`--patch-attempts 0` disables it; the maximum is three).
It keeps the rejected candidate and saves each model patch and patched source
in the trace directory. A patch can pass only when the original liquid-action
sequence, deck/labware map, and timed module commands are unchanged, and when
static validation, simulation, and event safety pass again. If the patch fails,
the ordinary bounded full-draft repair loop remains available. This is a
process-error repair strategy; it does not infer missing chemistry or make an
invalid liquid volume valid by changing task facts.
The adapter now verifies OT-2 GEN2 P20, P300, and P1000 working ranges from
[Opentrons' pipette table](https://docs.opentrons.com/python-api/pipettes/loading/),
effective loaded-tip capacity for literal strokes, and tip/held-volume state
from the structured runlog. For an instruction requiring on-deck thermocycler
heat shock followed by recovery, it also rejects liquid handling while the
block remains at the timed heat-shock temperature. Its separate
reaction-accounting helper can check
final volume and stock dilution when a caller supplies a structured recipe;
it does not infer missing reagent identities from prose or certify a method.

For future runs, the adapter passes a verbatim Methods section when a complete
Methods-to-Results span is present and shorter than 25,000 characters; otherwise it
uses the full paper. The generation trace records the original and excerpt SHA-256
digests, selection strategy, and line span. On the three longer supplied papers,
the selected text is 8,186 of 40,749 characters for colony PCR, 6,313 of 39,555
for Golden Gate, and 8,540 of 28,906 for RNA extraction. This reduces irrelevant
input and latency, but a rubric or scientist review must still check whether a
required detail appeared outside the selected Methods span.

The local Python 3.10 installation returned `opentrons_simulate 7.5.0`, and the
dataset's own `split-200ul-two-wells/tests/reference_protocol.py` simulated with exit
code 0. pyBravo's separate synthetic engineering suite passed 18/18 cases with:

```sh
.venv/bin/python -m pybravo.workflow.protocols.evaluate \
  --suite examples/protocols/benchmark.json
```

On this pinned revision, local Qwen generated both selected simple tasks. The
`a1-a12-100ul` case passed simulation after one repair; `split-200ul-two-wells`
passed on its first attempt. Both outputs had zero violations under the dataset's
`protocol_lint.py` and passed an independent Opentrons 7.5.0 simulation: **2/2 on
these two local gates**. The separate 18/18 pyBravo result is not a Text2WetLab model
score. A full official grade still needs the dataset's Harbor verifier, including
anti-hack checks and rubric judge, on each generated `/app/protocol.py` file.

The initial `ampure-bead-cleanup` candidate illustrates why simulator success alone
cannot be counted as task success. Its official lint and runlog gates passed, and the
nominal volumes and stage order appeared, but the runlog showed six tip-use cycles
that each aspirated from **all 96 sample wells** before dropping the tip. That carries
material between samples and fails the benchmark's contamination requirement. The
independent runner records this as a cross-well aspiration risk for review. The
first-pass evidence is in `/tmp/pybravo-text2wetlab-ampure-baseline/report.json`; it is
not an official score.

After the adapter gained a structured event gate, the first AMPure rerun was
**rejected**: three local-model candidates failed simulation with a `TipAttachedError`.
The revised repair prompt then generated an accepted candidate in two attempts (42.3
and 46.4 seconds). Its pinned lint had zero violations; Opentrons 7.5.0 simulation and
the pinned runlog passed; 6,912 action events included 768 tip pickups and drops with
no cross-sample carryover detected. The task explicitly permits unlimited virtual
tips through `reset_tipracks()`, which the candidate uses. Physical execution would
require rack restocking and a reviewed manual handoff at each reset. This is **3/3
selected tasks passing the local lint/simulator/runlog/event gates**, not an official
rubric score. The AMPure evidence is in `/tmp/pybravo-text2wetlab-ampure-repair2/report.json`;
the earlier rejected trace is in
`/tmp/pybravo-text2wetlab-ampure-gated/ampure-bead-cleanup/generation_trace.json`.
The accepted simple-task reports are at
`/tmp/pybravo-text2wetlab-baseline/report.json` and
`/tmp/pybravo-text2wetlab-split-baseline/report.json`.

The first `colony-pcr-screening` run failed safely after three AST-valid candidates:
all used integer indexing such as `pcr_plate[i]`, which Opentrons rejected with
`KeyError: 0`. No output passed simulation or the event gate. Its trace is
`/tmp/pybravo-text2wetlab-colony/colony-pcr-screening/generation_trace.json`.
The next two runs fixed integer indexing but exposed a scientific conflict in the
fixed deck: Q5 is a **2× stock**, each colony needs its own primers, and no separate
water source is listed. One candidate chose 5 µL Q5 + 1 µL primers + 1 µL template,
which yields only 7 µL and 1.43× Q5; another chose 9 + 1 + 1 µL while claiming a
10 µL reaction and used the P300 below its stated 20 µL minimum. The five-repair
run also exhausted P20 tips and repeated the same failed candidate four times.
Neither run produced a scientifically valid or simulator-accepted protocol.
Traces are in `/tmp/pybravo-text2wetlab-colony-repair2/` and
`/tmp/pybravo-text2wetlab-colony-repair3/`.

The first `ecoli-heat-shock-transformation` run used the checksum-matched pinned
paper but also failed safely after three AST-valid candidates. It initially tried
to set the thermocycler **lid** to 4 °C, outside its allowed 37–110 °C range;
subsequent repairs called nonexistent `wait_for_block_temperature()` or
`wait_for_block()` methods. The block itself supports 4 °C and its
`set_block_temperature()` command waits for the target. No output passed
simulation. Evidence is in `/tmp/pybravo-text2wetlab-ecoli/report.json`. After
adding the verified Opentrons 7.5 API limits to the adapter, a second run passed
on attempt 2. Pinned lint had zero violations; simulator, pinned runlog, and
structured event gate passed. Its 72 events show eight matched 1 µL plasmid
transfers, eight 50 µL SOC additions, and thermocycler holds at 4 °C/30 min,
42 °C/30 s, and 37 °C/60 min. The lid opens for pipetting and closes for the
temperature holds. **Semantic review remains open:** the generated runlog puts the
4 °C command *after* the eight DNA additions, even though the fixed setup says the
cells are on a pre-chilled block and the rubric expects cold handling during DNA
addition. More concretely, it leaves the block at 42 °C while SOC is added
serially to all eight wells after the stated 30-second heat shock, then sets
37 °C. This local mechanical pass cannot be counted as a benchmark success.
The evidence is in `/tmp/pybravo-text2wetlab-ecoli-repair2/report.json`; the
official rubric judge was not run.
One focused rerun after temperature-order guidance passed its then-current
local lint, simulator, and event-accounting gates after three attempts. Its
first 4 °C block command now precedes all DNA additions (event 1). However,
the timed 42 °C pulse is event 36, SOC aspirations begin at event 39, and the
first 37 °C block command is event 71, after all SOC additions. An independent
check with the subsequently added heat-shock transition gate rejects event 39:
the block remains at the high heat-shock temperature during serial liquid
handling. The saved report predates that gate and therefore says local
`simulator_passed`; this is **not** a semantic or official benchmark pass.
Evidence is in `/tmp/pybravo-text2wetlab-ecoli-transition/report.json`.

A focused repair experiment then used that saved, simulated candidate as its
starting point. The local Qwen model returned a **line edit**, not a complete
replacement protocol: insert an untimed 37 °C block command immediately after
the 42 °C/30-second pulse, before opening the lid for SOC additions. The
request took 7.1 seconds and produced 62 tokens, compared with 23.4 seconds
and 1,120 tokens for the prior full-code regeneration attempt. A mechanical
patcher applied the edit to a copy and required the original deck/labware map,
liquid-action sequence, and timed holds to remain identical. Static validation,
Opentrons 7.5 simulation, the task's standalone lint (zero violations), pinned
runlog, and the current heat-shock event gate all passed. The runlog orders the
4 °C block command before DNA, the 42 °C pulse at event 37, the untimed 37 °C
transition at event 38, the first SOC aspiration at event 41, and the 37 °C/
60-minute recovery hold at event 73. This is a **local gate pass for this one
candidate**, not an official Harbor rubric grade or Bravo hardware release.
The model patch, candidate, trace, and independent verification are in
`/tmp/pybravo-text2wetlab-ecoli-patch/`.

The first automatic CLI smoke test revealed a validator defect: it classified
the required plasmid-source-to-cell-destination transfer as cross-sample tip
reuse because both plates were aspirated somewhere in the protocol. The event
gate now permits one specimen source to feed one reaction well with the same
tip, including a mix in that destination; it still rejects another specimen
well and shared-stock backflow. The already generated candidate was
independently revalidated with this correction, but the earlier report remains
a record of the old failed gate.

A fresh end-to-end CLI run with the corrected gate then passed. Qwen produced
three full drafts (20.8, 19.5, and 23.9 seconds); the third passed simulation
but failed the heat-shock transition check. One Qwen line patch took 2.1 seconds
and produced 43 tokens. The final source passed static validation, the pinned
standalone lint with zero violations, an independent Opentrons 7.5 simulation,
the pinned runlog, and the current event-safety gate (73 events). The original
candidate, Qwen patch JSON, patched candidate, and nested trace are retained in
`/tmp/pybravo-text2wetlab-ecoli-patch-integrated2/`. The report has
`official_score: null`: this is local gate coverage, not an official 100% score.

The bounded experiment can be reproduced without writing a protocol by hand:

```sh
.venv/bin/python scripts/experiment_text2wetlab_patch_repair.py \
  --source /tmp/pybravo-text2wetlab-ecoli-transition/ecoli-heat-shock-transformation/protocol.py \
  --instruction /tmp/pybravo-text2wetlab-ecoli-transition/ecoli-heat-shock-transformation/instruction.md \
  --paper /private/tmp/pybravo-text2wetlab-ecoli-paper.txt \
  --event-logger /tmp/pybravo-text2wetlab-ecoli-transition/ecoli-heat-shock-transformation/official_runlog.py \
  --simulator /tmp/pybravo-text2wetlab-ot2-py310/bin/opentrons_simulate \
  --output-dir /tmp/pybravo-text2wetlab-ecoli-patch \
  --max-attempts 2 --model-timeout 120
```

The same bounded line-edit path now also handles a candidate rejected by the
Opentrons simulator. Each local-Qwen patch is saved next to the rejected
candidate and must pass the static source gate before it is simulated again.
Because a simulator-rejected candidate has no trustworthy event trace to
compare, a source-level guard holds fixed the loaded hardware and deck
bindings, liquid operation and declared-volume sequence, direct liquid
location arguments and literal reagent-well bindings, loop structure,
incubation commands, manual pauses, and tip-contact motion settings. It may
add fresh-tip cycles but cannot remove existing pickups or drops, introduce a
tip return, or silently reset a rack. It permits an explicit mix target or a
correction to a computed well index, then still
requires the Opentrons simulator and structured event-safety gate. The guard
does not establish scientific correctness or hardware readiness. The saved-
candidate experiment accepts an initially failing static gate as well, since
the current checks are stricter than when some earlier drafts were produced.

The first `golden-gate-assembly` run produced one AST-valid candidate, then
repeated it twice. Opentrons rejected `p20.mix(5, 15)` because the liquid
location was omitted inside the assembly-well loop. The candidate also assigned
several 1–19 µL reagent strokes to the P300 despite the task's 20 µL lower
working limit, and reused one tip across PCR reagent stocks. It did include
the seven PCRs, DpnI, manual column cleanup, four assembly mappings, and
transformation stages, but no candidate passed simulation. Evidence is in
`/tmp/pybravo-text2wetlab-golden-gate/report.json`.
An optional reasoning-mode comparison (`PYBRAVO_DRAFTER_ENABLE_THINKING=true`,
five repair attempts allowed) did not produce a first candidate: its only Qwen
request reached the 300-second local timeout. The adapter made no cloud request,
and no code was simulated. Evidence is in
`/tmp/pybravo-text2wetlab-golden-gate-thinking/report.json`.
One non-thinking comparison with the verbatim Methods excerpt and one allowed
repair reduced the first prompt from 11,600 to 5,714 tokens, while its model
response took 104.9 seconds versus 110.2 seconds with the full paper; the
second response took 95.1 seconds. Both different candidates were rejected
before simulation because line 78 aspirated 4 µL with the P300, below the
task's stated 20 µL working minimum. Thus this experiment supports shorter
source context, but does not establish a substantial latency improvement or
a valid protocol. The trace records Methods lines 17–38 and SHA-256 hashes;
evidence is in `/tmp/pybravo-text2wetlab-golden-gate-compact/report.json`.

A bounded saved-candidate line-patch experiment used that first Golden Gate
draft with the current, stricter static checks. Three local-Qwen patch calls
took 6.52, 7.47, and 4.41 seconds. The first correctly supplied the omitted
PCR master-mix well to `p300.mix`, exposing a later 19 µL P300 aspirate below
its 20 µL minimum. The second proposed a broad replacement that changed a
liquid action/volume and was rejected by the task-fact guard. The third added
another mix target but left the 19 µL stroke. None reached Opentrons
simulation or structured events; this is a bounded failure, not a benchmark
pass. The original candidate, all patch JSON files, patched sources, and
diagnostics are retained in
`/private/tmp/pybravo-text2wetlab-golden-patch-sim/patch_trace.json`.

The first `opentrons-rna-extraction` run used the pinned custom elution-plate
definition and had no standalone lint to apply. Its first candidate failed
simulation with an out-of-range well-list index: it multiplied a zero-based
column index by 12 instead of the plate's eight rows. The next two candidates
were identical and skipped. It also used 250–300 µL strokes with 200 µL tips,
passed lists instead of single wells as liquid locations, and treated a 4×6
sample tube rack as an eight-channel source. None reached event validation.
Evidence is in `/tmp/pybravo-text2wetlab-rna/report.json`.
With exact installed/custom labware geometry supplied before generation, the
next candidate correctly mapped 48 source tubes (two 4×6 racks) to the six odd
columns of the 8×12 extraction and elution plates. It nevertheless exhausted
the only 96-tip P1000 rack during per-well reagent additions; Opentrons raised
`OutOfTipsError` at line 102. Two repair requests returned byte-identical code,
so no simulator retry or event validation was warranted. The candidate also
passed lists of wells as multichannel liquid locations, which still needs
correction. Its three model calls took 72.6, 69.7, and 81.1 seconds. Evidence
is in `/tmp/pybravo-text2wetlab-rna-repair2/report.json` and the retained
`candidate_attempt_*.py` files there. This rerun used the original full paper;
the Methods-section compaction described above was integrated afterward.
