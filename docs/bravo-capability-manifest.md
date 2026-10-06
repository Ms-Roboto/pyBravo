# Bravo Capability Manifest, version 0.3

The Bravo Capability Manifest (BCM) gives a protocol-planning model a finite,
typed set of choices for the **configured** pyBravo instrument. It describes
what an assistant can propose, which robot operations pyBravo can compile, and
which catalog resources may be selected. It is discovery data, not a command
interface or permission to operate hardware.

The normative JSON Schema is
[`schemas/bravo-capability-manifest-v0.3.schema.json`](../schemas/bravo-capability-manifest-v0.3.schema.json).
The [example](../schemas/examples/bravo-capability-manifest-v0.3.example.json)
shows a 384ST simulation profile. Versions 0.1 and 0.2 remain available in the
same directories for consumers of the earlier formats. The identifier
`schema_version: "0.3.0"`
describes this document's format; it is independent of the `ProtocolPlan`
schema version and the instrument's firmware or controller protocol.

`GET /api/protocols/capabilities` returns the active profile's read-only
manifest. Fetch it again after changing the head, profile, labware catalog,
tips, or liquid classes. Protocol Assistant sends a bounded subset of these
choices to the local model and checks returned action IDs against it; the
full endpoint is useful for external planners and catalog review.

`method_registry` points to `GET /api/protocols/methods` and typed
`POST /api/protocols/methods/lookup`. Its digest and status counts let a client
detect changes without copying method settings into the manifest. The lookup
endpoint is read-only: it ranks applicable methods and reports evidence and
mismatches; it cannot edit settings or authorize hardware execution. The
method record and its referenced liquid class hold numeric pipetting settings.
`catalog_summary` reports static catalog status and provenance, not physical
deck contents or method qualification. See [Bravo method knowledge](bravo-method-knowledge.md)
for the separation of catalogs, method records, skills, recipes, and inventory.

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
| `labware[].dead_volume_ul`, `labware[].dead_volume_status` | A plate's catalog starting estimate and whether it is `reviewed` or an unreviewed `placeholder`. The value does not establish the residual for a particular liquid, tip, or aspiration method; the protocol planner requests scientist confirmation before using a placeholder for a source. |
| `catalog_summary` | Counts by catalog status, with provenance of the configured stores. `identity_complete` for a liquid class means its machine, head and tip identifiers exist; it does not mean the class has been calibrated. |
| `method_registry` | Version, digest, status counts, and discovery URLs for the separate method library. No motion setpoints or asserted live inventory are embedded. |
| `setup_decision_rules` | Conditional, evidence-tagged recommendations for setup fields. These are planning suggestions, not completed scientist decisions. |
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

## Setup decision rules

`setup_decision_rules` gives an assistant a small rule language for the setup
fields that otherwise require automation-engineer judgment. Each rule names a
JSON-pointer `decision`, a conjunction of typed fact predicates in `when.all`,
and a `recommendation` or `null`. A predicate supports `eq`, `gte`, or `in`.
Consumers MUST establish every required fact from the current reviewed plan
and its source evidence before applying a rule. A missing or ambiguous fact is
**unknown**, not `false`. Rules are advisory even when their
`evidence_level` is `derived`; the validator and scientist still check the
result. In particular, an assistant MUST NOT turn a rule's `required_evidence`
text into a purported scientist answer.

| Evidence level | Meaning |
| --- | --- |
| `derived` | The suggested value follows deterministically from verified plan facts and configured geometry. This does not verify physical setup. |
| `heuristic` | An engineering option fits the stated facts but needs a contamination, inventory, or physical-layout review. |
| `scientist_input` | No value is recommended. The scientist must provide or approve a procedure-specific value. |

The plan-fact vocabulary is deliberately narrower than natural language:

| Fact | Meaning |
| --- | --- |
| `liquid_step_count` | Number of planned transfer or mix intents after expanding bounded repeats. |
| `full_head_footprint` | Cited all-wells instructions or a narrowly matched four-source quadrant plan support a full-head proposal on verified grids. A plate's well count or anchor alone is insufficient; free-text evidence remains a heuristic until the scientist confirms this exact plan. |
| `source_count` | Distinct source materials addressed by liquid steps, in first-use order. |
| `dedicated_tip_rack_per_source` | The plan has one distinct tip material per source, and each selected rack–tip pair is exact and catalog-compatible with the active head. This does not establish actual tip inventory. |
| `st10_384_dedicated_racks` | Every source has an exact, execution-ready 384 ST10 rack–tip catalog pair. |
| `destination_initially_empty` | Every destination well addressed by the proposed reuse pattern has a recorded initial volume of zero, and source footprints on each destination do not overlap. Unknown volumes or overlapping source footprints do not satisfy this fact. |
| `destination_footprints_disjoint` | Different sources address separate destination wells under the proposed full-head mapping. This does not establish that those wells start empty. |
| `no_recorded_destination_liquid` | The draft records no nonzero starting liquid in either destination. A missing volume is still unknown, not confirmation of emptiness. |
| `four_source_quadrant_recipe`, `four_source_five_ul_pairings` | Cited source text and the exact plan describe four 384 sources, two 1536 destinations, eight quadrant transfers, and two 5 µL transfers per source. |
| `same_source_reuse_prohibited` | A scientist decision or explicit instruction requires fresh tips between the two destinations. |
| `same_source_reuse_authorized` | A scientist decision bound to the current plan allows a dedicated set to dispense from one source into the planned destinations. Empty destination plates by themselves do not grant authorization. |
| `tip_strategy` | A saved choice or an earlier advisory proposal in the same resolver response. Cascaded proposals remain unconfirmed until the scientist reviews them. |
| `waste_material_present` | The plan contains an on-deck material with role `waste`; an unplaced catalog entry does not count. |
| `minimum_addressed_well_depth_mm` | Minimum positive catalog depth among all addressed plates, when all depths are known. Used only as an exclusive upper bound on pipetting height. |

For a full-head 384-to-1536 quadrant candidate, the rule recommends
`{subset_type: all_barrels, subset_config: back_left}`. The full head's 16×24
channel count comes from configured geometry; `row_count` and `column_count`
remain `null` in setup. A source phrase or model interpretation provides a
heuristic proposal; only a scientist decision bound to that plan supplies
derived scope evidence. If the intended footprint is partial, the model must
ask for the shape and corner rather than choose an arbitrary quadrant of the
head. The `tip_rack_order_by_source` rule uses a fixed
`source_ordered_verified_tip_rack_ids` resolver: a consumer may fill this only
when each source has an unambiguous link to one exact, distinct, catalog-
compatible tip material, then list those racks in source first-use order.
An explicit scientist mapping is derived evidence. A rack ID containing the
exact source ID is a heuristic proposal that requires confirmation. Material
list order and fuzzy name similarity do not establish a link.

The source-dedicated tip rule suggests `fresh_each_source` when the
scientist explicitly permits same-source reuse, each source has its own
compatible rack, and addressed destinations are recorded empty. A second,
narrow rule offers the same strategy as a **conditional draft** for the cited
four-source 384-to-two-1536 ST10 pattern, even when destination starting
volumes have not yet been recorded. It requires the scientist to confirm that
both destinations start empty and that the dispense method avoids unacceptable
carryover. Two 5 µL dispenses fill the nominal 10 µL tip capacity, so this
proposal retains two separate aspirations unless a reviewed method establishes
enough effective capacity for a shared aspiration. Neither rule authors
`tip_reuse_reason`; the contamination assessment remains open. The resolver
may use a proposed strategy to offer a rack order and disposal choice in the
same response, but it does not save any of them. If the scientist explicitly
rejects reuse,
`fresh_each_step` is a possible strategy subject to enough fresh tips. With
source-dedicated racks and no waste material, the return rule may suggest the
literal `return_to_source_rack`, provided used tips return to their original
wells and are never reselected. A waste receptacle instead requires its actual
on-deck material ID. These conditions are not claims that racks are stocked or
that waste capacity is sufficient.

Pipetting height is never guessed from plate depth. Its rule publishes the
only generic bound supported by the catalog and validator:
`0 <= distance_from_bottom_mm < minimum_addressed_well_depth_mm` when all
addressed well depths are known. The approved height, liquid class, and reuse
assessment remain scientist inputs because they depend on the reagent,
geometry, tip, volumes, and qualified method.

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

An **assistant operation** is a scientific planning intent. `transfer` means
one source-to-destination movement and lowers to a distinct `liquid/Aspirate`
followed by `liquid/Dispense` at the same volume. `distribute` addresses one
source and an ordered list of destination/volume pairs. It lowers to one
aspiration followed by ordered, distinct dispenses only when the selected
method, effective tip capacity and calibrated command volumes permit it.
Otherwise the compiler uses separate aspiration/dispense pairs when safe, or
reports a validation error. A consumer MUST NOT
assume that `lowers_to` is a fixed node count: it lists node *types*. The
reviewed DAG shows every actual aspiration and dispense.

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
   reject stale IDs, incompatible rack-tip-head-plate combinations, stale
   method versions, and unresolved setup. Simulation and scientist approval
   are separate gates.
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
