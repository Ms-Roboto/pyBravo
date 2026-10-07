"""Versioned liquid-handling methods and conservative, explainable lookup.

A method is a reviewed application of a liquid class to a reagent and labware,
not another copy of the machine's motion settings. Imported legacy classes are
useful search candidates, but they cannot silently acquire missing technique,
tip identity, or qualification from their names or normalizer defaults.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

try:  # Bravo deployments may author methods on Windows.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix
    msvcrt = None

from pybravo.head_mode import head_geometry_for_type, normalize_head_mode, plate_footprint_wells
from pybravo.types import HeadType

from .tipbox_choices import compatible_tipbox_choices

STANDARD = "pybravo.bravo-method-registry"
METHOD_REGISTRY_SCHEMA_VERSION = "1.0.0"
_STORE_PATH = Path(__file__).resolve().parents[3] / "config" / "protocol_methods.yaml"
_LIQUID_STORE_PATH = Path(__file__).resolve().parents[3] / "config" / "liquid_classes.yaml"
_MOTION_FIELDS = (
    "w_velocity_ul_s",
    "w_acceleration_ul_s2",
    "post_delay_ms",
    "z_in_velocity_mm_s",
    "z_in_acceleration_mm_s2",
    "z_out_velocity_mm_s",
    "z_out_acceleration_mm_s2",
)


class MethodQuery(BaseModel):
    operation: Literal["transfer", "mix", "distribute"] = "transfer"
    machine_id: str | None = None
    head_type: str | None = None
    tip_id: str | None = None
    tipbox_id: str | None = None
    source_labware_id: str | None = None
    destination_labware_id: str | None = None
    reagent_id: str | None = None
    reagent_family: str | None = None
    volume_ul: float | None = None
    dispense_volumes_ul: list[float] | None = None
    source_anchor: str | None = None
    destination_anchor: str | None = None


class MethodApplicability(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    head_types: list[str] = Field(default_factory=list)
    tip_ids: list[str] = Field(default_factory=list)
    tipbox_ids: list[str] = Field(default_factory=list)
    source_labware_ids: list[str] = Field(default_factory=list)
    destination_labware_ids: list[str] = Field(default_factory=list)
    reagent_ids: list[str] = Field(default_factory=list)
    reagent_families: list[str] = Field(default_factory=list)
    operations: list[Literal["transfer", "mix", "distribute"]] = Field(default_factory=lambda: ["transfer"])
    min_volume_ul: float | None = None
    max_volume_ul: float | None = None


class LiquidClassRef(BaseModel):
    id: str
    name: str | None = None
    digest: str | None = None
    # Scientist attestations on a method. `reviewed_default` and
    # `reviewed_inferred` explicitly acknowledge legacy normalization.
    field_origins: dict[str, str] = Field(default_factory=dict)
    # Derived from the raw store; never trusted from an authoring request.
    source_field_origins: dict[str, str] = Field(default_factory=dict)


class MethodSourceRef(BaseModel):
    method_id: str
    revision: str


class AspirateSettings(BaseModel):
    liquid_class_ref: LiquidClassRef | None = None
    distance_from_bottom_mm: float | None = None
    pre_air_ul: float | None = None
    post_air_ul: float | None = None
    # Millimetres of Z travel during the standalone aspirate stroke.
    dynamic_tip_extension: float | None = None
    tip_touch: bool | None = None
    well_offset: dict[str, float] | None = None


class DispenseSettings(BaseModel):
    liquid_class_ref: LiquidClassRef | None = None
    distance_from_bottom_mm: float | None = None
    blowout_ul: float | None = None
    # Millimetres of Z travel per microlitre dispensed.
    dynamic_tip_retraction: float | None = None
    tip_touch: bool | None = None
    well_offset: dict[str, float] | None = None


class MethodProvenance(BaseModel):
    source_type: Literal[
        "local_config", "local_review", "local_qualification", "scientist_statement",
        "publication", "synthetic_example",
    ]
    source_path: str | None = None
    source_url: str | None = None
    source_digest: str | None = None
    retrieved_at: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None
    run_id: str | None = None
    note: str | None = None


class MethodRecord(BaseModel):
    method_id: str
    version: str
    status: Literal["reference_candidate", "imported_unverified", "reviewed", "qualified"]
    title: str
    derived_from: MethodSourceRef | None = None
    applicability: MethodApplicability
    aspirate: AspirateSettings = Field(default_factory=AspirateSettings)
    dispense: DispenseSettings = Field(default_factory=DispenseSettings)
    tip_policy: Literal["fresh_per_source", "fresh_per_transfer", "reuse_within_source"] | None = None
    evidence: list[MethodProvenance] = Field(default_factory=list)


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _store_path() -> Path:
    configured = os.environ.get("PYBRAVO_METHOD_STORE_PATH", "").strip()
    return Path(configured).expanduser() if configured else _STORE_PATH


def _liquid_store_path() -> Path:
    configured = os.environ.get("PYBRAVO_LIQUID_CLASS_STORE_PATH", "").strip()
    return Path(configured).expanduser() if configured else _LIQUID_STORE_PATH


def _raw_liquid_classes() -> dict[str, dict[str, Any]]:
    """Inspect the unnormalized file so omitted settings remain distinguishable."""
    # The optional Mongo backend mirrors normalized data to YAML and thereby
    # loses evidence of which legacy fields were originally omitted. In that
    # case treat provenance as unknown rather than claiming the mirror is raw.
    if os.environ.get("PYBRAVO_LIQUID_MONGO_URI") and os.environ.get("PYBRAVO_LIQUID_MONGO_DB"):
        return {}
    path = _liquid_store_path()
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        content = yaml.safe_load(fh) or {}
    return {
        str(row["liquid_class_id"]): row for row in _rows(content.get("liquid_classes")) if row.get("liquid_class_id")
    }


def _class_ref(row: Mapping[str, Any], raw: Mapping[str, Any] | None) -> LiquidClassRef:
    if isinstance(raw, Mapping) and (
        raw.get("machine_id") != row.get("machine_id") or raw.get("head_type") != row.get("head_type")
    ):
        raw = None
    origins = {}
    declarations = raw.get("field_origins") if isinstance(raw, Mapping) else None
    declarations = declarations if isinstance(declarations, Mapping) else {}
    for phase in ("aspirate", "dispense"):
        raw_phase = raw.get(phase) if isinstance(raw, Mapping) else None
        active_phase = row.get(phase) if isinstance(row.get(phase), Mapping) else {}
        for field in _MOTION_FIELDS:
            key = f"{phase}.{field}"
            present = isinstance(raw_phase, Mapping) and field in raw_phase
            if present:
                try:
                    present = math.isclose(float(raw_phase[field]), float(active_phase[field]), rel_tol=0, abs_tol=1e-9)
                except (KeyError, TypeError, ValueError):
                    present = False
            declared = declarations.get(key)
            origins[key] = (
                declared
                if present and declared in {"measured", "reviewed", "imported_config"}
                else "imported_config"
                if present
                else "normalizer_default_or_unknown"
            )
    raw_equation = raw.get("equation") if isinstance(raw, Mapping) else None
    raw_points = raw_equation.get("control_points") if isinstance(raw_equation, Mapping) else None
    active_equation = row.get("equation") if isinstance(row.get("equation"), Mapping) else None
    active_points = active_equation.get("control_points") if isinstance(active_equation, Mapping) else None
    points_match = False
    if isinstance(raw_points, list) and isinstance(active_points, list) and len(raw_points) >= 2:
        try:
            source_points = sorted((float(point["desired_ul"]), float(point["commanded_ul"])) for point in raw_points)
            target_points = sorted(
                (float(point["desired_ul"]), float(point["commanded_ul"])) for point in active_points
            )
            points_match = source_points == target_points
        except (KeyError, TypeError, ValueError):
            pass
    origins["equation.control_points"] = "imported_config" if points_match else "normalizer_default_or_unknown"
    declared_tip = declarations.get("tip_id")
    tip_present = isinstance(raw, Mapping) and raw.get("tip_id") and raw.get("tip_id") == row.get("tip_id")
    origins["tip_id"] = (
        declared_tip
        if tip_present and declared_tip in {"measured", "reviewed", "imported_config"}
        else "imported_config"
        if tip_present
        else "inferred_or_unknown"
    )
    return LiquidClassRef(
        id=str(row.get("liquid_class_id") or row.get("id") or ""),
        name=str(row.get("name") or ""),
        digest=_digest(dict(row)),
        field_origins=origins,
        source_field_origins=origins,
    )


def _imported_method(row: Mapping[str, Any], raw: Mapping[str, Any] | None) -> MethodRecord | None:
    class_id = str(row.get("liquid_class_id") or row.get("id") or "")
    machine, head = str(row.get("machine_id") or ""), str(row.get("head_type") or "")
    if not class_id or not machine or not head:
        return None
    # A normalizer can infer an empty tip ID from nominal capacity. Retain only
    # an explicitly stored tip as an applicability claim.
    raw_tip = (
        str(raw.get("tip_id"))
        if isinstance(raw, Mapping) and raw.get("tip_id") and raw.get("tip_id") == row.get("tip_id")
        else ""
    )
    ref = _class_ref(row, raw)
    capacity = row.get("tip_capacity_ul")
    max_volume = float(capacity) if _positive(capacity) else None
    return MethodRecord(
        method_id=f"imported:{class_id}",
        version="1.0.0",
        status="imported_unverified",
        title=str(row.get("name") or class_id),
        applicability=MethodApplicability(
            machine_ids=[machine],
            head_types=[head],
            tip_ids=[raw_tip] if raw_tip else [],
            max_volume_ul=max_volume,
        ),
        aspirate=AspirateSettings(liquid_class_ref=ref),
        dispense=DispenseSettings(liquid_class_ref=ref),
        evidence=[
            MethodProvenance(
                source_type="local_config",
                source_path=(
                    "config/liquid_classes.yaml"
                    if _liquid_store_path() == _LIQUID_STORE_PATH
                    else "configured-liquid-class-store"
                ),
                source_digest=_digest(dict(raw)) if raw else ref.digest,
                note="Imported class parameters are candidates, not a reagent-qualified method.",
            )
        ],
    )


def _authored_methods() -> list[MethodRecord]:
    path = _store_path()
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if data.get("version") != 1:
        raise ValueError(f"Unsupported protocol method store version in {path}")
    records = [MethodRecord.model_validate(row) for row in _rows(data.get("methods"))]
    for record in records:
        _semantic_version(record.version)
    keys = [(record.method_id, record.version) for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate protocol method ID and version")
    return records


def _positive(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _nonnegative(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _active_class(context: Mapping[str, Any], ref: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not ref:
        return None
    for row in _rows(context.get("liquid_classes")):
        if (row.get("liquid_class_id") or row.get("id")) == ref.get("id"):
            return row
    return None


def _missing_fields(
    record: MethodRecord,
    context: Mapping[str, Any],
    raw_classes: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    missing: list[str] = []
    reviewed = any(
        item.source_type == "local_review" and item.reviewer and item.reviewed_at and item.note
        for item in record.evidence
    )
    applies = record.applicability
    for field in ("machine_ids", "head_types", "tip_ids", "tipbox_ids", "source_labware_ids", "reagent_families"):
        if not getattr(applies, field):
            missing.append(f"applicability.{field}")
    if any(operation != "mix" for operation in applies.operations) and not applies.destination_labware_ids:
        missing.append("applicability.destination_labware_ids")
    if not applies.operations:
        missing.append("applicability.operations")
    if not _positive(applies.max_volume_ul):
        missing.append("applicability.max_volume_ul")
    if applies.min_volume_ul is None or not _nonnegative(applies.min_volume_ul):
        missing.append("applicability.min_volume_ul")
    if (
        _positive(applies.max_volume_ul)
        and _nonnegative(applies.min_volume_ul)
        and applies.min_volume_ul > applies.max_volume_ul
    ):
        missing.append("applicability.volume_range")
    if (
        _positive(context.get("head_max_volume_ul"))
        and _positive(applies.max_volume_ul)
        and applies.max_volume_ul > context["head_max_volume_ul"]
    ):
        missing.append("applicability.head_capacity")
    if record.tip_policy is None:
        missing.append("tip_policy")
    for phase_name, keys in (
        ("aspirate", ("distance_from_bottom_mm", "pre_air_ul", "post_air_ul", "dynamic_tip_extension", "tip_touch")),
        ("dispense", ("distance_from_bottom_mm", "blowout_ul", "dynamic_tip_retraction", "tip_touch")),
    ):
        phase = getattr(record, phase_name)
        ref = phase.liquid_class_ref.model_dump() if phase.liquid_class_ref else None
        liquid_class = _active_class(context, ref)
        if liquid_class is None or not ref or ref.get("digest") != _digest(dict(liquid_class)):
            missing.append(f"{phase_name}.liquid_class_ref")
        elif (
            liquid_class.get("machine_id") not in applies.machine_ids
            or liquid_class.get("head_type") not in applies.head_types
            or liquid_class.get("tip_id") not in applies.tip_ids
        ):
            missing.append(f"{phase_name}.liquid_class_compatibility")
        if liquid_class is not None:
            source = raw_classes.get(str(ref.get("id"))) if ref else None
            actual_origins = _class_ref(liquid_class, source).field_origins
            attestations = (ref or {}).get("field_origins") or {}
            if any(
                (
                    origin in {"normalizer_default_or_unknown", "inferred_or_unknown"}
                    and (
                        not reviewed
                        or attestations.get(key) != ("reviewed_inferred" if key == "tip_id" else "reviewed_default")
                    )
                )
                for key, origin in actual_origins.items()
            ):
                missing.append(f"{phase_name}.liquid_class_unverified_fields")
            if any(
                declared is not None
                and declared
                not in (
                    {"reviewed_inferred"}
                    if key == "tip_id" and origin == "inferred_or_unknown"
                    else {"reviewed_default"}
                    if origin == "normalizer_default_or_unknown"
                    else {origin, "measured", "reviewed"}
                )
                for key, origin in actual_origins.items()
                if (declared := attestations.get(key)) is not None
            ):
                missing.append(f"{phase_name}.liquid_class_origin_conflict")
            motion = liquid_class.get(phase_name) or {}
            if not isinstance(motion, Mapping) or any(
                not (_nonnegative(motion.get(key)) if key == "post_delay_ms" else _positive(motion.get(key)))
                for key in _MOTION_FIELDS
            ):
                missing.append(f"{phase_name}.liquid_class_motion_invalid")
            points = (liquid_class.get("equation") or {}).get("control_points") or []
            if not isinstance(points, list):
                missing.append(f"{phase_name}.liquid_class_calibration_range")
            else:
                desired = [
                    point.get("desired_ul")
                    for point in points
                    if isinstance(point, Mapping) and _nonnegative(point.get("desired_ul"))
                ]
                if (
                    not desired
                    or (_nonnegative(applies.min_volume_ul) and min(desired) > applies.min_volume_ul)
                    or (_positive(applies.max_volume_ul) and max(desired) < applies.max_volume_ul)
                ):
                    missing.append(f"{phase_name}.liquid_class_calibration_range")
        for key in keys:
            value = getattr(phase, key)
            if value is None or (
                isinstance(value, (int, float)) and not isinstance(value, bool) and not _nonnegative(value)
            ):
                missing.append(f"{phase_name}.{key}")
        if phase.well_offset is None:
            missing.append(f"{phase_name}.well_offset")
        elif set(phase.well_offset) != {"x_fraction_of_well_radius", "y_fraction_of_well_radius"} or any(
            not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or value != 0
            for value in phase.well_offset.values()
        ):
            # Nonzero VWorks well offsets are not yet represented by Bravo DAG nodes.
            missing.append(f"{phase_name}.well_offset_unsupported")
    if record.status not in {"reviewed", "qualified"}:
        missing.append("status.review_required")
    if not reviewed:
        missing.append("evidence.local_review")
    if record.status == "qualified" and not any(
        item.source_type == "local_qualification" and item.run_id and item.reviewed_at for item in record.evidence
    ):
        missing.append("evidence.local_qualification")
    tip_definitions = {str(row.get("tip_id") or row.get("id")): row for row in _rows(context.get("tip_definitions"))}
    ready_pairs = {
        (row.get("labware_id"), row.get("tip_definition_id"))
        for row in _rows(context.get("tipbox_choices"))
        if row.get("execution_ready") is True
    }
    for tip_id in applies.tip_ids:
        tip = tip_definitions.get(tip_id)
        if (
            not tip
            or not _positive(tip.get("capacity_ul"))
            or (_positive(applies.max_volume_ul) and applies.max_volume_ul > tip["capacity_ul"])
        ):
            missing.append("applicability.tip_capacity")
        if not any((rack, tip_id) in ready_pairs for rack in applies.tipbox_ids):
            missing.append("applicability.tipbox_compatibility")
        for phase_name in ("aspirate", "dispense"):
            phase_ref = getattr(record, phase_name).liquid_class_ref
            active = _active_class(context, phase_ref.model_dump() if phase_ref else None)
            if (
                tip is not None
                and active is not None
                and _positive(tip.get("capacity_ul"))
                and not math.isclose(
                    float(active.get("tip_capacity_ul") or 0.0), float(tip["capacity_ul"]), abs_tol=1e-6
                )
            ):
                missing.append(f"{phase_name}.liquid_class_tip_capacity")
        if tip and _positive(tip.get("capacity_ul")) and _positive(applies.max_volume_ul):
            air = (record.aspirate.pre_air_ul or 0) + (record.aspirate.post_air_ul or 0)
            if applies.max_volume_ul + air > tip["capacity_ul"]:
                missing.append("aspirate.effective_tip_capacity")
    labware = {str(row.get("id")): row for row in _rows(context.get("labware"))}
    for phase_name, plate_ids in (
        ("aspirate", applies.source_labware_ids),
        ("dispense", applies.destination_labware_ids),
    ):
        height = getattr(record, phase_name).distance_from_bottom_mm
        for plate_id in plate_ids:
            plate = labware.get(plate_id)
            if (
                plate is None
                or plate.get("provisional")
                or plate.get("base_class") != "microplate"
                or not _positive(plate.get("well_depth_mm"))
                or (height is not None and height >= plate["well_depth_mm"])
            ):
                missing.append(f"applicability.{phase_name}_labware_geometry")
    return sorted(set(missing))


def _record_dict(
    record: MethodRecord,
    context: Mapping[str, Any],
    raw_classes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    base = record.model_dump(mode="json")
    for phase_name in ("aspirate", "dispense"):
        ref = base[phase_name].get("liquid_class_ref")
        if ref:
            active = _active_class(context, ref)
            raw = raw_classes.get(str(ref.get("id")))
            ref["source_field_origins"] = _class_ref(active, raw).field_origins if active else {}
    base["revision"] = _digest(base)
    base["missing_fields"] = _missing_fields(record, context, raw_classes)
    base["execution_ready"] = not base["missing_fields"]
    return base


def list_methods(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_classes = _raw_liquid_classes()
    imported = []
    for row in _rows(context.get("liquid_classes")):
        class_id = str(row.get("liquid_class_id") or row.get("id") or "")
        method = _imported_method(row, raw_classes.get(class_id))
        if method:
            imported.append(method)
    authored = _authored_methods()
    records = [*_record_dicts(imported, context, raw_classes), *_record_dicts(authored, context, raw_classes)]
    return sorted(records, key=lambda row: (row["method_id"], row["version"]))


def _record_dicts(
    records: list[MethodRecord],
    context: Mapping[str, Any],
    raw_classes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [_record_dict(record, context, raw_classes) for record in records]


def get_method(context: Mapping[str, Any], method_id: str, revision: str | None = None) -> dict[str, Any] | None:
    """Return a method only at its exact pinned revision when one is supplied."""
    candidates = [row for row in list_methods(context) if row["method_id"] == method_id]
    if revision is not None:
        candidates = [row for row in candidates if row["revision"] == revision]
    return max(candidates, key=lambda row: _semantic_version(row["version"])) if candidates else None


def method_registry(context: Mapping[str, Any]) -> dict[str, Any]:
    methods = list_methods(context)
    payload = {"standard": STANDARD, "schema_version": METHOD_REGISTRY_SCHEMA_VERSION, "methods": methods}
    return {
        **payload,
        "digest": _digest(payload),
        "status_counts": dict(sorted(Counter(row["status"] for row in methods).items())),
    }


class MethodConflictError(RuntimeError):
    """The reviewed method store changed or a version already exists."""


class MethodValidationError(ValueError):
    """A candidate lacks explicit evidence or settings for execution."""

    def __init__(self, message: str, missing_fields: list[str] | None = None):
        super().__init__(message)
        self.missing_fields = missing_fields or []


def _semantic_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise MethodValidationError("Method version must be a numeric major.minor.patch value.")
    return tuple(int(part) for part in value.split("."))


def _read_authored_store(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "methods": []}
    with path.open(encoding="utf-8") as fh:
        store = yaml.safe_load(fh) or {}
    if store.get("version") != 1 or not isinstance(store.get("methods"), list):
        raise MethodValidationError("Unsupported or malformed protocol method store.")
    return store


def _write_authored_store(path: Path, store: Mapping[str, Any]) -> None:
    """Replace YAML atomically while the caller owns the sidecar file lock."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as fh:
            temporary = fh.name
            yaml.safe_dump(dict(store), fh, sort_keys=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def _exclusive_method_lock(path: Path):
    with path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            if path.stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported platform
            raise RuntimeError("No supported file-locking primitive is available")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def save_reviewed_method(
    context: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    expected_registry_digest: str,
    expected_method_revision: str | None = None,
) -> dict[str, Any]:
    """Append an explicitly reviewed method version without promoting a guess.

    The registry digest is an optimistic concurrency token. Updating an
    existing method ID additionally requires its latest pinned revision and a
    strictly newer semantic version. The source candidate, when supplied, is
    itself pinned by `derived_from`.
    """
    if not expected_registry_digest:
        raise MethodConflictError("Expected registry digest is required for curation.")
    record = MethodRecord.model_validate(payload)
    if record.status != "reviewed":
        raise MethodValidationError("This operation can save only reviewed methods; qualification is separate.")
    if not record.method_id or record.method_id.startswith(("imported:", "reference:")):
        raise MethodValidationError("Use a distinct, non-reserved ID for an authored method.")
    incoming_version = _semantic_version(record.version)
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _exclusive_method_lock(lock_path):
        registry = method_registry(context)
        if registry["digest"] != expected_registry_digest:
            raise MethodConflictError("Method registry changed; reload before saving this review.")
        if record.derived_from and not any(
            row["method_id"] == record.derived_from.method_id and row["revision"] == record.derived_from.revision
            for row in registry["methods"]
        ):
            raise MethodConflictError("Source candidate revision changed or is unavailable.")
        store = _read_authored_store(path)
        versions = [row for row in _rows(store["methods"]) if row.get("method_id") == record.method_id]
        if any(row.get("version") == record.version for row in versions):
            raise MethodConflictError("That method ID and version already exist.")
        if versions:
            latest = max(versions, key=lambda row: _semantic_version(str(row.get("version") or "")))
            latest_revision = get_method(context, record.method_id)
            if (
                latest_revision is None
                or expected_method_revision is None
                or latest_revision["revision"] != expected_method_revision
            ):
                raise MethodConflictError("Latest method revision must be supplied to append a version.")
            if incoming_version <= _semantic_version(str(latest["version"])):
                raise MethodConflictError("New method version must be greater than the latest version.")
        elif expected_method_revision is not None:
            raise MethodConflictError("An expected method revision was supplied for a new ID.")
        raw_classes = _raw_liquid_classes()
        reviewed = _record_dict(record, context, raw_classes)
        if not reviewed["execution_ready"]:
            raise MethodValidationError(
                "Reviewed method is incomplete or incompatible with the active catalogs.",
                reviewed["missing_fields"],
            )
        # Preserve the author-controlled data; derived source origins and
        # readiness are recomputed on every subsequent read.
        authored = record.model_dump(
            mode="json",
            exclude={
                "aspirate": {"liquid_class_ref": {"source_field_origins"}},
                "dispense": {"liquid_class_ref": {"source_field_origins"}},
            },
        )
        store["methods"].append(authored)
        _write_authored_store(path, store)
        return reviewed


def _query_issues(context: Mapping[str, Any], query: MethodQuery) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def issue(field: str, reason: str) -> None:
        issues.append({"field": field, "reason": reason})

    machine = query.machine_id or context.get("machine_id")
    head_name = query.head_type or context.get("head_type")
    if not machine or machine != context.get("machine_id"):
        issue("machine_id", "Must identify the active machine.")
    try:
        head = HeadType[str(head_name)]
        if not head.is_disposable or head.name != context.get("head_type"):
            issue("head_type", "Must identify the active disposable head.")
    except (KeyError, TypeError, ValueError):
        head = None
        issue("head_type", "Unknown head type.")
    tip = next(
        (row for row in _rows(context.get("tip_definitions")) if (row.get("tip_id") or row.get("id")) == query.tip_id),
        None,
    )
    if (
        not query.tip_id
        or tip is None
        or not _positive(tip.get("capacity_ul"))
        or not head
        or head.name not in (tip.get("compatible_heads") or tip.get("supported_head_types") or [])
    ):
        issue("tip_id", "Select an explicitly head-compatible tip definition.")
    if query.tipbox_id:
        choices = _rows(context.get("tipbox_choices")) or compatible_tipbox_choices(
            head_name, _rows(context.get("labware")), _rows(context.get("tip_definitions"))
        )
        if not any(
            row.get("labware_id") == query.tipbox_id
            and row.get("tip_definition_id") == query.tip_id
            and row.get("execution_ready") is True
            for row in choices
        ):
            issue("tipbox_id", "No execution-ready catalog rack/tip pair for this head.")
    else:
        issue("tipbox_id", "Select a catalog tip box; live inventory is confirmed separately.")
    aliquots = query.dispense_volumes_ul if query.operation == "distribute" else None
    stroke_volume = (
        max(aliquots)
        if isinstance(aliquots, list) and aliquots and all(_positive(value) for value in aliquots)
        else query.volume_ul
    )
    if not _positive(query.volume_ul) or (
        tip
        and _positive(tip.get("capacity_ul"))
        and _positive(stroke_volume)
        and stroke_volume > float(tip["capacity_ul"])
    ):
        issue("volume_ul", "Volume must be positive and fit the selected tip.")
    if query.operation == "distribute":
        if (
            not isinstance(aliquots, list)
            or not aliquots
            or not all(_positive(value) for value in aliquots)
            or not _positive(query.volume_ul)
            or not math.isclose(sum(aliquots), query.volume_ul, abs_tol=1e-6)
        ):
            issue("dispense_volumes_ul", "Distribute aliquots must be positive and sum to the aspiration volume.")
    if (
        _positive(stroke_volume)
        and _positive(context.get("head_max_volume_ul"))
        and stroke_volume > context["head_max_volume_ul"]
    ):
        issue("volume_ul", "Volume exceeds the configured head stroke.")
    try:
        from .capabilities import _tip_plate_compatibility  # same published hard relation

        incompatible = {
            (row["target_labware_id"], row["tip_definition_id"])
            for row in _tip_plate_compatibility(
                head,
                _rows(context.get("labware")),
                _rows(context.get("tip_definitions")),
                _rows(context.get("tipbox_choices")),
            )
            if row.get("compatible") is False
        }
    except (ImportError, KeyError, TypeError):
        incompatible = set()
    for field in ("source_labware_id", "destination_labware_id"):
        identity = getattr(query, field)
        if field == "destination_labware_id" and query.operation == "mix":
            continue
        plate = next((row for row in _rows(context.get("labware")) if row.get("id") == identity), None)
        if not identity or plate is None or plate.get("provisional") or plate.get("base_class") != "microplate":
            issue(field, "Select a verified microplate catalog ID.")
            continue
        if (
            not all(
                isinstance(plate.get(key), int) and not isinstance(plate.get(key), bool) and plate[key] > 0
                for key in ("rows", "cols", "wells")
            )
            or plate["rows"] * plate["cols"] != plate["wells"]
            or not all(
                _positive(plate.get(key)) for key in ("spacing_x_mm", "spacing_y_mm", "well_depth_mm", "well_volume_ul")
            )
        ):
            issue(field, "Plate grid, pitch, well depth and capacity must be verified.")
            continue
        addressed_volume = (
            max(query.dispense_volumes_ul)
            if field == "destination_labware_id"
            and query.operation == "distribute"
            and query.dispense_volumes_ul
            and all(_positive(value) for value in query.dispense_volumes_ul)
            else query.volume_ul
        )
        if _positive(addressed_volume) and addressed_volume > plate["well_volume_ul"]:
            issue(field, "Requested volume exceeds a well's catalog capacity.")
        if query.tip_id == "st_70ul" and plate.get("wells") == 1536:
            issue(field, "ST70 tips cannot address 1536-well microplates.")
        elif (identity, query.tip_id) in incompatible:
            issue(field, "The capability manifest explicitly excludes this tip/plate pair.")
        anchor = query.source_anchor if field == "source_labware_id" else query.destination_anchor
        if anchor and head:
            from .validation import well_cell  # local import avoids a validation/methods cycle

            try:
                cell = well_cell(anchor)
                mode = normalize_head_mode(head, "all_barrels", "back_left")
                cells = plate_footprint_wells(
                    head,
                    mode,
                    int(plate["rows"]),
                    int(plate["cols"]),
                    float(plate["spacing_x_mm"]),
                    float(plate["spacing_y_mm"]),
                    *cell,
                )
                geometry = head_geometry_for_type(head)
                if len(cells) != geometry.rows * geometry.columns:
                    issue(field, f"The full head footprint does not fit anchor {anchor}.")
            except (KeyError, ValueError, TypeError):
                issue(field, f"Invalid or unreachable anchor {anchor}.")
    return issues


def _mismatches(method: Mapping[str, Any], query: MethodQuery) -> list[dict[str, Any]]:
    applies = method["applicability"]
    fields = (
        ("machine_ids", query.machine_id),
        ("head_types", query.head_type),
        ("tip_ids", query.tip_id),
        ("tipbox_ids", query.tipbox_id),
        ("source_labware_ids", query.source_labware_id),
        ("destination_labware_ids", query.destination_labware_id),
        ("reagent_families", query.reagent_family),
        ("operations", query.operation),
    )
    result = []
    for field, requested in fields:
        if field == "destination_labware_ids" and query.operation == "mix":
            continue
        options = applies.get(field) or []
        if not options or requested is None or requested not in options:
            result.append(
                {
                    "field": field,
                    "requested": requested,
                    "method": options,
                    "reason": (
                        "unspecified" if not options else "request_unspecified" if requested is None else "different"
                    ),
                }
            )
    # Exact formulations are optional in the library. When a record names
    # one, a broad reagent-family match must not stand in for that identity.
    reagent_ids = applies.get("reagent_ids") or []
    if reagent_ids and (not query.reagent_id or not any(
        query.reagent_id.casefold() == str(identity).casefold() for identity in reagent_ids
    )):
        result.append({
            "field": "reagent_ids",
            "requested": query.reagent_id,
            "method": reagent_ids,
            "reason": "request_unspecified" if query.reagent_id is None else "different",
        })
    volumes = (
        query.dispense_volumes_ul
        if query.operation == "distribute" and query.dispense_volumes_ul
        else [query.volume_ul]
    )
    outside = [
        volume
        for volume in volumes
        if volume is not None
        and (
            (applies.get("min_volume_ul") is not None and volume < applies["min_volume_ul"])
            or (applies.get("max_volume_ul") is not None and volume > applies["max_volume_ul"])
        )
    ]
    if outside:
        result.append(
            {
                "field": "volume_ul",
                "requested": outside if query.operation == "distribute" else outside[0],
                "method": [applies.get("min_volume_ul"), applies.get("max_volume_ul")],
                "reason": "outside_method_range",
            }
        )
    return result


def lookup_methods(context: Mapping[str, Any], query: Mapping[str, Any] | MethodQuery) -> dict[str, Any]:
    """Rank compatible methods without treating a closest match as qualified."""
    request = query if isinstance(query, MethodQuery) else MethodQuery.model_validate(query)
    request = request.model_copy(
        update={
            "machine_id": request.machine_id or context.get("machine_id"),
            "head_type": request.head_type or context.get("head_type"),
        }
    )
    issues = _query_issues(context, request)
    registry = method_registry(context)
    result: dict[str, Any] = {
        "registry_digest": registry["digest"],
        "query": request.model_dump(mode="json"),
        "issues": issues,
        "candidates": [],
    }
    if issues:
        return result
    tip = next(
        (
            row
            for row in _rows(context.get("tip_definitions"))
            if (row.get("tip_id") or row.get("id")) == request.tip_id
        ),
        None,
    )
    stroke_limit = min(
        value
        for value in (tip.get("capacity_ul") if tip else None, context.get("head_max_volume_ul"))
        if _positive(value)
    )
    fallback_needed = request.operation == "distribute" and request.volume_ul > stroke_limit
    result["shared_aspiration_feasible"] = not fallback_needed
    if fallback_needed:
        result["planning_notes"] = [
            "Combined aspiration exceeds the tip/head stroke; use separate aspirate/dispense pairs "
            "for each aliquot if the reviewed method and contamination policy permit it."
        ]
    candidates = []
    for method in registry["methods"]:
        applies = method["applicability"]
        if (applies["machine_ids"] and request.machine_id not in applies["machine_ids"]) or (
            applies["head_types"] and request.head_type not in applies["head_types"]
        ):
            continue
        # A different explicit tip is a hardware incompatibility. Missing tip
        # identity remains visible as an incomplete research candidate.
        if applies["tip_ids"] and request.tip_id not in applies["tip_ids"]:
            continue
        if applies["operations"] and request.operation not in applies["operations"]:
            continue
        mismatch = _mismatches(method, request)
        if fallback_needed:
            mismatch.append(
                {
                    "field": "shared_aspiration_capacity",
                    "requested": request.volume_ul,
                    "method": stroke_limit,
                    "reason": "paired_aspirations_required",
                }
            )
        score = max(
            0,
            100
            - 12 * sum(item["reason"] == "different" for item in mismatch)
            - 6 * sum(item["reason"] == "unspecified" for item in mismatch)
            - 30 * sum(item["reason"] == "outside_method_range" for item in mismatch),
        )
        candidates.append(
            {
                **method,
                "method_ref": {"method_id": method["method_id"], "revision": method["revision"]},
                "score": score,
                "match_kind": "exact" if not mismatch else "closest",
                "mismatches": mismatch,
            }
        )
    candidates.sort(key=lambda row: (-row["execution_ready"], -row["score"], row["method_id"], row["version"]))
    result["candidates"] = candidates[:20]
    return result
