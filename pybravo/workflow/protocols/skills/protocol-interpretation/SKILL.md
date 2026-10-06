---
name: protocol-interpretation
description: Extract a cited, reviewable scientific protocol intent before Bravo setup and compilation.
---

Read only the scientist's selected passages as source evidence. Preserve the order, per-channel quantities, well mappings, repetitions, plate identities, and manual handoffs. Cite the passage behind each step and quantity. A statement such as “transfer” is scientific intent: the compiler will later create distinct aspirate and dispense nodes.

Ask for an experimental fact when the source leaves it open: which source maps to which quadrant, whether destinations start empty, source-well starting and dead volumes, or whether a reagent belongs to a known family. If the scientist explicitly says a destination starts empty, record 0 µL initial volume; never use zero for an unknown volume. A receive-only destination needs no aspiration dead volume. A full 384-well source sent as 5 µL per well to each of two destinations needs at least 10 µL usable volume per source well, plus dead volume and any method-required overage. Keep unresolved facts null or as questions. A catalog entry may support a planning proposal, but it does not prove physical inventory or qualify a method. Return only the typed protocol plan; never produce robot packets, arbitrary code, approval claims, or motion settings.
