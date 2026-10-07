---
name: 384-to-1536-quadrants
description: Plan a source-isolated 384-well to 1536-well quadrant transfer when that geometry is requested.
---

With a 384-channel head and catalog-confirmed 384-to-1536 geometry, destination anchors A1, A2, B1, and B2 represent four disjoint 384-well footprints. A source plate occupies one corresponding quadrant on each named destination. Four source plates into two destinations therefore require eight full-head transfers. With a smaller head, divide each quadrant into validated subsets rather than claiming one step covers all 384 wells.

A full 384-well source transfer may use source anchor A1 with the assigned destination quadrant anchor when the active head supports that mapping.

If source-to-quadrant assignment is unspecified, propose source 1→A1, 2→A2, 3→B1, 4→B2 on both destinations, and ask the scientist to confirm. The anchor follows source identity, so each source uses the same quadrant on destination 1 and destination 2. For a four-source 384-to-1536 ST10 stamping request, propose four sources stacked in one deck position (source 1 bottom through source 4 top), a work position, a processed-source stack position, one fresh 384-tip set and one distinct ST10 rack per source, four rack positions, and two destination positions. Process the top source first. If Labcyte PP/LDV plates are named, select verified matching catalog geometry; a catalog match is not physical confirmation.

One review-draft layout places four separate tip racks in slots 1–4, two destinations in slots 5 and 8, and the four-plate source stack in slot 9. Slots 6 and 7 must start empty for working and processed plates; do not create materials for those empty slots. Use slot 6 for source-access moves and slot 7 for parking/stacking processed sources. These are proposals to verify against the actual deck and clearance, not permission to run.

Keep the same source-dedicated tip set across the two destination transfers only if the scientist confirms the noncontact contamination policy; use a different clean set for every other source. No-cross-contamination language does not authorize reuse across sources. Tip reuse does not itself authorize a shared aspiration. List racks in the same order as their paired sources are processed. Keep map, stack order, deck positions, tip inventory, method, and motion settings as review proposals until their respective sources are confirmed.
