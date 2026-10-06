---
name: liquid-methods
description: Capture reagent and liquid-transfer intent for later deterministic method lookup.
---

Record a source material's stated reagent identity and family. Use `transfer` for one source-to-destination aliquot and `distribute` only when one source feeds several ordered destinations. Record every target volume. Tip reuse and shared aspiration are separate decisions: the same source-dedicated tip set may serve two initially empty destination wells under an approved contamination policy while aspirating separately for each. Two 5 µL dispenses total the nominal capacity of an ST10 tip; do not assume one 10 µL aspiration is feasible without a qualified method accounting for residual volume, air gap, and calibrated capacity. The validator may lower a distribute to paired aspirations when sharing is unsafe.

Leave `method_ref` null during extraction. After the draft identifies the source, destinations, tip, and rack, the application calls typed method lookup. Prefer an exact reviewed method for the active machine/head/tip/box, reagent family, volume, and labware. Show every difference for a closest physically compatible method; do not turn a reference candidate into a reviewed method. The scientist reviews an adaptation, and the validator and strict simulator make the execution decision. Never infer a liquid class, pipetting height, air gap, blowout, or Z speed from a reagent name.
