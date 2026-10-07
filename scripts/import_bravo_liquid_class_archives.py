"""Reconcile Agilent ROI liquid-class archives with pyBravo's liquid catalog.

The archive XML contains motion and calibration data, but does not identify a
machine, reagent, plate pair, or (reliably) a tip. Existing catalog bindings
are therefore retained unchanged. New classes require an explicit mapping:

    classes:
      AM_250uLTipsLowVol:
        head_type: HT_96_D_200
        tip_id: lt_250ul
        tip_capacity_ul: 250

Usage::

    python scripts/import_bravo_liquid_class_archives.py \
        '/Users/kelsorj/Desktop/Bravo LC' \
        --machine-id 04-91-62-CF-7B-B0 \
        --index /tmp/bravo-liquid-class-archives.yaml

    python scripts/import_bravo_liquid_class_archives.py \
        '/Users/kelsorj/Desktop/Bravo LC' \
        --machine-id 04-91-62-CF-7B-B0 --apply

Without ``--apply`` the catalog is not written. ``--apply`` also writes a
reviewable YAML index alongside the catalog, unless ``--index`` overrides its
path. ``--index`` may be used without ``--apply`` to review all archives first.
Neither importing nor matching an archive qualifies a liquid method.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "config" / "liquid_classes.yaml"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


MOTION_FIELDS = {
    "aspirate.w_velocity_ul_s": "Aspirate Velocity",
    "aspirate.w_acceleration_ul_s2": "Aspirate Acceleration",
    "aspirate.post_delay_ms": "Post Aspirate Delay",
    "aspirate.z_in_velocity_mm_s": "Aspirate Velocity Into Wells",
    "aspirate.z_in_acceleration_mm_s2": "Aspirate Acceleration Into Wells",
    "aspirate.z_out_velocity_mm_s": "Aspirate Velocity Out Of Wells",
    "aspirate.z_out_acceleration_mm_s2": "Aspirate Acceleration Out Of Wells",
    "dispense.w_velocity_ul_s": "Dispense Velocity",
    "dispense.w_acceleration_ul_s2": "Dispense Acceleration",
    "dispense.post_delay_ms": "Post Dispense Delay",
    "dispense.z_in_velocity_mm_s": "Dispense Velocity Into Wells",
    "dispense.z_in_acceleration_mm_s2": "Dispense Acceleration Into Wells",
    "dispense.z_out_velocity_mm_s": "Dispense Velocity Out Of Wells",
    "dispense.z_out_acceleration_mm_s2": "Dispense Acceleration Out Of Wells",
}


def _number(text: str, *, context: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: expected a number, got {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{context}: non-finite number")
    return value


def _metadata_root(raw: bytes) -> ET.Element:
    # Some Agilent exports contain UTF-8 bytes with a false UTF-16 declaration.
    # Parse the actual encoding and omit the declaration, rather than trusting it.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text).strip()
    return ET.fromstring(text)


def _control_points(coefficients: list[float], tip_capacity_ul: float) -> list[dict[str, float]]:
    """Sample the vendor correction polynomial, retaining coefficients in evidence."""
    if tip_capacity_ul <= 0 or not math.isfinite(tip_capacity_ul):
        raise ValueError("tip_capacity_ul must be positive and finite")
    if len(coefficients) <= 2:
        desired_volumes = [0.0, tip_capacity_ul]
    else:
        count = min(len(coefficients) + 2, 8)
        desired_volumes = [tip_capacity_ul * i / (count - 1) for i in range(count)]
    return [
        {
            "desired_ul": round(desired, 6),
            "commanded_ul": round(max(0.0, sum(c * desired**i for i, c in enumerate(coefficients))), 6),
        }
        for desired in desired_volumes
    ]


def parse_archive(path: Path) -> dict[str, Any]:
    """Parse one ROI ZIP without assigning unsupported applicability metadata."""
    try:
        with ZipFile(path) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member:
                raise ValueError(f"{path.name}: corrupt ZIP member {corrupt_member}")
            xml_members = [
                name for name in archive.namelist()
                if name.lower().endswith(".xml") and name != "[Content_Types].xml"
            ]
            if len(xml_members) != 1:
                raise ValueError(f"{path.name}: expected exactly one liquid-entry XML member")
            xml_member = xml_members[0]
            root = ET.fromstring(archive.read(xml_member).rstrip(b"\x00"))
            if root.tag != "Velocity11" or root.get("file") != "Liquid Entry":
                raise ValueError(f"{path.name}: not a Velocity11 Liquid Entry")
            name = str(root.get("extrainfo") or "").strip()
            if not name:
                raise ValueError(f"{path.name}: missing class name")
            values: dict[str, str] = {}
            for value in root.findall("./values/value"):
                key = value.get("name")
                if not key or key in values:
                    raise ValueError(f"{path.name}: missing or duplicate XML value key")
                values[key] = str(value.get("value") or "")
            motion: dict[str, dict[str, float | int]] = {"aspirate": {}, "dispense": {}}
            for dotted, xml_key in MOTION_FIELDS.items():
                if xml_key not in values:
                    raise ValueError(f"{path.name}: missing {xml_key}")
                value = _number(values[xml_key], context=f"{path.name}:{xml_key}")
                if value < 0 or ("delay" not in dotted and value == 0):
                    raise ValueError(f"{path.name}: invalid {xml_key}={value}")
                group, field = dotted.split(".", 1)
                motion[group][field] = int(value) if "delay" in dotted else value
                if "delay" in dotted and value != int(value):
                    raise ValueError(f"{path.name}: {xml_key} must be an integer")
            coefficient_values: dict[str, str] = {}
            for value in root.findall("./subkeys/subkey[@name='Coefficients']/values/value"):
                key = value.get("name")
                if not key or key in coefficient_values:
                    raise ValueError(f"{path.name}: duplicate coefficient key")
                coefficient_values[key] = str(value.get("value") or "")
            try:
                count = int(coefficient_values["Number of Coefficients"])
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path.name}: invalid coefficient count") from exc
            if not 1 <= count <= 16 or set(coefficient_values) != {
                "Number of Coefficients", *(str(i) for i in range(count))
            }:
                raise ValueError(f"{path.name}: coefficient count does not match values")
            coefficients = [
                _number(coefficient_values[str(i)], context=f"{path.name}:coefficient {i}")
                for i in range(count)
            ]
            metadata_members = [n for n in archive.namelist() if n.endswith(".otherMetadata")]
            if len(metadata_members) != 1:
                raise ValueError(f"{path.name}: missing or duplicate ROI metadata")
            roi_state = _metadata_root(archive.read(metadata_members[0])).findtext("ROIState")
            if not roi_state:
                raise ValueError(f"{path.name}: missing ROIState")
            md5_members = [n for n in archive.namelist() if n.endswith(".md5")]
            md5_sidecar = archive.read(md5_members[0]).decode("ascii") if len(md5_members) == 1 else None
    except BadZipFile as exc:
        raise ValueError(f"{path.name}: invalid ROI ZIP") from exc
    return {
        "name": name,
        "description": values.get("Note", ""),
        "motion": motion,
        "coefficients": coefficients,
        "source_archive": {
            "filename": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "roi_state": roi_state,
            "xml_version": root.get("version"),
            "entry_version": values.get("VERSION"),
            "raw_coefficients": coefficients,
            "raw_coefficient_values": [coefficient_values[str(i)] for i in range(count)],
            "embedded_md5": root.get("md5sum"),
            "sidecar_md5": md5_sidecar,
        },
    }


def load_archives(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise ValueError(f"Archive directory does not exist: {directory}")
    paths = sorted(directory.glob("*.xml.roiZip"))
    if not paths:
        raise ValueError(f"No .xml.roiZip files found in {directory}")
    entries = [parse_archive(path) for path in paths]
    names = [entry["name"] for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate liquid-class names in archive directory")
    return entries


def _get_nested(record: dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for key in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _differences(record: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for dotted in MOTION_FIELDS:
        group, field = dotted.split(".", 1)
        catalog_value = _get_nested(record, dotted)
        archive_value = entry["motion"][group][field]
        try:
            equal = math.isclose(float(catalog_value), float(archive_value), rel_tol=0, abs_tol=1e-6)
        except (TypeError, ValueError):
            equal = False
        if not equal:
            differences.append({"field": dotted, "catalog": catalog_value, "archive": archive_value})
    capacity = record.get("tip_capacity_ul")
    if capacity is not None:
        expected_points = _control_points(entry["coefficients"], float(capacity))
        catalog_points = _get_nested(record, "equation.control_points")
        if catalog_points != expected_points:
            differences.append({
                "field": "equation.control_points",
                "catalog": catalog_points,
                "archive": expected_points,
            })
    return differences


def _validate_explicit_mapping(mapping: dict[str, Any]) -> tuple[str, str, float]:
    head_type = str(mapping.get("head_type") or "").strip()
    tip_id = str(mapping.get("tip_id") or "").strip()
    try:
        capacity = float(mapping.get("tip_capacity_ul"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Explicit mapping requires tip_capacity_ul") from exc
    if not head_type or not tip_id or capacity <= 0:
        raise ValueError("Explicit mapping requires head_type, tip_id, and positive tip_capacity_ul")
    from pybravo.tips import get_tip_definition, get_tip_definition_by_id

    tip = get_tip_definition_by_id(tip_id)
    compatible = get_tip_definition(head_type, tip_id)
    if tip is None or compatible is None or compatible.tip_id != tip_id:
        raise ValueError(f"Unsupported head/tip pair: {head_type}/{tip_id}")
    if not math.isclose(float(tip.capacity_ul), capacity, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"Tip capacity mismatch for {tip_id}: catalog={tip.capacity_ul}, mapping={capacity}")
    return head_type, tip_id, capacity


def reconcile(
    store: dict[str, Any], entries: list[dict[str, Any]], *, machine_id: str,
    mappings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add archive evidence to exact matches; import only explicitly mapped new rows."""
    if not machine_id.strip():
        raise ValueError("machine_id is required")
    mappings = mappings or {}
    unknown = set(mappings) - {entry["name"] for entry in entries}
    if unknown:
        raise ValueError(f"Mapping contains unknown class names: {sorted(unknown)}")
    updated = deepcopy(store)
    classes = updated.setdefault("liquid_classes", [])
    report: dict[str, Any] = {"version": 1, "machine_id": machine_id, "archives": []}
    ids = {row.get("liquid_class_id") for row in classes}
    for entry in entries:
        name = entry["name"]
        candidates = [
            row for row in classes
            if row.get("name") == name and row.get("machine_id") == machine_id
        ]
        index: dict[str, Any] = {
            "name": name,
            "source_archive": entry["source_archive"],
            "archive_note": entry["description"],
            "archive_motion": entry["motion"],
            "status": "unmapped",
            "needs_review": ["archive does not establish machine/head/tip or reagent/plate applicability"],
        }
        if len(candidates) > 1:
            index["status"] = "ambiguous_catalog_match"
            index["matching_ids"] = [row.get("liquid_class_id") for row in candidates]
        elif candidates:
            row = candidates[0]
            if name in mappings:
                head, tip, capacity = _validate_explicit_mapping(mappings[name])
                if (row.get("head_type"), row.get("tip_id"), float(row.get("tip_capacity_ul") or 0)) != (
                    head, tip, capacity
                ):
                    raise ValueError(f"Explicit mapping conflicts with existing catalog row: {name}")
            old_source = row.get("source_archive")
            if isinstance(old_source, dict) and old_source.get("filename") not in (None, entry["source_archive"]["filename"]):
                index["status"] = "source_conflict"
                index["catalog_id"] = row.get("liquid_class_id")
                index["prior_source_archive"] = old_source
            else:
                differences = _differences(row, entry)
                if not differences:
                    row["source_archive"] = deepcopy(entry["source_archive"])
                    origins = row.setdefault("field_origins", {})
                    for field in (*MOTION_FIELDS, "equation.control_points"):
                        if _get_nested(row, field) is not None:
                            origins.setdefault(field, "imported_config")
                index["status"] = "matched_values_differ" if differences else "matched_preserved"
                index["catalog_id"] = row.get("liquid_class_id")
                index["catalog_binding"] = {
                    key: row.get(key) for key in ("machine_id", "head_type", "tip_id", "tip_capacity_ul")
                }
                index["differences"] = differences
                if not row.get("tip_id"):
                    index["needs_review"].append("tip ID is blank in existing catalog")
        elif name in mappings:
            head, tip, capacity = _validate_explicit_mapping(mappings[name])
            digest = hashlib.sha256(f"{machine_id}\0{head}\0{tip}\0{name}".encode()).hexdigest()
            liquid_id = "liq_" + digest[:10]
            if liquid_id in ids:
                raise ValueError(f"Generated liquid-class ID collision for {name}")
            ids.add(liquid_id)
            row = {
                "liquid_class_id": liquid_id,
                "name": name,
                "description": entry["description"],
                "machine_id": machine_id,
                "head_type": head,
                "tip_id": tip,
                "tip_capacity_ul": capacity,
                "aspirate": deepcopy(entry["motion"]["aspirate"]),
                "dispense": deepcopy(entry["motion"]["dispense"]),
                "equation": {"control_points": _control_points(entry["coefficients"], capacity)},
                "source_archive": deepcopy(entry["source_archive"]),
                "field_origins": {field: "imported_config" for field in (*MOTION_FIELDS, "equation.control_points")},
            }
            classes.append(row)
            index["status"] = "imported_explicit_mapping"
            index["catalog_id"] = liquid_id
            index["catalog_binding"] = {
                key: row.get(key) for key in ("machine_id", "head_type", "tip_id", "tip_capacity_ul")
            }
            index["needs_review"].append("reagent/plate applicability not qualified by archive")
        report["archives"].append(index)
    counts: dict[str, int] = {}
    for item in report["archives"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    report["summary"] = {"archive_count": len(entries), "status_counts": counts, "catalog_count": len(classes)}
    return updated, report


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--mapping", type=Path, help="YAML file with explicit `classes` head/tip assignments")
    parser.add_argument("--index", "--report", dest="index", type=Path, help="Write YAML archive index to this path")
    parser.add_argument("--apply", action="store_true", help="Write the reconciled catalog")
    args = parser.parse_args(argv)
    try:
        entries = load_archives(args.archive_dir)
        store = yaml.safe_load(args.catalog.read_text(encoding="utf-8")) or {}
        mappings = {}
        if args.mapping:
            raw_mapping = yaml.safe_load(args.mapping.read_text(encoding="utf-8")) or {}
            mappings = raw_mapping.get("classes") or {}
            if not isinstance(mappings, dict):
                raise ValueError("Mapping file `classes` must be a mapping")
        updated, report = reconcile(store, entries, machine_id=args.machine_id, mappings=mappings)
        catalog_changed = False
        if args.apply:
            catalog_changed = _write_if_changed(
                args.catalog, yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)
            )
        report_path = args.index or (args.catalog.parent / "liquid_class_archives.yaml" if args.apply else None)
        if report_path:
            _write_if_changed(report_path, yaml.safe_dump(report, sort_keys=False, allow_unicode=True))
        print(yaml.safe_dump({
            "summary": report["summary"],
            "catalog_written": catalog_changed,
            "index": str(report_path) if report_path else None,
        }, sort_keys=False).rstrip())
        return 0
    except (OSError, ValueError, ET.ParseError, yaml.YAMLError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
