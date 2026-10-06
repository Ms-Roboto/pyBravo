# Bravo method knowledge

The [Bravo Capability Manifest](bravo-capability-manifest.md) is the public
discovery contract. It deliberately does not contain the automation engineer's
entire knowledge base. A planner should use five separate layers:

| Layer | Purpose | Source of truth |
| --- | --- | --- |
| Hardware catalogs | Head, independent tip and tip-box IDs, grid geometry, and teachpoint constraints | Active machine profile, labware and tip catalogs, tip offsets |
| Liquid classes | Machine/head/tip-specific aspiration, dispensing, Z entry/exit motion, and calibration | Versioned local liquid-class store |
| Methods | When a liquid class and technique apply: fluid family, volume and plate range, tip policy, heights, air gaps, blowout and other task choices | Versioned method registry with evidence and reviewer |
| Skills and recipes | How to interpret a procedure, retrieve evidence, plan a deck, and check contamination; recurring scientific patterns | Short, focused skill instructions and recipe templates |
| Runtime state | What is actually loaded, fresh, mounted and accessible | Current run setup and instrument observation; never the manifest |

An agent first extracts scientific intent with source references, asks method
lookup for applicable choices, and then gives the resolver and validator the
selected IDs. It must preserve unknown quantities. A skill may tell it *when*
to ask for a class or contamination decision, but cannot invent motion values.
Numeric speeds belong in the referenced liquid class; method records say when
to use it. A protocol revision pins the method and catalog digest used for
review. The compiler, strict simulation, and scientist approval remain
separate checks.

Volume accounting asks for a measured or scientist-confirmed starting volume
on each liquid plate. Dead volume is required only where the protocol
aspirates, including a plate later reused as a source or mixed in place. A
receive-only destination has no aspiration dead-volume parameter to guess.
For two 5 µL transfers from every source well, the known withdrawal is 10 µL
per well; starting volume must also cover that source's dead volume and any
reviewed method overage.

The public JSON Schemas are the
[method record](../schemas/bravo-method-record-v1.schema.json),
[typed lookup query](../schemas/bravo-method-query-v1.schema.json), and
[registry response](../schemas/bravo-method-registry-v1.schema.json). The
[lookup response](../schemas/bravo-method-lookup-v1.schema.json) covers ranked
candidates, issues and fallback notes. A
[registry example](../schemas/examples/bravo-method-registry-v1.example.json)
is generated from the simulation profile. The
[distribute lookup example](../schemas/examples/bravo-method-query-v1.example.json)
uses `volume_ul` for the requested total and `dispense_volumes_ul` for ordered
aliquots; its [lookup result](../schemas/examples/bravo-method-lookup-v1.example.json)
shows the corresponding candidate response. `GET /api/protocols/methods` is
read-only; `POST /api/protocols/methods/lookup` is a read-only search despite
using POST for its structured query. `POST /api/protocols/methods` is the
separate, version-checked curation action for a complete scientist-reviewed
record.

For `distribute`, the lookup requires positive ordered aliquots whose sum is
`volume_ul`. It checks each aliquot against tip, head and destination-well
limits. If the combined volume cannot fit one aspiration, the read-only result
sets `shared_aspiration_feasible: false`, adds `planning_notes`, and marks
candidate differences with `shared_aspiration_capacity` /
`paired_aspirations_required`. The compiler can then use separate
aspirate/dispense pairs when the selected method and contamination policy
allow them; a ranked candidate alone is never an execution grant.

## Evidence and release states

`reference_candidate` means a paper or vendor source suggests a technique;
it has not been mapped to a runnable local class. `imported_unverified` means
a local record exists but its provenance or physical qualification is
incomplete. `reviewed` means an automation scientist approved its stated
applicability. `qualified` additionally carries local performance evidence for
the specified machine, tip, liquid and volume range. A class identity or a
plausible catalog pair alone never establishes qualification.

The resolver rejects physical incompatibilities first, then ranks method
applicability. It reports each reagent, plate or volume mismatch rather than
silently broadening a method's scope. A scientist may accept a closest
compatible method for the current revision after reviewing the differences
and passing strict simulation; the adaptation remains labeled as an
adaptation, not a qualified method. New observations propose a separately
reviewed method version; they do not alter a previously approved protocol.
Each referenced liquid class carries a digest plus field-level origins for
stroke, delay and Z-motion parameters. If a source value was defaulted, a
scientist must explicitly attest it as `reviewed_default` or
`reviewed_inferred` during curation; the original source origin remains
visible. `source_field_origins` is derived provenance, not an editable
attestation.

## Source candidates for curation

The following sources were checked on 2026-10-05. They provide evidence to
curate, not executable settings for this instrument. Record the exact URL,
retrieval date, source type and a digest of the extracted facts in each method
record; keep the source's applicability and any local test result distinct.

| Source | Provenance and useful evidence | Initial status |
| --- | --- | --- |
| [Agilent, Creating a liquid class](https://automation.help.agilent.com/AutomationSolutionsKB14/VWorks%20Setup%20Guide/03_SpecPipetteSpeed.06.4.html) | Vendor setup guide defines separate aspirate/dispense stroke, delay and Z entry/exit settings. It notes published limits may not be achievable on every device. | `reference_candidate` |
| [Agilent, Aspirate task parameters](https://automation.help.agilent.com/AutomationSolutionsKB14/Bravo%20User%20Guide/QuickRef.11.11.html) and [Dispense task parameters](https://automation.help.agilent.com/AutomationSolutionsKB14/Bravo%20User%20Guide/QuickRef.11.12.html) | Vendor task references identify technique choices such as air volumes, height, dynamic tip movement, blowout and tip touch. | `reference_candidate` |
| [Agilent, Bravo automated liquid handling applications](https://www.agilent.com/en/product/automated-liquid-handling/automated-liquid-handling-applications/bravo-ngs) | Vendor application notes and protocols are candidates for recipe structure and stated consumables; each referenced application needs its own provenance record. | `reference_candidate` |
| [Reddi et al., 2026, DOI 10.1016/j.slast.2026.100442](https://pubmed.ncbi.nlm.nih.gov/42229725/) | Peer-reviewed Bravo 96-head study of low-volume photometric verification; its abstract reports different performance for tip and post-aspirate-air combinations. Useful for qualification-test design, not transferable speed settings. | `reference_candidate` |
| [Automated gravimetric calibration, 2016](https://pmc.ncbi.nlm.nih.gov/articles/PMC5030733/) | Peer-reviewed study on another liquid handler showing that fluid properties affect class selection and that calibration needs confirmation for each liquid. Methodology reference only. | `reference_candidate` |
| `examples/protocols/` | pyBravo synthetic graph and benchmark fixtures, including a four-source 384-to-1536 pattern. Useful as structural regression cases; not scientist-reviewed or hardware-qualified. | `imported_unverified` |

The runtime's `pipette_technique` name currently describes an orbiting movement.
Agilent's [VWorks pipette technique](https://automation.help.agilent.com/AutomationSolutionsKB14/VWorks%20User%20Guide/10_PipetteTechniques.13.3.html)
describes a well-position offset. Curators should retain these as distinct
technique types rather than copy settings between them by name.

## Growing coverage

Audit the configured stores across all eight disposable head types before
curating methods for a real instrument:

```sh
.venv/bin/python -m pybravo.workflow.protocols.catalog_audit --machine-id YOUR_MACHINE_ID
```

The read-only report flags missing rack-to-tip links, unknown tip lengths,
absent exact local liquid classes, and source class fields inferred or
defaulted by normalization. It does not claim qualification or physical tip
inventory.

Start with a reviewed corpus covering every disposable head, major reagent
families, source/destination plate geometries, and volume bands. Include
negative cases, such as ST70 against 1536-well plates or a tip rack that lacks
an exact tip link. Measure expert agreement on method choice, unresolved
questions, edits after recommendation, and simulation outcomes. Expand the
corpus from 30 reviewed cases toward at least 100 varied cases before treating
broad protocol coverage as demonstrated. Skills should remain few and focused;
new protocol families usually add a recipe or method record, not another copy
of the hardware catalog inside a prompt.

Summarize evidence already saved in Protocol Assistant without changing it:

```sh
.venv/bin/python -m pybravo.workflow.protocols.evaluation
.venv/bin/python -m pybravo.workflow.protocols.evaluation --store /path/to/protocol-store
```

The report distinguishes top-candidate selections, newly curated method
selections, and bulk method proposals, alongside edit revisions, unresolved
questions, validation errors, simulation outcomes and release bases. Expert agreement
remains unscored until an expert records a comparison; a passing simulation is
not an agreement or qualification result.
