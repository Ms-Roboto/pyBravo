"""Read-only, deterministic coverage audit for the pinned Text2WetLab tasks.

This module reads a candidate's source and the dataset's structured simulator
events. It does not execute the source, call a model, or reproduce the Harbor
judge. A ``supported`` check means only that the stated observable condition
was found; ``needs_review`` deliberately preserves scientific and manual-stage
questions for a human. The official score is always null.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

REVISION = "d7c8a9b93428997447eeaf2ee9e27ac3ce026872"
RUBRIC_IDS: dict[str, tuple[str, ...]] = {
    "a1-a12-100ul": ("deck_and_hardware", "volumes_and_wells", "tip_usage", "robot_practice", "fidelity_to_task"),
    "split-200ul-two-wells": ("deck_and_hardware", "volumes_and_wells", "tip_usage", "robot_practice", "fidelity_to_task"),
    "ampure-bead-cleanup": ("binding", "supernatant_and_washes", "drying_and_elution", "recovery", "tips_and_contamination"),
    "colony-pcr-screening": ("reaction_setup", "sample_mapping", "tips_and_contamination", "thermocycling", "fidelity_to_paper"),
    "ecoli-heat-shock-transformation": ("dna_addition", "heat_shock", "soc_recovery", "tip_usage", "fidelity_to_paper"),
    "golden-gate-assembly": ("pcr_setup", "dpni_and_cleanup", "assembly_mix", "cycling_and_transformation", "tips_and_contamination"),
    "opentrons-rna-extraction": ("sample_handling", "binding_and_separation", "washes_and_drying", "elution_recovery", "fidelity_to_paper"),
}

_PLATE_WELLS = tuple(f"{row}{column}" for column in range(1, 13) for row in "ABCDEFGH")
_RNA_WELLS = tuple(f"{row}{column}" for column in (1, 3, 5, 7, 9, 11) for row in "ABCDEFGH")


@dataclass(frozen=True)
class Transfer:
    index: int
    source_labware: str
    source_well: str
    destination_labware: str
    destination_well: str
    volume: float
    instrument: str
    tip: int


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _source_facts(source: str) -> tuple[list[tuple[str, int | None, str | None]], list[str], str | None, str | None]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [], None, f"source syntax error at line {exc.lineno}"
    loads: list[tuple[str, int | None, str | None]] = []
    notes: list[str] = []
    api_level = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "metadata" for t in node.targets):
            value = _literal(node.value)
            if isinstance(value, dict):
                api_level = value.get("apiLevel")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method == "load_labware" and node.args:
            load_name = _literal(node.args[0])
            slot = _literal(node.args[1]) if len(node.args) > 1 else None
            label = next((_literal(kw.value) for kw in node.keywords if kw.arg == "label"), None)
            if isinstance(load_name, str):
                loads.append((load_name, slot if isinstance(slot, int) else None,
                              label if isinstance(label, str) else None))
        elif method in {"comment", "pause"} and node.args:
            note = _literal(node.args[0])
            if isinstance(note, str):
                notes.append(note)
    return loads, notes, api_level if isinstance(api_level, str) else None, None


def _slot(labware: str) -> int | None:
    match = re.search(r"\bon (?:slot )?(\d+)\s*$", labware)
    return int(match.group(1)) if match else None


def _role(task: str, labware: str) -> str:
    slot = _slot(labware)
    by_slot = {
        "a1-a12-100ul": {1: "reservoir", 2: "plate"},
        "split-200ul-two-wells": {1: "reservoir", 2: "plate"},
        "ampure-bead-cleanup": {1: "sample", 2: "beads", 3: "ethanol", 4: "water", 5: "waste", 6: "elution"},
        "colony-pcr-screening": {1: "colony", 2: "pcr", 3: "master_mix", 4: "primer"},
        "ecoli-heat-shock-transformation": {1: "plasmid", 2: "soc"},
        "golden-gate-assembly": {1: "water", 2: "reagents", 3: "primer", 4: "template", 5: "pcr", 6: "assembly", 7: "cells", 8: "lb"},
        "opentrons-rna-extraction": {1: "waste", 4: "extraction", 5: "reservoir", 6: "elution", 7: "samples", 10: "samples"},
    }
    if task == "ecoli-heat-shock-transformation" and "transformation_plate" in labware.lower():
        return "transformation"
    if task == "opentrons-rna-extraction" and slot in {2, 3, 9, 11, 12}:
        return "tips_or_trash"
    return by_slot.get(task, {}).get(slot, "unknown")


def _expanded_wells(task: str, labware: str, well: str, channels: int) -> tuple[str, ...]:
    """Expand an 8-channel plate action; reservoir troughs stay one source."""
    if task == "opentrons-rna-extraction" and channels == 8 and _role(task, labware) in {
        "extraction", "elution", "waste",
    } and re.fullmatch(r"A\d{1,2}", well):
        return tuple(f"{row}{well[1:]}" for row in "ABCDEFGH")
    return (well,)


def _transfers(task: str, events: list[dict]) -> list[Transfer]:
    held: dict[str, tuple[tuple[str, str] | None, float]] = {}
    tip_ids: dict[str, int] = {}
    next_tip = 0
    result: list[Transfer] = []
    for index, event in enumerate(events):
        kind, instrument = event.get("kind"), event.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            continue
        if kind == "pick":
            next_tip += 1
            tip_ids[instrument] = next_tip
            held[instrument] = (None, 0.0)
            continue
        if kind == "drop":
            held[instrument] = (None, 0.0)
            tip_ids.pop(instrument, None)
            continue
        if kind not in {"aspirate", "dispense"}:
            continue
        labware, well, volume = event.get("labware"), event.get("well"), event.get("volume")
        if not isinstance(labware, str) or not isinstance(well, str) or not isinstance(volume, (int, float)):
            continue
        prior_source, prior_volume = held.get(instrument, (None, 0.0))
        if kind == "aspirate":
            loc = (labware, well)
            new_source = loc if prior_volume <= 1e-8 else prior_source if prior_source == loc else None
            held[instrument] = (new_source, prior_volume + float(volume))
            continue
        if prior_source and prior_source != (labware, well) and prior_volume + 1e-8 >= volume:
            channels = event.get("channels", 1)
            channels = channels if isinstance(channels, int) else 1
            sources = _expanded_wells(task, prior_source[0], prior_source[1], channels)
            destinations = _expanded_wells(task, labware, well, channels)
            if len(sources) == 1 and len(destinations) > 1:
                sources = sources * len(destinations)
            if len(destinations) == 1 and len(sources) > 1:
                destinations = destinations * len(sources)
            if len(sources) == len(destinations):
                result.extend(Transfer(index, prior_source[0], src, labware, dst, float(volume),
                                       instrument, tip_ids.get(instrument, 0))
                              for src, dst in zip(sources, destinations))
        remaining = max(0.0, prior_volume - float(volume))
        held[instrument] = (prior_source if remaining > 1e-8 else None, remaining)
    return result


def _matching(transfers: list[Transfer], task: str, *, source: str | None = None,
              destination: str | None = None, source_well: str | None = None,
              destination_well: str | None = None) -> list[Transfer]:
    return [t for t in transfers
            if (source is None or _role(task, t.source_labware) == source)
            and (destination is None or _role(task, t.destination_labware) == destination)
            and (source_well is None or t.source_well == source_well)
            and (destination_well is None or t.destination_well == destination_well)]


def _volume(transfers: list[Transfer]) -> float:
    return sum(t.volume for t in transfers)


def _close(value: float, target: float, tolerance: float = 0.05) -> bool:
    return abs(value - target) <= tolerance


def _tips_closed(events: list[dict]) -> bool:
    attached: dict[str, bool] = {}
    for event in events:
        instrument, kind = event.get("instrument"), event.get("kind")
        if not instrument:
            continue
        if kind == "pick":
            if attached.get(instrument):
                return False
            attached[instrument] = True
        elif kind == "drop":
            if not attached.get(instrument):
                return False
            attached[instrument] = False
        elif kind in {"aspirate", "dispense"} and not attached.get(instrument):
            return False
    return not any(attached.values())


def _tip_isolation(task: str, transfers: list[Transfer], specimen_roles: set[str]) -> bool:
    """One tip may serve one specimen source and its matching destination."""
    by_tip: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for transfer in transfers:
        if _role(task, transfer.source_labware) in specimen_roles:
            by_tip[transfer.tip].add((_role(task, transfer.source_labware), transfer.source_well))
    return all(tip > 0 and len(sources) <= 1 for tip, sources in by_tip.items())


def _magnet_state(events: list[dict], index: int) -> bool:
    state = False
    for event in events[:index + 1]:
        if event.get("kind") == "engage":
            state = True
        elif event.get("kind") == "disengage":
            state = False
    return state


def _manual_notes(notes: list[str], *patterns: str) -> bool:
    return all(any(re.search(pattern, note, re.IGNORECASE) for note in notes) for pattern in patterns)


def _mix_cycles(events: list[dict], task: str, role: str, well: str,
                start: int = 0, stop: int | None = None) -> int:
    """Count paired same-well aspirations/dispenses in a bounded phase."""
    stop = len(events) if stop is None else stop
    count = 0
    for index in range(max(start, 0), min(stop - 1, len(events) - 1)):
        first, second = events[index], events[index + 1]
        if first.get("kind") != "aspirate" or second.get("kind") != "dispense":
            continue
        if first.get("instrument") != second.get("instrument"):
            continue
        if first.get("labware") != second.get("labware") or first.get("well") != second.get("well"):
            continue
        if _role(task, str(first.get("labware"))) != role:
            continue
        if first.get("volume") != second.get("volume"):
            continue
        channels = first.get("channels", 1)
        channel_count = channels if isinstance(channels, int) else 1
        if well in _expanded_wells(task, str(first.get("labware")), str(first.get("well")), channel_count):
            count += 1
    return count


class _Audit:
    def __init__(self, task: str) -> None:
        self.task = task
        self.checks: dict[str, list[dict[str, str]]] = {key: [] for key in RUBRIC_IDS[task]}

    def add(self, item: str, name: str, condition: bool | None, evidence: str) -> None:
        self.checks[item].append({
            "name": name,
            "status": "needs_review" if condition is None else "supported" if condition else "failed",
            "evidence": evidence,
        })

    def result(self) -> dict:
        items = []
        for rubric_id, checks in self.checks.items():
            statuses = {check["status"] for check in checks}
            status = "failed" if "failed" in statuses else "needs_review" if "needs_review" in statuses else "supported"
            items.append({"id": rubric_id, "status": status, "checks": checks})
        statuses = {item["status"] for item in items}
        return {
            "metric": "local_rubric_coverage_audit",
            "dataset_revision": REVISION,
            "task": self.task,
            "status": "failed" if "failed" in statuses else "needs_review" if "needs_review" in statuses else "supported",
            "official_score": None,
            "items": items,
        }


def _audit_simple(a: _Audit, events: list[dict], transfers: list[Transfer], loads: list[tuple], api: str | None) -> None:
    task = a.task
    expected = {(1, "nest_1_reservoir_195ml", "reservoir"),
                (2, "corning_96_wellplate_360ul_flat", "plate")}
    actual = {(slot, name, label) for name, slot, label in loads}
    a.add("deck_and_hardware", "fixed deck", expected <= actual,
          "Reservoir and destination plate must have the pinned load names, slots, and labels.")
    destination = {f"A{i}" for i in range(1, 13)} if task == "a1-a12-100ul" else {"A1", "B1"}
    relevant = _matching(transfers, task, destination="plate")
    volumes = {well: _volume([t for t in relevant if t.destination_well == well]) for well in destination}
    exact = (set(t.destination_well for t in relevant) == destination
             and all(_close(volumes[well], 100) for well in destination)
             and all(_role(task, t.source_labware) == "reservoir" and t.source_well == "A1"
                     and "P300" in t.instrument.upper() for t in relevant))
    a.add("volumes_and_wells", "source, destination, and amounts", exact,
          f"Observed {len(relevant)} directed transfers into the plate; expected 100 µL from reservoir A1 to each of {len(destination)} wells, with no extras.")
    a.add("tip_usage", "attached and dropped tips", _tips_closed(events),
          "Every liquid action needs an attached tip, and every picked tip must be dropped.")
    valid_api = api is not None and bool(re.fullmatch(r"2\.(?:[2-9]|1[0-5])", api))
    pipette_range = all(20 <= t.volume <= 300 for t in relevant)
    a.add("robot_practice", "API and P300 range", valid_api and pipette_range,
          f"apiLevel={api!r}; each observed P300 transfer must be 20–300 µL.")
    a.add("fidelity_to_task", "task-specific semantics", None,
          "The observable transfer pattern is checked above; source metadata and other unparsed actions need review.")


def _audit_ampure(a: _Audit, events: list[dict], transfers: list[Transfer], notes: list[str]) -> None:
    task = a.task
    wells = set(_PLATE_WELLS)
    beads = _matching(transfers, task, source="beads", destination="sample")
    bead_map = {w: _volume([t for t in beads if t.destination_well == w]) for w in wells}
    a.add("binding", "bead volume and coverage", set(t.destination_well for t in beads) == wells
          and all(_close(bead_map[w], 40) for w in wells),
          "All 96 samples need 40 µL beads from the bead reservoir.")
    bead_mix = all(_mix_cycles(events, task, "sample", w,
                               start=min((t.index for t in beads if t.destination_well == w), default=len(events)),
                               stop=min((t.index for t in transfers if _role(task, t.source_labware) == "sample"
                                         and t.source_well == w and _role(task, t.destination_labware) == "waste"),
                                        default=len(events))) >= 10 for w in wells)
    a.add("binding", "ten mixes after bead addition", bead_mix,
          "The runlog should contain at least ten same-well mix cycles per sample after beads are added.")
    a.add("binding", "five-minute incubation", None if _manual_notes(notes, r"incubat.*5\s*min") else False,
          "A five-minute incubation is documented if present; the pinned runlog removes comments, so physical completion remains unverified.")
    waste = _matching(transfers, task, source="sample", destination="waste")
    ethanol = _matching(transfers, task, source="ethanol", destination="sample")
    waste_ok = all(sorted(round(t.volume) for t in waste if t.source_well == w) == [90, 200, 200]
                   for w in wells)
    ethanol_ok = all(_close(_volume([t for t in ethanol if t.destination_well == w]), 400)
                     for w in wells)
    a.add("supernatant_and_washes", "96 supernatants and two ethanol washes", waste_ok and ethanol_ok,
          "Each sample requires 90 µL supernatant removal and two 200 µL ethanol additions/removals.")
    a.add("supernatant_and_washes", "magnet handoff", None if _manual_notes(notes, r"engage.*magnet") else False,
          "Magnet use is documented in a manual note; no magnet event is available on this fixed deck.")
    water = _matching(transfers, task, source="water", destination="sample")
    a.add("drying_and_elution", "water volume", all(_close(_volume([t for t in water if t.destination_well == w]), 50)
                                                for w in wells),
          "Each sample needs 50 µL elution water.")
    water_mix = all(_mix_cycles(events, task, "sample", w,
                                start=min((t.index for t in water if t.destination_well == w), default=len(events)),
                                stop=min((t.index for t in transfers if _role(task, t.source_labware) == "sample"
                                          and t.source_well == w and _role(task, t.destination_labware) == "elution"),
                                         default=len(events))) >= 10 for w in wells)
    a.add("drying_and_elution", "ten mixes after elution water", water_mix,
          "The runlog should contain at least ten same-well mix cycles per sample after water addition.")
    a.add("drying_and_elution", "dry and disengage handoffs",
          None if _manual_notes(notes, r"dry.*5\s*min", r"disengage.*magnet") else False,
          "Drying and magnet removal are documented only; execution requires manual review.")
    recovery = _matching(transfers, task, source="sample", destination="elution")
    a.add("recovery", "matched recovery", {t.source_well for t in recovery} == wells and
          all(t.source_well == t.destination_well for t in recovery) and
          all(_close(_volume([t for t in recovery if t.source_well == w]), 45) for w in wells),
          "One 45 µL sample-to-matching-elution transfer is required for every well.")
    a.add("tips_and_contamination", "sample tip isolation", _tips_closed(events)
          and _tip_isolation(task, transfers, {"sample"}),
          "All sample removals must use a tracked tip that is not shared across sample sources.")
    a.add("tips_and_contamination", "other stock backflow", None,
          "Mix-after-dispense and stock re-entry need a full per-tip runlog review.")


def _audit_colony(a: _Audit, events: list[dict], transfers: list[Transfer], notes: list[str]) -> None:
    task = a.task
    wells = set(_PLATE_WELLS)
    incoming = _matching(transfers, task, destination="pcr")
    bad_wells = set(t.destination_well for t in incoming) - wells
    setup_ok = not bad_wells
    mapping_ok = not bad_wells
    bad_examples: list[str] = []
    for well in wells:
        rows = [t for t in incoming if t.destination_well == well]
        master = _volume([t for t in rows if _role(task, t.source_labware) == "master_mix" and t.source_well == "A1"])
        colony = [t for t in rows if _role(task, t.source_labware) == "colony"]
        primer = [t for t in rows if _role(task, t.source_labware) == "primer"]
        final = _volume(rows)
        volume_ok = (_close(final, 10) or 20 <= final <= 25.05) and _close(master * 2, final)
        setup_ok &= volume_ok and _close(_volume(colony), 1) and bool(primer)
        mapping_ok &= len(colony) == 1 and colony[0].source_well == well and bool(primer)
        mapping_ok &= all(t.source_well == well for t in primer)
        if (not volume_ok or not colony or not primer) and len(bad_examples) < 4:
            bad_examples.append(f"{well}: total={final:g}, 2x mix={master:g}, template={_volume(colony):g}, primer={_volume(primer):g}")
    a.add("reaction_setup", "reaction volumes and 2× dilution", bool(setup_ok),
          "For each of 96 wells, the observed 2× mix must be half the 10 or 20–25 µL final reaction; template is about 1 µL. "
          + ("Examples: " + "; ".join(bad_examples) if bad_examples else "All observed volumes fit."))
    a.add("reaction_setup", "master mix before template", all(
        min((t.index for t in incoming if t.destination_well == well and _role(task, t.source_labware) == "master_mix"), default=10**9)
        < min((t.index for t in incoming if t.destination_well == well and _role(task, t.source_labware) == "colony"), default=-1)
        for well in wells), "The 2× master mix must reach each PCR well before its colony template.")
    a.add("sample_mapping", "96 matching colonies and primers", bool(mapping_ok),
          "Each colony and primer-pair source well must match its PCR destination, once per well.")
    specimen = [t for t in transfers if _role(task, t.source_labware) in {"colony", "primer"}]
    distinct_tips = _tip_isolation(task, specimen, {"colony", "primer"})
    a.add("tips_and_contamination", "fresh source tips", _tips_closed(events) and distinct_tips,
          "Colony and primer transfers require separate fresh tip cycles, with no tip held at the end.")
    documented = _manual_notes(notes, r"seal", r"thermocycl|cycle", r"4\s*°?\s*C|4\s*C")
    a.add("thermocycling", "off-deck cycle handoff", None if documented else False,
          "Seal, thermocycle, and 4 °C hold must be recorded at the handoff; comments do not prove execution.")
    a.add("fidelity_to_paper", "paper-specific cycle and chemistry", None,
          "A scientist must compare primer/amplicon settings and the complete paper method with this candidate.")


def _audit_ecoli(a: _Audit, events: list[dict], transfers: list[Transfer]) -> None:
    task = a.task
    wells = {f"{row}1" for row in "ABCDEFGH"}
    dna = _matching(transfers, task, source="plasmid", destination="transformation")
    dna_ok = len(dna) == 8 and {t.destination_well for t in dna} == wells and all(
        t.source_well == t.destination_well and _close(t.volume, 1) for t in dna)
    a.add("dna_addition", "eight matched 1 µL DNA transfers", dna_ok,
          "Each plasmid A1–H1 must feed only its matching cell well at 1 µL.")
    thermos = [(i, str(e.get("text", ""))) for i, e in enumerate(events) if e.get("kind") == "thermocycler"]
    cold = [i for i, s in thermos if re.search(r"block temperature to 4(?:\.0+)?\s*°C", s)]
    hot = [i for i, s in thermos if re.search(r"block temperature to 42(?:\.0+)?\s*°C", s)
           and re.search(r"30(?:\.0+)?\s*seconds", s)]
    warm = [i for i, s in thermos if re.search(r"block temperature to 37(?:\.0+)?\s*°C", s)]
    closed = [i for i, s in thermos if "Closing Thermocycler lid" in s]
    a.add("dna_addition", "cold block before pipetting", bool(cold and dna and min(cold) < min(t.index for t in dna)),
          "The 4 °C block command must precede every plasmid addition.")
    a.add("heat_shock", "timed thermocycler sequence", bool(cold and hot and
          any(re.search(r"30(?:\.0+)?\s*minutes", s) and i < hot[0] for i, s in thermos)
          and max(t.index for t in dna) < hot[0]),
          "After DNA, the block must hold 4 °C for 30 min, then 42 °C for 30 s.")
    a.add("heat_shock", "closed thermocycler lid", bool(closed and hot and
          any(max(t.index for t in dna) < i < hot[0] for i in closed)),
          "The lid must close after DNA pipetting and before the timed heat shock.")
    soc = _matching(transfers, task, source="soc", destination="transformation")
    soc_ok = len(soc) == 8 and {t.destination_well for t in soc} == wells and all(_close(t.volume, 50) for t in soc)
    a.add("soc_recovery", "eight 50 µL SOC additions", soc_ok and bool(hot) and
          min((t.index for t in soc), default=-1) > max(hot, default=10**9),
          "SOC additions must follow the 42 °C pulse and cover all eight transformations.")
    a.add("soc_recovery", "return to 37 °C before serial pipetting and recover", bool(warm and soc)
          and min(warm) < min(t.index for t in soc)
          and any(re.search(r"60(?:\.0+)?\s*minutes", s) and i > max(t.index for t in soc) for i, s in thermos),
          "The hot block should leave 42 °C before SOC pipetting, followed by a 37 °C/60 min hold.")
    a.add("tip_usage", "DNA tip isolation", _tips_closed(events) and _tip_isolation(task, transfers, {"plasmid", "transformation"}),
          "Every plasmid must use an isolated, fully dropped tip cycle.")
    a.add("fidelity_to_paper", "scientific fidelity", None,
          "The paper method, exact lid behavior, cell handling and reagent identity require review.")


_GOLDEN_FRAGMENTS = {
    "A1": ("A1", "A1", "A2"), "B1": ("A1", "B1", "B2"),
    "C1": ("B1", "C1", "C2"), "D1": ("C1", "D1", "D2"),
    "E1": ("A1", "E1", "E2"), "F1": ("A1", "F1", "F2"),
    "G1": ("D1", "G1", "G2"),
}
_GOLDEN_ASSEMBLIES = {
    "A1": {"B1": 3, "E1": 2, "F1": 3, "A1": 2},
    "B1": {"B1": 3, "E1": 2, "F1": 3, "C1": 2},
    "C1": {"B1": 3, "E1": 2, "F1": 3, "D1": 2},
    "D1": {"B1": 3, "E1": 2, "F1": 3, "G1": 2},
}


def _audit_golden(a: _Audit, events: list[dict], transfers: list[Transfer], notes: list[str]) -> None:
    task = a.task
    pcr_all = _matching(transfers, task, destination="pcr")
    # DpnI is added to the same wells after PCR. Its 19 µL water stroke marks
    # the second phase; counting it as PCR would incorrectly make 50 µL PCRs.
    dpni_start = min((t.index for t in pcr_all if _role(task, t.source_labware) == "water"
                      and _close(t.volume, 19)), default=10**9)
    pcr = [t for t in pcr_all if t.index < dpni_start]
    pcr_ok = True
    for well, (template, forward, reverse) in _GOLDEN_FRAGMENTS.items():
        incoming = [t for t in pcr if t.destination_well == well]
        primer_rows = [t for t in incoming if _role(task, t.source_labware) == "primer"]
        template_rows = [t for t in incoming if _role(task, t.source_labware) == "template"]
        pcr_ok &= (_close(_volume(incoming), 25)
                   and {t.source_well for t in primer_rows} == {forward, reverse}
                   and all(_close(_volume([t for t in primer_rows if t.source_well == w]), 2.5)
                           for w in (forward, reverse))
                   and len(template_rows) == 1 and template_rows[0].source_well == template
                   and _close(_volume(template_rows), 1))
    a.add("pcr_setup", "seven mapped 25 µL PCRs", bool(pcr_ok),
          "Each A1–G1 PCR needs its fixed template, 2.5 µL of each 1 µM primer, 1 µL 0.5 ng/µL template and 25 µL total.")
    a.add("pcr_setup", "Q5 master-mix composition", None,
          "The buffer/dNTP/polymerase/water composition and 1× final chemistry need review, especially when a premix is used.")
    dpni = all(_close(_volume([t for t in _matching(transfers, task, source="water", destination="pcr", destination_well=w)
                                   if t.index >= dpni_start]), 19)
               and _close(_volume([t for t in _matching(transfers, task, source="reagents", destination="pcr", source_well="A2", destination_well=w)
                                   if t.index >= dpni_start]), 5)
               and _close(_volume([t for t in _matching(transfers, task, source="reagents", destination="pcr", source_well="B2", destination_well=w)
                                   if t.index >= dpni_start]), 1)
               for w in _GOLDEN_FRAGMENTS)
    a.add("dpni_and_cleanup", "DpnI addition volumes", dpni,
          "After each PCR, DpnI setup needs 19 µL water, 5 µL rCutSmart, and 1 µL DpnI.")
    stage_notes = _manual_notes(notes, r"98.*30\s*s", r"DpnI|37.*30\s*min", r"clean.?up|column")
    a.add("dpni_and_cleanup", "gradient PCR, digestion, cleanup handoffs", None if stage_notes else False,
          "The off-deck PCR/DpnI/column stages must be documented in order; notes alone do not establish completion.")
    assembly = _matching(transfers, task, destination="assembly")
    assembly_ok = set(t.destination_well for t in assembly) == set(_GOLDEN_ASSEMBLIES)
    for well, fragments in _GOLDEN_ASSEMBLIES.items():
        rows = [t for t in assembly if t.destination_well == well]
        assembly_ok &= _close(_volume(rows), 20)
        assembly_ok &= all(_close(_volume([t for t in rows if _role(task, t.source_labware) == "pcr" and t.source_well == src]), vol)
                           for src, vol in fragments.items())
        assembly_ok &= _close(_volume([t for t in rows if _role(task, t.source_labware) == "reagents" and t.source_well == "C2"]), 2)
        enzyme = _volume([t for t in rows if _role(task, t.source_labware) == "reagents" and t.source_well == "D2"])
        assembly_ok &= 1 <= enzyme <= 2.05
    a.add("assembly_mix", "four 20 µL design-table assemblies", bool(assembly_ok),
          "Each A1–D1 assembly needs its exact four fragment volumes, 2 µL 10× buffer, 1–2 µL enzyme, and 20 µL total.")
    cycle_notes = _manual_notes(notes, r"Golden Gate|37.*16", r"heat shock|transform|TOP10", r"kanamycin|LB agar")
    a.add("cycling_and_transformation", "cycle and plating handoffs", None if cycle_notes else False,
          "The Golden Gate cycle, TOP10 transformation/recovery and kanamycin plating must be documented; physical steps need review.")
    cells = {f"{row}1" for row in "ABCD"}
    assembly_to_cells = _matching(transfers, task, source="assembly", destination="cells")
    lb_to_cells = _matching(transfers, task, source="lb", destination="cells", source_well="A1")
    recovery_pipetting = all(
        any(t.source_well == well and t.destination_well == well for t in assembly_to_cells)
        and _close(_volume([t for t in lb_to_cells if t.destination_well == well]), 250)
        for well in cells)
    a.add("cycling_and_transformation", "assembly-to-cells and LB recovery pipetting", recovery_pipetting,
          "Each assembly must feed its matching TOP10 well, followed by 250 µL LB plus dextrose for recovery.")
    a.add("tips_and_contamination", "primer/template/fragment tip isolation", _tips_closed(events)
          and _tip_isolation(task, transfers, {"primer", "template", "pcr", "assembly"}),
          "Tips used for distinct primers, templates, fragments or assemblies must not be shared.")
    a.add("tips_and_contamination", "shared reagent backflow", None,
          "An independent per-tip inspection should confirm no shared stock is re-entered after touching a reaction.")


def _audit_rna(a: _Audit, events: list[dict], transfers: list[Transfer]) -> None:
    task = a.task
    wells = set(_RNA_WELLS)
    samples = _matching(transfers, task, source="samples", destination="extraction")
    sample_wells = {(t.source_labware, t.source_well) for t in samples}
    sample_ok = (len(sample_wells) == 48
                 and {t.destination_well for t in samples} == wells
                 and all(len({t.destination_well for t in samples
                              if (t.source_labware, t.source_well) == src}) == 1
                         and _close(_volume([t for t in samples
                                             if (t.source_labware, t.source_well) == src]), 250)
                         for src in sample_wells))
    a.add("sample_handling", "48 one-to-one 250 µL samples", sample_ok,
          "Two 24-tube racks must feed 48 distinct extraction wells in the six odd columns, one sample each.")
    a.add("sample_handling", "sample tip isolation", _tips_closed(events)
          and _tip_isolation(task, samples, {"samples"})
          and len({t.tip for t in samples}) == len(sample_wells),
          "Every sample transfer requires a fresh, fully dropped tip cycle.")
    beads = _matching(transfers, task, source="reservoir", destination="extraction", source_well="A2")
    iso = [t for t in _matching(transfers, task, source="reservoir", destination="extraction")
           if t.source_well in {"A6", "A7"}]
    binding_ok = all(_close(_volume([t for t in beads if t.destination_well == w]), 40)
                     and _close(_volume([t for t in iso if t.destination_well == w]), 250)
                     and any(t.destination_well == w for t in samples)
                     and min((t.index for t in beads if t.destination_well == w), default=10**9)
                     < min((t.index for t in iso if t.destination_well == w), default=-1)
                     < min((t.index for t in samples if t.destination_well == w), default=-1)
                     for w in wells)
    a.add("binding_and_separation", "beads → isopropanol → sample", binding_ok,
          "Every extraction well needs 40 µL beads, then 250 µL isopropanol, then 250 µL sample.")
    first_engage = next((i for i, e in enumerate(events) if e.get("kind") == "engage"), -1)
    binding_delay = any(e.get("kind") == "delay" and 270 <= e.get("seconds", 0) <= 330
                        for e in events[:first_engage]) if first_engage >= 0 else False
    settle_delay = any(e.get("kind") == "delay" and 210 <= e.get("seconds", 0) <= 300
                       for e in events[first_engage + 1:]) if first_engage >= 0 else False
    removed = _matching(transfers, task, source="extraction", destination="waste")
    removal_after_magnet = bool(removed) and all(_magnet_state(events, t.index) for t in removed)
    a.add("binding_and_separation", "binding/settling waits and magnetic removal",
          binding_delay and settle_delay and removal_after_magnet,
          "A ~5 min binding wait, magnet engagement/~4 min settling, then waste removal with magnet on are required.")
    mixed = all(_mix_cycles(events, task, "extraction", w,
                            start=min((t.index for t in samples if t.destination_well == w), default=len(events)),
                            stop=first_engage if first_engage >= 0 else len(events)) >= 5 for w in wells)
    a.add("binding_and_separation", "five mixing cycles per sample", mixed,
          "Each sample needs at least five same-well mix cycles after sample addition and before magnetic separation.")
    etoh = [t for t in _matching(transfers, task, source="reservoir", destination="extraction")
            if t.source_well in {"A9", "A10", "A11", "A12"}]
    def wash_cycles(well: str) -> list[tuple[float, float, bool]]:
        additions = [t for t in etoh if t.destination_well == well]
        if not additions:
            return []
        operations = sorted([("add", t) for t in additions]
                            + [("remove", t) for t in removed if t.source_well == well and
                               t.index > min(row.index for row in additions)], key=lambda entry: entry[1].index)
        cycles: list[tuple[float, float, bool]] = []
        added = removed_volume = 0.0
        magnet_on = True
        for kind, transfer in operations:
            if kind == "add":
                if removed_volume > 0:
                    cycles.append((added, removed_volume, magnet_on))
                    added = removed_volume = 0.0
                    magnet_on = True
                added += transfer.volume
            elif added > 0:
                removed_volume += transfer.volume
                magnet_on &= _magnet_state(events, transfer.index)
        if added > 0:
            cycles.append((added, removed_volume, magnet_on))
        return cycles

    washes_ok = all(len(cycles := wash_cycles(w)) == 2 and
                     all(490 <= added <= 510 and removed_volume >= added - 10 and magnet_on
                         for added, removed_volume, magnet_on in cycles)
                     for w in wells)
    a.add("washes_and_drying", "two separated 500 µL ethanol washes", washes_ok,
          "Each sample needs two distinct ~500 µL 70% ethanol additions, each removed to waste with the magnet on.")
    elution_buffer = _matching(transfers, task, source="reservoir", destination="extraction", source_well="A4")
    first_elution = min((t.index for t in elution_buffer), default=len(events))
    last_wash_removal = max((t.index for t in removed if t.index < first_elution), default=-1)
    dry = any(e.get("kind") == "delay" and 210 <= e.get("seconds", 0) <= 300
              for e in events[last_wash_removal + 1:first_elution])
    disengaged = any(e.get("kind") == "disengage" for e in events[last_wash_removal + 1:first_elution])
    a.add("washes_and_drying", "dry and magnet disengage", dry and disengaged,
          "After the last wash removal, a ~4 min dry and magnet disengagement must precede elution.")
    buffer_ok = all(_close(_volume([t for t in elution_buffer if t.destination_well == w]), 100) for w in wells)
    off_magnet = all(not _magnet_state(events, t.index) for t in elution_buffer)
    recovered = _matching(transfers, task, source="extraction", destination="elution")
    recovery_ok = ({t.source_well for t in recovered} == wells
                   and all(t.source_well == t.destination_well for t in recovered)
                   and all(_close(_volume([t for t in recovered if t.source_well == w]), 80) for w in wells)
                   and any(e.get("kind") == "temp" and _close(float(e.get("celsius", -1)), 4)
                           for e in events[:min((t.index for t in recovered), default=0)]))
    a.add("elution_recovery", "100 µL off-magnet elution", buffer_ok and off_magnet,
          "Each extraction well needs 100 µL elution buffer while the magnet is off.")
    a.add("elution_recovery", "80 µL matched recovery at 4 °C", recovery_ok,
          "Every source well must recover about 80 µL to its own 4 °C elution well.")
    mixed_elution = all(_mix_cycles(events, task, "extraction", w,
                                 start=min((t.index for t in elution_buffer if t.destination_well == w), default=len(events)),
                                 stop=min((t.index for t in recovered if t.source_well == w), default=len(events))) >= 1
                        for w in wells)
    clearing = all(any(e.get("kind") == "delay" and 75 <= e.get("seconds", 0) <= 110
                       for e in events[max(0, min((t.index for t in elution_buffer if t.destination_well == w),
                                                    default=len(events))):
                                       min((t.index for t in recovered if t.source_well == w), default=len(events))])
                   for w in wells)
    recovery_on_magnet = bool(recovered) and all(_magnet_state(events, t.index) for t in recovered)
    a.add("elution_recovery", "90 s magnetic clearing and mixing", mixed_elution and clearing and recovery_on_magnet,
          "Each well needs a mix after elution buffer and an approximately 90 s clearing wait before recovery.")
    a.add("fidelity_to_paper", "paper and reagent fidelity", None,
          "The complete paper method, stock identities and comments/metadata require scientist review.")


def audit_rubric_coverage(task: str, events: list[dict], source: str,
                          labware: dict[str, str] | None = None) -> dict:
    """Audit observable rubric coverage without producing an official grade.

    ``events`` and ``labware`` are the pinned ``tests/runlog.py`` output. The
    optional labware map is retained in the signature for runner integration;
    the fixed task deck slots define roles even when RNA's logger omits it.
    """
    if task not in RUBRIC_IDS:
        raise ValueError(f"Unknown pinned Text2WetLab task: {task}")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("events must be the pinned runlog's array of objects")
    if not isinstance(source, str):
        raise TypeError("source must be protocol Python text")
    _ = labware  # The pinned RNA runlog does not provide a labware map.
    loads, notes, api, parse_error = _source_facts(source)
    audit = _Audit(task)
    if parse_error:
        for rubric_id in RUBRIC_IDS[task]:
            audit.add(rubric_id, "source parse", False, parse_error)
        return audit.result()
    transfers = _transfers(task, events)
    if task in {"a1-a12-100ul", "split-200ul-two-wells"}:
        _audit_simple(audit, events, transfers, loads, api)
    elif task == "ampure-bead-cleanup":
        _audit_ampure(audit, events, transfers, notes)
    elif task == "colony-pcr-screening":
        _audit_colony(audit, events, transfers, notes)
    elif task == "ecoli-heat-shock-transformation":
        _audit_ecoli(audit, events, transfers)
    elif task == "golden-gate-assembly":
        _audit_golden(audit, events, transfers, notes)
    elif task == "opentrons-rna-extraction":
        _audit_rna(audit, events, transfers)
    return audit.result()
