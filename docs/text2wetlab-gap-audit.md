# Text2WetLab gap audit: scientific plans before code

This is an independent local audit of the seven pinned Harbor tasks at dataset revision
`d7c8a9b93428997447eeaf2ee9e27ac3ce026872`. It is **not an official score**. The
task instructions, supplied papers, and saved Qwen candidates are evidence; the public
grader rubrics are used only to inspect coverage, never as model-generation instructions.
No protocol answer is written here.

| Task | Current local observation | General gap |
| --- | --- | --- |
| A1–A12 transfer | Four observable checks supported; fidelity needs review. | Verify comments and absence of outcome-changing extra steps. |
| Split transfer | Four observable checks supported; fidelity needs review. | Preserve one 200 µL aspiration feeding two 100 µL dispenses. |
| AMPure cleanup | Core transfer volumes and 96-well mapping supported; manual stages and stock backflow need review. | Represent manual magnet/drying handoffs and per-tip liquid contact. |
| E. coli heat shock | Measurable transfer/module sequence supported; paper fidelity needs review. | Compare all executed stages and comments to the cited method. |
| Golden Gate | One local category supported, two failed, two need review. | Enforce reaction stoichiometry and an ordered, parameterized handoff timeline. |
| Colony PCR | A newer Qwen candidate passes the local simulator and pinned run log, but still needs scientific review. | Resolve the unspecified primer stock concentration and require a real operator handoff before each permitted tip-rack refill. |
| RNA extraction | No accepted simulator run. | Plan by pipette/tip capacity, eight-channel column geometry, and sample isolation before code. |

The saved Golden Gate protocol at
`/private/tmp/pybravo-text2wetlab-golden-planning/golden-gate-assembly/protocol.py`
dispenses 20 µL premix, 2.5 µL of each primer, and 1 µL template into each PCR well:
**26 µL total**. The supplied Methods specify 25 µL PCRs, 0.1 µM each primer, and
0.5 ng linearized template. The fixed task inventory lists 1 µM primers and
0.5 ng/µL template. From those cited values, a general concentration/volume solver
derives 2.5 µL of each primer, 1 µL template, and **19 µL remaining** for premix.
The model's PCR handoff also says only “run PCR program,” omitting the supplied 98 °C
initial denaturation, 34–36 cycles, and final extension. The local audit in
`pybravo/evals/text2wetlab/rubric_audit.py` catches the per-well volume and missing
program note but does not verify mixture composition or the chronology of all manual
handoffs. A newer direct Qwen candidate prepared the same master mix twice in the
same initially empty rack well; a reminder to emit only final code did not prevent it.

An earlier colony PCR candidate correctly stated that its P20 stages need 193 pickups
from one 96-tip rack, but reset only after the 96-primer loop. One earlier master-mix
tip meant the 97th pickup failed **inside** that loop. It also treated 3.5 µL of absent
water as a manual addition, leaving the robot with only 7 µL in a nominal 10 µL
reaction. The fixed inventory does not list water or a premixed 1× source. A newer Qwen
candidate instead simulates 5 µL of 2× Q5 mix, 4 µL of the matching primer-pair solution,
and 1 µL of colony template per well, with rack resets before tip exhaustion. Its own
comments acknowledge that primer concentration is unknown, so 4 µL cannot yet be judged
chemically suitable. It reports a refill by comment and calls reset_tipracks(), but does
not pause for the operator to load fresh tips. Simulation success does not resolve either
gap. The paper's 9 µL master mix plus 1 µL colony template is a method description; the
task's fixed stocks require a feasible, explicitly justified adaptation, never an
undocumented substitution or a false claim in comments.

The RNA candidate consumes its 96 P1000 tips on 48 bead and 48 isopropanol additions,
so it fails before the first sample pickup. It later passes lists of eight wells to
single-location multichannel calls, attempts 100 µL recovery from each 100 µL elution,
and uses P1000 strokes below the stated 100 µL minimum. The task has three 96-tip
P300 racks: 36 eight-channel pickups. Its 200 µL tips cap each P300 stroke at 200 µL.
A valid plan must distinguish shared-reagent, noncontact dispensing from tips that
have touched distinct samples; it should retain a tip set within one sample column
across split removal strokes when safe, and count every pickup by phase. The paper
states 100 µL elution buffer but no exact recovery yield, so the knowledge layer
must record an experimentally reviewable residual/recovery assumption rather than
pretend the paper supplied a number.

## Reusable implementation contract

1. Have the local model produce a **cited, typed stage plan** before executable code.
   The plan names each starting vessel, generated intermediate, per-well additions,
   source/destination mapping, manual handoff, module state, and tip policy. Keep
   benchmark rubrics outside the model prompt.
2. Deterministically solve each reaction's volume and stock-to-final concentration
   equations. Trace generated mixtures as material states, not just vessel names.
   Reject missing components, duplicate preparations, overfilled reactions, and
   comments that claim an unexecuted fix. Preserve unresolved quantities as review
   questions; do not manufacture a scientific fact.
3. Build an ordered event ledger from the simulated run: reagent-to-well transfers,
   actual mixing cycles, module transitions, delays, and manual handoffs. Compare
   the ledger to the cited plan, including **stage order and parameters**, rather
   than only searching comments for words. A manual pause is an asserted operator
   handoff for the OT-2 benchmark, not proof of physical completion on Bravo.
4. Preflight tip availability at each pickup, including loop boundaries and
   multichannel consumption. Include effective capacity from the installed tips,
   not only the pipette name. Permit refill/reset only with task authorization and
   a physical handoff at exhaustion. A simulator error then points to the precise
   resource-plan discrepancy instead of prompting a full regeneration.
5. Track per-tip contact provenance. Reusing a clean tip for a shared reagent is
   safe only when destination dispenses avoid contact; after sample contact, reject
   another specimen or shared-stock entry. Check physical well shapes and channel
   pitch before allowing multichannel moves.

The current typed plan (`pybravo/evals/text2wetlab/planning.py`) already has reactions,
sources, stages, tip budgets, and source quotes, but it does not compile ordered
per-well actions. The current audit (`rubric_audit.py`) checks many endpoint facts but
leaves mixture ancestry, manual chronology, and stock backflow to review. A generic
plan-to-action compiler plus these validators is the shortest path to reproducible
coverage. It must stay separate from Bravo execution: OT-2 task decks and modules
do not become physical Bravo capabilities merely because an OT-2 draft simulates.
