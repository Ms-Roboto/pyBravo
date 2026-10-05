# Protocol Assistant

Protocol Assistant turns written laboratory procedures into reviewed pyBravo
workflows. Open **Protocol Assistant** from the control panel or designer, or
visit `/protocol-assistant`.

The workflow is **source → interpretation → questions → setup → validation →
strict simulation → approval → designer**. The local model proposes structured
data. A deterministic compiler supplies the executable operations. Generated
Python is not accepted in this path.

## Start with the local model

Install the optional `llm` dependencies with `pip install -e '.[llm]'`, or run
the launcher with `PYBRAVO_EXTRAS=llm`. Protocol Assistant defaults to
`http://sparky.local:8000/v1`, model alias `qwen`. No cloud key is required and
there is no automatic cloud fallback.

Use `PYBRAVO_DRAFTER_BASE_URL` and `PYBRAVO_DRAFTER_MODEL` to select a different
local compatible endpoint. The server must support chat completions with JSON
schema responses. Configuration is shared with the optional legacy drafter;
setting `PYBRAVO_DRAFTER_PROVIDER=local` explicitly selects local generation
for its endpoints too. See [configuration](configuration.md).

## Build a graph through chat

In the workflow designer, open **Protocol chat** and describe the procedure in
ordinary language. The panel keeps the conversation and asks for missing
scientific details. Each successful message updates the structured plan and
draws its ordered steps as a DAG on a separate designer tab. Follow-up messages
can revise the plan; the original messages remain available as source evidence.

The chat DAG is a structural draft. Its review nodes show operations, citations
and unresolved values; they are not robot tasks. The designer blocks simulation
and execution of this graph. Open its linked Protocol Assistant session to
finish the physical setup, validate and strictly simulate the plan, record
scientist approval, and export the compiled workflow back to the designer.
That export contains the executable nodes and the reviewed deck configuration.

## Prepare a protocol

1. Paste a procedure, or upload a PDF. Selectable-text PDFs work locally;
   scanned PDFs require a configured Docling service. Review the extracted
   passages, page references, tables and reading order. Select the procedure
   you intend to automate when a document contains several experiments.
   Pasted input is limited to 200,000 characters; PDFs to 20 MiB, 300 pages
   and 400,000 extracted characters. If Docling fails, selectable-text
   extraction is attempted and the source records a warning.
2. Select the instrument's profile in the control panel. The assistant reads
   its head, calibrated geometry, tip definitions, liquid classes and labware
   catalog. Choose a saved setup or assign the deck explicitly.
3. Extract the plan. Volumes and times retain their source units and citations.
   Unknown values stay blank. Transfers stay transfers even when their plates
   or deck positions have not been chosen. External operations such as
   centrifugation remain manual checkpoints.
4. Review the materials and ordered steps. Supply missing wells, quantities,
   tip strategy, liquid class and deck assignments. Enter starting and dead
   volumes **per well**; use well overrides for uneven starting supplies.
   A transfer's volume is per active channel. The run sheet reports total
   reagent consumption across those channels.
5. Record a reason when changing a source value or supplying an experimental
   decision. Source evidence retains the original quantity: a two-minute wait
   has `2 min` evidence and a normalized duration of 120 seconds. The software
   checks numerical agreement, while the scientist checks that the cited
   passage refers to the intended operation and that the experiment is complete.

Material rows describe physical containers, not a separate deck position for
each reagent name. The current compiler supports disposable pipetting heads,
transfers, mixing, elapsed-time waits, manual checkpoints, gripper plate moves,
and bounded nested repeats. Head footprints must be physically reachable.
Unsupported operations must remain explicit manual handoffs.

Tip supply racks and disposal containers are separate materials. A supply can
be full or list exactly which tips remain. An empty tip box used for disposal
must be marked explicitly empty. Reusing tips requires a written justification.
Manual handoffs eject tips before pausing; return any manually handled labware
to its declared location before confirming completion.

## Validate, simulate and approve

**Validate** checks source identifiers and numeric evidence, missing answers,
deck occupancy, head footprints, tip availability and capacity, liquid-class
compatibility, per-well source/dead volumes, destination capacities and bounded
operation counts. Errors remain visible alongside the editable plan.

**Strict simulation** compiles the accepted plan and runs the actual task logic
against a fresh simulation controller using the reviewed profile and catalog.
Hardware accessories are disabled. Every task error fails the result. Manual
steps are logged as simulated checkpoints and elapsed waits are skipped, so a
passing result does not prove that a physical handoff was completed or that a
timed experiment is biologically valid.
Strict simulation has a 180-second execution timeout; a timed-out run fails
and must be run again after its cause is resolved.

This differs from the designer's visual **Simulate** preview, which is intended
for animation and can continue after task warnings. The assistant requires its
own successful strict result before approval.

Review the run sheet, deck diagram and manual interventions. Record the
scientist's name and select either a **supervised water/dye qualification run**
or a previously qualified procedure with its reference in the notes. The
software does not attest that hardware qualification has happened merely
because a box was checked. Qualified staff must perform and assess that work.

Approval binds the source, selected passages, plan, setup, profile, catalogs,
tip-offset calibration and compiled workflow. Changing any of these invalidates
the corresponding result. Export opens the approved workflow in the designer.
Canvas arrangement and saving preserve its executable meaning. Editing steps
requires a new reviewed release. The execution endpoint checks the release
**before initialization or motion** and requires an empty pipetting head.
It checks approval again after initialization and rejects an instrument change
during startup. Only one designer workflow can start or run at a time; an abort
retains that ownership until the workflow has finished stopping.

Physical execution remains an explicit action in the designer. A manual
checkpoint requires the operator to type `completed`; it cannot be ignored.
Read [safety](safety.md) and use the instrument's emergency stop when necessary.

## Reuse and traceability

Save a setup to reuse head selections, tip strategy and deck material
assignments. Publish an approved procedure to the protocol library. Reusing a
library entry creates a new session, retains its source and decisions, and
requires validation, simulation and approval for the new run.

Sessions retain their revision history, original extracted plan, scientist
corrections, local-model settings and completion metadata, source passages,
uploaded source PDF, simulation events, approval and execution outcomes.
The browser remembers the last session and its URL contains a resumable session
identifier. Records live under `~/.pybravo/protocols` unless
`PYBRAVO_PROTOCOL_STORE` selects another directory. Back up this directory
together with the ordinary workflow directory.

These are local provenance records, not authenticated electronic signatures.
The surrounding pyBravo service has the same access assumptions as its existing
control API. Restrict access to the laboratory's trusted control network.

## Catalog readiness and limits

Real labware must have measured geometry, well capacity, an appropriate kind
and a compatible tip definition. The bundled snapshot contains incomplete tip
entries; their names alone do not establish pitch, height or usable capacity.
Correct these through the labware/tip editors and configure the machine's
liquid classes before attempting a liquid workflow. Missing data blocks release.
Never copy the synthetic benchmark's dimensions into a real instrument setup.

Simulation checks the software model; it cannot verify physical deck placement,
calibration accuracy, liquid properties, contamination tolerance or scientific
equivalence. Device-specific liquid-handling qualification remains necessary.

## Evaluate changes

The [synthetic benchmark](../examples/protocols/README.md) includes 18 cases
covering transfers, dilution, mixing, repeats, manual handoffs, unit conversion
and deliberately invalid or underspecified plans. Run:

```sh
python -m pybravo.workflow.protocols.evaluate
python -m pybravo.workflow.protocols.evaluate --extract --limit 4 --output /tmp/protocol-evaluation.json
```

The second command additionally measures the configured local model. Replace or
extend these examples with 10–20 scientist-reviewed laboratory protocols before
assessing laboratory productivity. Track parameter fidelity, omitted steps,
questions, strict-simulation success and actual review time. Stored corrections
provide examples for later prompt improvements; no automatic fine-tuning occurs.

## API

All routes below use the `/api/protocols` prefix. Drafting, validation and strict
simulation never execute physical hardware.

| Method and suffix | Result |
|---|---|
| `GET /context` | Active machine and authoritative catalogs |
| `POST /chat` | Add a scientist message and return a cited draft graph |
| `POST /from-text` | New session from `{text, name}` |
| `POST /ingest` | New session from multipart PDF `file` |
| `GET` at the base path | Session summaries (`GET /api/protocols`) |
| `GET /{id}` | Session and current simulation status |
| `GET /{id}/chat-preview` | Resume the conversation and its draft graph |
| `GET /{id}/source-pdf` | Stored original PDF, when present |
| `PATCH /{id}` | Edit `plan`, `setup`, selected passages with expected `revision` |
| `POST /{id}/extract` | Local-model extraction with bounded repair |
| `POST /{id}/validate` | Deterministic checks and run sheet |
| `POST /{id}/simulate` | Start strict simulation; poll `GET /{id}` |
| `POST /{id}/approve` | Record review, qualification intent and approval |
| `POST /{id}/export-workflow` | Release to designer; return workflow ID and URL |
| `POST /{id}/publish` | Publish an approved named library entry |
| `GET /library` | Approved library entries |
| `POST /library/{id}/reuse` | New unapproved session from an entry |
| `GET /setups`, `POST /setups` | List/save reusable experiment setups |

HTTP 409 indicates a stale revision, unresolved checks, or missing approval.
HTTP 422 indicates invalid input or a model response that could not be repaired.
The interactive `/docs` page contains request schemas.
