# Text2WetLab Designer draft audit

The seven saved Text2WetLab adaptations are local-Qwen-authored **native Designer graphs**. They open with `/designer?workflow=<id>` and use ordinary LiteGraph primitive node types. All seven returned HTTP 200 from the local `/api/workflows/{id}` endpoint on 2026-10-07 with their draft marker and node graph intact. That proves the diagrams can be inspected; it does not prove that their nodes form a valid, simulatable Bravo procedure. Designer now permits software rehearsal of native generated drafts on an isolated simulator, with catalog preflight and a stop on any task error. Hardware Execute remains blocked while `protocol_generated_draft` is true, and rehearsal does not qualify or approve a method. This marker must not be removed to work around validation.

This audit inspected the saved workflows under `~/.pybravo/workflows` against the active `/api/protocols/context` on 2026-10-07. The profile was `04-91-62-CF-7B-B0 / HT_384_D_70`. It offered one execution-ready rack/tip pair: 384-position `lw-4914769d0af7` with independent `st_10ul` tips. Five active classes used `st_10ul`. Two `st_30ul` classes existed, but the context did not offer an execution-ready matching rack. Catalog availability is still separate from confirmation of physical loading or liquid-method qualification.

For unresolved drafts, Designer also offers **Walk through draft** through
`POST /api/workflows/{id}/walkthrough`. This separate, nonexecuting viewer
replays connected task order and retracted head hovers over taught deck
locations. It does not pick tips, move plates, perform liquid strokes, or
validate collision geometry, liquid methods, or scientific completeness.
Every event and response reports `validation_passed=false` and
`qualification_granted=false`. Unknown class references remain visible, and
the strict `/simulate` and hardware `/execute` gates remain in force.

Designer now scopes its class dropdown to the active machine/head (or an
explicit persisted `protocol_simulation_target`). A proposed virtual target
must use catalog head/tip compatibility, capacity, and a known tip length;
it changes only a copied simulator profile. The installed instrument and
hardware class records remain unchanged. Saving retains prior draft findings
with a stale-validation marker instead of displaying a false zero-check result.

| Task | Saved graph | Main blockers for this active Bravo profile |
| --- | --- | --- |
| A1–A12 transfer | [7 nodes](http://127.0.0.1:8000/designer?workflow=10c25e05-4f13-4da6-be02-45f8c233e957), including a 12-iteration Loop | Fixed tip-cell pickup inside the Loop; source and destination labware absent from the proposed deck; 100 µL transfer exceeds the ready ST10 tip capacity; proposed class/rack unavailable. |
| Split 200 µL | [9 nodes](http://127.0.0.1:8000/designer?workflow=b7735e14-79f1-47b2-8560-5469422a0e64) | Two 100 µL liquid actions exceed the ready ST10 tip capacity; proposed 96-position rack and class are unavailable for this head. |
| AMPure cleanup | [42 nodes](http://127.0.0.1:8000/designer?workflow=e41685a4-f7b9-459b-92f2-46c6f45085bd), with separate aspirate/dispense primitives and manual magnet stages | No proposed deck entries at all; liquid locations and tip supply cannot resolve. Several 40–200 µL actions exceed the ready ST10 capacity, and the generic class is unavailable. |
| Colony PCR | [11 nodes](http://127.0.0.1:8000/designer?workflow=236647b7-704b-4b9c-a7cf-1a9339d211d1) | Full 384-channel head mode is proposed against 96-well labware; sample mapping and tip isolation are unproven; proposed rack/class unavailable. |
| E. coli heat shock | [13 nodes](http://127.0.0.1:8000/designer?workflow=c5c9055e-043d-4ad4-b467-9c72d0eca7a1), including external-stage handoffs | Saved review issues include head/plate footprint, missing executable well mapping, and unavailable rack/class. The independent re-audit needs the exact source paper supplied at generation time to verify its saved SHA-256; the default dataset source does not satisfy that digest. |
| Golden Gate | [26 nodes](http://127.0.0.1:8000/designer?workflow=dc69afcd-3825-4340-81ca-3ed769695e6d) | Liquid nodes use a descriptive `wells` string such as `A1:G1`; the Bravo executor reads executable `anchor` and head mode instead. Several transfers exceed ready ST10 capacity; the proposed rack/class are unavailable and sample mapping is unproven. |
| RNA extraction | [25 nodes](http://127.0.0.1:8000/designer?workflow=3530efd6-76b7-4204-94ce-52a9e0d35638) | Full 384-channel mode is aimed at 96-well labware; 48-sample coverage, tip isolation, and elution stage are unproven; proposed rack/classes unavailable. |

These are independent **Bravo** problems. The benchmark's required OT-2 labware, pipettes, modules, and deck slots do not establish matching Bravo consumables or positions. The current profile cannot complete the larger-volume tasks using its one ready ST10 pairing. Switching to or adding a verified head, rack, tip, and method is a hardware-catalog decision; silently splitting a required operation or renaming a method would change the scientific procedure.

## Generic path from a diagram to a usable workflow

1. Have the local model author an ordered, typed scientific action plan with source/destination wells, volumes, sample identities, tip-isolation groups, and external-stage handoffs. Store exact source citations and unresolved experimental facts. Its `wells` list is evidence of intent, not a command to the Bravo executor.
2. Resolve each action against an explicit machine profile and current deck inventory. Require an execution-ready head–tip–rack–liquid-class combination and labware geometry; compare every single stroke with effective capacity. Report unsupported actions instead of creating runnable-looking nodes.
3. Lower resolved actions deterministically to the existing `tips/TipsOn`, `liquid/Aspirate`, `liquid/Dispense`, `tips/TipsOff`, plate, wait, and manual primitives. Expand or parameterize repeated operations so the runtime receives valid `anchor` and head mode for each iteration and fresh-tip selection cannot repeat a spent cell. Preserve the action-to-node provenance.
4. Validate the resulting DAG's topology, per-iteration material and tip ledger, live deck transitions, method settings, and external handoffs. Run strict simulation against the same pinned profile and graph revision. Only a scientist-reviewed, simulation-passing revision should become executable.

To reproduce a **read-only** audit, run `.venv/bin/python scripts/repair_text2wetlab_designer.py --output-dir /tmp/pybravo-text2wetlab-designer-audit`. It writes a report under `/tmp` and never contacts Qwen unless `--run` is supplied. Pass `--ecoli-paper` with the exact original paper if auditing that draft's source provenance. The saved draft remains unchanged.
