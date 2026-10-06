---
name: liquid-methods
description: Capture reagent and liquid-transfer intent for later deterministic method lookup.
---

Record a source material's stated reagent identity and family. Use `transfer` for one source-to-destination aliquot and `distribute` only when one source feeds several ordered destinations. Record every target volume; the validator may lower a distribute to paired aspirations if a shared aspiration is unsafe.

Leave `method_ref` null during extraction. After the draft identifies the source, destinations, tip, and rack, the application calls typed method lookup. Prefer an exact reviewed method for the active machine/head/tip/box, reagent family, volume, and labware. Show every difference for a closest physically compatible method; do not turn a reference candidate into a reviewed method. The scientist reviews an adaptation, and the validator and strict simulator make the execution decision. Never infer a liquid class, pipetting height, air gap, blowout, or Z speed from a reagent name.
