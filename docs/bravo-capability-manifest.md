# Bravo Capability Manifest, version 0.1

The Bravo Capability Manifest (BCM) gives a protocol-planning model a finite,
typed set of choices for the **configured** pyBravo instrument. It describes
what an assistant can propose, which robot operations pyBravo can compile, and
which catalog resources may be selected. It is discovery data, not a command
interface or permission to operate hardware.

The normative JSON Schema is
[`schemas/bravo-capability-manifest-v0.1.schema.json`](../schemas/bravo-capability-manifest-v0.1.schema.json).
The [example](../schemas/examples/bravo-capability-manifest-v0.1.example.json)
shows a 384ST simulation profile. The identifier `schema_version: "0.1.0"`
describes this document's format; it is independent of the `ProtocolPlan`
schema version and the instrument's firmware or controller protocol.

`GET /api/protocols/capabilities` returns the active profile's read-only
manifest. Fetch it again after changing the head, profile, labware catalog,
tips, or liquid classes. Protocol Assistant sends a bounded subset of these
choices to the local model and checks returned action IDs against it; the
full endpoint is useful for external planners and catalog review.

## Model Hardware Standard relationship

[Anthropic's Model Hardware Standard (MHS)](https://www.anthropic.com/news/model-hardware-standard-research-preview)
is in a research preview, and its schema is not yet public. BCM is a pyBravo
format designed for Bravo protocol planning. It does not claim MHS conformance.
When an MHS schema becomes available, a separate adapter could map these
capabilities into it without changing pyBravo's hardware validation.

## Document model

The server derives a manifest from its active machine context. A conforming
producer MUST publish these top-level fields:

| Field | Meaning |
| --- | --- |
| `standard`, `schema_version` | Format identity and semantic version. |
| `context_hash` | Digest of the profile and catalogs used to make these choices. A plan MUST be checked against current context before compilation. |
| `machine` | Configured head ID, channel geometry, and whether a gripper is configured. A head's numeric model name is **not** a tip-selection rule. |
| `assistant_operations` | High-level, model-selectable `ProtocolStep.kind` choices with typed parameters, preconditions, effects, availability, and deterministic lowering targets. |
| `robot_operations` | Compiler-allowlisted graph node types. These are for explanation and auditing; their presence never makes them directly selectable by the Protocol Assistant. |
| `tipbox_choices` | Exact, catalog-backed rack **and** tip pairs for this head. The rack and loaded tip retain independent identities. |
| `tipbox_catalog_candidates` | Plausible incomplete rack records for catalog maintenance, never verified choices. |
| `tip_plate_compatibility` | Exact tip ID to catalog plate ID planning rules, including explicit incompatibilities. These rules do not qualify a run. |
| `labware`, `tip_definitions`, `liquid_classes` | Compact catalog entries with stable IDs and known values. A listed entry does not establish physical presence on the deck. |
| `constraints`, `review_requirements` | Stable rule IDs and human-readable explanations. The server enforces rules; the descriptions help a model ask useful questions. |

`setup_options` offers the protocol setup's tip strategies, disposal policies,
and head modes as explicit IDs. A `requires_reason` or
`requires_confirmation` flag is a prompt to collect a scientist's decision,
not a model decision. The `waste_container` disposal option has
`value_kind: "material_id"`: the chosen `tip_disposal_id` must be an actual
material with role `waste`, placed on the deck. The mode name itself is **not**
a valid `tip_disposal_id`. `return_to_source_rack` has
`value_kind: "literal"` and is the only literal disposal value in this
version; it requires the `fresh_each_source` strategy and confirmation that
spent tips will not be selected again. Head-mode options list the fields that
must be supplied and still require confirmation and exact rack–tip footprint
validation. `transfer_patterns` contains geometry-backed mapping
options. For a compatible 384-channel head and cataloged 384/1536 plates, it
lists source anchor A1 and destination anchors A1, A2, B1, B2. The `status`
value `geometry_option` means a shape that can be reviewed; it is not evidence
that the physical plates, teachpoints, or liquid class are suitable.

All dimensions and quantities carry unit-suffixed keys or an explicit `unit`.
Volume is in µL, position in mm, and duration in seconds unless a parameter
declares another unit. Unknown numeric values MUST remain absent or `null`;
zero is a measured value, not a substitute for unknown. A consumer MUST use
IDs for selection rather than matching names, prefixes, or capacity numbers.
It MUST ignore unknown namespaced extensions for planning and MUST NOT turn
unrecognized extension values into robot commands.

`context_hash` describes configuration, not momentary state. Tip inventory,
mounted tips, deck occupancy, connection status, and live axis positions can
change while a manifest is displayed. A producer MUST NOT use a cached
manifest as proof of those conditions. At compile time, pyBravo rechecks the
current profile, catalogs, setup, recorded inventory, proposed deck, and all
validation rules; the scientist confirms physical state before a run.

## Intent versus robot operation

An **assistant operation** is a scientific planning intent. In version 0.1,
`transfer` means one source-to-destination movement and lowers to a distinct
`liquid/Aspirate` followed by `liquid/Dispense` at the same volume. Thus two
5 µL transfers currently mean two 5 µL aspirations and two 5 µL dispenses.
The Protocol Assistant does **not** yet express one 10 µL aspiration followed
by two 5 µL dispenses, even though the Designer has separate robot nodes. The
manifest MUST NOT advertise that multidispense pattern as an assistant choice
until the plan schema, compiler, volume accounting, validation, and simulation
all support it.

`selectable: true` means the Protocol Assistant has a plan representation for
that intent. `availability` reports the active profile's coarse status:
`available`, `requires_setup`, or `unavailable`. Neither field means the
specific run is qualified. The server still checks the selected materials,
head mode, calibrated liquid class, confirmed volumes, tip inventory,
teachpoints, stack clearance, and instrument state. A configured machine with
no gripper, for example, can describe `move_plate` but MUST mark it unavailable
for automated planning. Unsupported scientific actions become explicit manual
steps; they do not disappear from the protocol.

`robot_operations` describes compiled graph node types and is not an LLM tool
list. Each robot operation has `model_selectable: false`, even when the graph
node is available to the deterministic compiler. A model MUST return a
reviewable protocol plan with operation IDs and
source evidence, never raw graph nodes, device packets, Python code, or a
claim that its own selections have been approved.

## Resource compatibility and qualification

A selectable tip combination MUST have an exact rack-to-tip catalog link,
head-compatible tip definition, and compatible rack geometry. For example,
the current catalog records both `st_10ul` and `st_70ul` for 384ST rack
`lw-4914769d0af7`. The rack name or its default tip does not choose which
tips are loaded. The head ID `HT_384_D_70` also does not require 70 µL tips.
`execution_ready` on a pair means its static metadata is complete enough to
offer as a candidate; it does not certify that a physical rack contains those
tips or that a particular liquid class and plate are qualified.

`tip_plate_compatibility` is a separate relation because tip-to-head and
rack-to-tip compatibility do not imply suitability for every plate. Each row
names a `tip_definition_id`, a contacted `target_labware_id`, its well count,
and the contact roles in `applies_to` (`source`, `destination`, or `mix`). A
`planning_compatible` row permits **drafting** only and always has
`execution_ready: false`; it does not establish a calibrated dispense,
physical clearance, alignment, or tip inventory. An `incompatible` row tells
the planner to exclude that exact tip/plate pairing, even when the tip fits
the head and rack. Missing rows mean **unknown**, never compatible by default.
The runtime validator remains authoritative if a catalog row or profile
changes.

Some catalog entries are intentionally provisional. The 96ST visual model
`lw-96st-provisional` has no approved tip links and MUST stay outside
`tipbox_choices`. It may appear in `tipbox_catalog_candidates` with an
explanation of missing evidence. A missing tip length, plate well capacity,
calibration, or source volume MUST create a review requirement rather than an
invented value. Plate compatibility can be more restrictive than head and rack
compatibility: Agilent's
[384ST 10 µL overview](https://www.agilent.com/cs/library/technicaloverviews/public/te-bravo-automated-liquid-handling-384st-10-ul-tip-5990-3645en-agilent.pdf)
supports ST10 tips with 1536-well plates and excludes ST70; the validator
enforces that distinction for the current workflow.

The manifest is a **starting dictionary**, not a substitute for the catalog
or a record of scientist approval. The scientist confirms physical deck items,
tip type and freshness, well volumes, mapping, liquid class, and relevant
clearances. Validation and strict simulation precede any executable export.

## Producer and consumer rules

1. A producer MUST generate choices from the active machine profile and
   authoritative catalogs; a model response cannot amend capabilities.
2. A producer MUST give each action and rule a stable ID. `preconditions` and
   `effects` refer to named rules or state concepts rather than executable
   expressions. Natural-language descriptions are explanatory, not the source
   of truth for validation.
3. A consumer MAY rank valid choices or propose values, but MUST preserve
   unknowns and cite the scientist's source text for quantitative steps.
4. The server MUST validate the proposed plan against current context and
   reject stale IDs, incompatible rack-tip-head-plate combinations, and
   unresolved setup. Simulation and scientist approval are separate gates.
5. A producer MUST NOT set `execution_ready: true` for provisional pairs or
   infer missing associations from a rack name. A consumer MUST NOT promote a
   catalog candidate into a selectable pair.
6. A change to field meaning or required structure increments the minor or
   major schema version. Additive optional fields increment the minor version;
   incompatible changes increment the major version. Consumers MUST reject an
   unsupported major version rather than guessing its semantics.

JSON Schema validates shape and basic types. Cross-field identities, catalog
membership, physical geometry, well accounting, and tip lifecycle are checked
by pyBravo's deterministic validator, not by JSON Schema or an LLM.

The checked-in example is a static snapshot from `profiles/simulation.yaml`.
It has no liquid class for machine ID `SIMULATED`, so its operations illustrate
planning choices rather than a runnable configuration. Regenerate it only
from a deliberately selected profile and validate it against the checked-in
schema; do not capture connected instrument state in the example.
