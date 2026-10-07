"""Reviewed protocol API. Drafting and simulation never use a hardware controller."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from pybravo.bravo import Bravo
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.ingest import (
    MAX_PDF_BYTES,
    MAX_TEXT_CHARS,
    IngestedProtocol,
    SourceParagraph,
    ingest_pdf,
    ingest_text,
)
from pybravo.workflow.protocols.liquid_class_proposals import LiquidClassProposalQuery
from pybravo.workflow.protocols.methods import MethodQuery, MethodRecord
from pybravo.workflow.protocols.models import ProtocolPlan, ProtocolSetup
from pybravo.workflow.protocols.store import (
    ProtocolStore,
    RevisionConflict,
    now,
    record_digest,
    workflow_digest,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Protocol assistant"])
_store = ProtocolStore()
_simulations: dict[str, asyncio.Task] = {}


def _server():
    from pybravo.web import server
    return server


def _bravo():
    return _server()._bravo or Bravo(mode="simulation")


def _record(identity: str) -> dict:
    try:
        record = _store.get("sessions", identity)
    except ValueError as exc:
        raise HTTPException(404, "Protocol not found") from exc
    if record is None:
        raise HTTPException(404, "Protocol not found")
    return record


def _sources(record: dict) -> list[dict]:
    return IngestedProtocol.model_validate(record["source"]).select(
        record["selected_paragraph_ids"]).model_dump()["paragraphs"]


def _validate(record: dict, context: dict) -> dict:
    from pybravo.workflow.protocols.validation import validate_plan
    if not record.get("plan"):
        raise HTTPException(409, "Extract or enter a protocol before validation.")
    return validate_plan(ProtocolPlan.model_validate(record["plan"]),
                         ProtocolSetup.model_validate(record["setup"]), context, sources=_sources(record))


def _compile(record: dict, context: dict) -> dict:
    from pybravo.workflow.protocols.compiler import compile_plan
    return compile_plan(ProtocolPlan.model_validate(record["plan"]),
                        ProtocolSetup.model_validate(record["setup"]), context, sources=_sources(record))


def _valid(report: dict) -> bool:
    return report.get("valid", report.get("ok")) is True and not any(
        i.get("severity") == "error" for i in report.get("issues", []))


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextRequest(Payload):
    text: str = Field(min_length=1, max_length=200000)
    name: str = "Pasted protocol"


class EditRequest(Payload):
    revision: int
    plan: ProtocolPlan | None = None
    setup: ProtocolSetup | None = None
    selected_paragraph_ids: list[str] | None = None


class ApplyCatalogDeadVolumesRequest(Payload):
    revision: int = Field(ge=1)


class ExtractRequest(Payload):
    revision: int | None = None
    selected_paragraph_ids: list[str] | None = None
    instructions: str = ""


class ChatRequest(Payload):
    message: str = Field(min_length=1, max_length=200000)
    session_id: str | None = None
    revision: int | None = Field(default=None, ge=1)


class SetupRecommendationRequest(Payload):
    """Evaluate the current draft, including edits that have not been saved yet."""

    plan: ProtocolPlan
    setup: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    selected_paragraph_ids: list[str] | None = None


class MethodSaveRequest(Payload):
    expected_registry_digest: str = Field(min_length=1)
    expected_method_revision: str | None = None
    method: MethodRecord


class ApprovalRequest(Payload):
    scientist: str = Field(min_length=1, max_length=200)
    notes: str = ""
    qualification: str
    reviewed: bool = False
    deck_confirmed: bool = False
    method_differences_accepted: bool = False
    revision: int | None = None


class PublishRequest(Payload):
    name: str = Field(min_length=1, max_length=300)


class SetupRequest(PublishRequest):
    setup: ProtocolSetup
    materials: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/protocol-assistant", response_class=HTMLResponse)
async def assistant_page():
    return HTMLResponse((Path(__file__).resolve().parents[3] / "frontend" / "protocol_assistant.html").read_text())


@router.get("/api/protocols/context")
async def context():
    return machine_context(_bravo())


@router.get("/api/protocols/capabilities")
async def capabilities():
    """Publish a sanitized, read-only capability manifest for the active profile."""
    from pybravo.workflow.protocols.capabilities import build_capability_manifest

    return build_capability_manifest(machine_context(_bravo()))


@router.get("/api/protocols/methods")
async def methods():
    """Read the versioned method library for the active machine and head."""
    from pybravo.workflow.protocols.methods import method_registry

    return method_registry(machine_context(_bravo()))


@router.post("/api/protocols/methods")
async def save_method(request: MethodSaveRequest):
    """Add a scientist-reviewed method version; never promote source material automatically."""
    from pybravo.workflow.protocols.methods import (
        MethodConflictError,
        MethodValidationError,
        save_reviewed_method,
    )

    try:
        return save_reviewed_method(
            machine_context(_bravo()), request.method,
            expected_registry_digest=request.expected_registry_digest,
            expected_method_revision=request.expected_method_revision,
        )
    except MethodConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except MethodValidationError as exc:
        raise HTTPException(422, {"message": str(exc), "missing_fields": exc.missing_fields}) from exc


@router.get("/api/protocols/recipes")
async def recipes():
    """Read composable planning patterns; none are execution qualifications."""
    from pybravo.workflow.protocols.recipes import recipe_catalog

    return recipe_catalog()


@router.post("/api/protocols/methods/lookup")
async def method_lookup(request: MethodQuery):
    """Return compatible methods and explicit differences without changing a draft."""
    from pybravo.workflow.protocols.methods import lookup_methods

    return lookup_methods(machine_context(_bravo()), request)


@router.post("/api/protocols/liquid-class-proposals")
async def liquid_class_proposals(request: LiquidClassProposalQuery):
    """Read cross-profile class settings as unverified planning references only."""
    from pybravo.workflow.protocols.liquid_class_proposals import propose_liquid_classes

    return propose_liquid_classes(machine_context(_bravo()), request)


@router.post("/api/protocols/setup-recommendations")
async def setup_recommendations(request: SetupRecommendationRequest):
    """Suggest reviewable setup choices without editing or approving a draft."""
    from pybravo.workflow.protocols.capabilities import build_capability_manifest
    from pybravo.workflow.protocols.setup_recommendations import recommend_setup

    if request.session_id:
        record = _record(request.session_id)
        selected = (record["selected_paragraph_ids"] if request.selected_paragraph_ids is None
                    else request.selected_paragraph_ids)
        try:
            source = IngestedProtocol.model_validate(record["source"]).select(selected).model_dump()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        if request.selected_paragraph_ids is not None:
            raise HTTPException(422, "A paragraph selection requires a session_id.")
        source = None
    bravo = _bravo()
    manifest = build_capability_manifest(machine_context(bravo))
    try:
        runtime_snapshot = bravo.get_state()
    except Exception as exc:
        logger.warning("Software deck snapshot unavailable for setup advice: %s", exc)
        runtime_snapshot = None
    return recommend_setup(request.plan.model_dump(), request.setup, manifest, source=source,
                           runtime_snapshot=runtime_snapshot)


@router.get("/api/protocols/library")
async def library():
    return {"items": _store.list("library")}


@router.get("/api/protocols/setups")
async def setups():
    return {"items": _store.list("setups")}


@router.post("/api/protocols/setups")
async def save_setup(request: SetupRequest):
    from pybravo.workflow.protocols.models import ProtocolMaterial
    materials = [ProtocolMaterial.model_validate(item).model_dump() for item in request.materials]
    return _store.put("setups", {"name": request.name, "setup": request.setup.model_dump(),
                                 "materials": materials, "context_hash": machine_context(_bravo())["context_hash"]})


@router.post("/api/protocols/library/{identity}/reuse")
async def reuse(identity: str):
    item = _store.get("library", identity)
    if not item:
        raise HTTPException(404, "Library protocol not found")
    record = _store.create_session(item["source"], setup=item["setup"], plan=item["plan"])
    return _store.annotate(record["id"], {"library_origin": identity,
        "selected_paragraph_ids": item["selected_paragraph_ids"]}, revision=record["revision"])


@router.post("/api/protocols/from-text")
async def from_text(request: TextRequest):
    try:
        source = ingest_text(request.text, source_name=request.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _store.create_session(source.model_dump())


@router.post("/api/protocols/ingest")
async def ingest(file: UploadFile = File(...)):
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "Upload a PDF no larger than 20 MB.")
    try:
        source = await ingest_pdf(data, filename=Path(file.filename or "protocol.pdf").name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _store.save_pdf(source.source_id, data)
    source.metadata["original_pdf"] = True
    return _store.create_session(source.model_dump())


def _chat_preview(record: dict, capabilities: dict | None = None) -> dict:
    from pybravo.workflow.protocols.preview import build_chat_preview

    preview = build_chat_preview(
        record["plan"], record["id"], record["revision"], sources=_sources(record),
        setup=record.get("setup") or {},
    )
    preview.setdefault("questions", preview.get("protocol_questions", []))
    machine = capabilities if capabilities is not None else machine_context(_bravo())
    preview["head_type"] = machine.get("head_type")
    preview["tipbox_choices"] = machine.get("tipbox_choices", [])
    preview["tipbox_catalog_candidates"] = machine.get("tipbox_catalog_candidates", [])
    preview["tipbox_choices_reason"] = machine.get("tipbox_choices_reason", "")
    return preview


def _chat_reply(plan: ProtocolPlan, preview: dict) -> str:
    """Describe the proposed plan without implying scientific or run approval."""
    count = len(plan.steps)
    reply = f"Proposed {count} {'step' if count == 1 else 'steps'} for review."
    questions = preview.get("questions", [])
    if questions:
        prompts = [str(q.get("prompt") or "").strip() for q in questions[:3]]
        prompts = [prompt for prompt in prompts if prompt]
        if prompts:
            reply += " " + " ".join(prompts)
    else:
        reply += " Review the steps and complete the instrument setup before validation."
    return reply


@router.post("/api/protocols/chat")
async def chat(request: ChatRequest):
    """Propose a cited protocol revision; this never approves or runs it."""
    from pybravo.workflow.protocols.llm import extract_protocol_plan

    message = request.message.strip()
    if not message:
        raise HTTPException(422, "Enter a message before sending it.")
    record = _record(request.session_id) if request.session_id is not None else None
    if record is not None:
        if request.revision is None or request.revision != record["revision"]:
            raise HTTPException(409, "The current revision is required. Reload the conversation before sending another message.")
        source = IngestedProtocol.model_validate(record["source"])
        messages = copy.deepcopy(record.get("chat_messages") or [])
        selected = list(record["selected_paragraph_ids"])
        identity = record["id"]
    else:
        if request.revision is not None:
            raise HTTPException(422, "A revision requires an existing session_id.")
        identity = str(uuid.uuid4())
        source = IngestedProtocol(source_id="chat-" + identity, name="Designer conversation", paragraphs=[],
                                  metadata={"parser": "chat"})
        messages = []
        selected = []
    if sum(len(p.text) for p in source.paragraphs) + len(message) > MAX_TEXT_CHARS:
        raise HTTPException(422, "The conversation is too long. Start a new protocol conversation.")
    paragraph_id = source.source_id + "-chat-" + uuid.uuid4().hex[:12]
    source.paragraphs.append(SourceParagraph(id=paragraph_id, text=message, kind="chat_message", section="Scientist conversation"))
    selected.append(paragraph_id)
    messages.append({"role": "user", "content": message, "time": now(), "source_paragraph_id": paragraph_id})
    try:
        capabilities = machine_context(_bravo())
        result = await extract_protocol_plan(source.select(selected), context={
            **capabilities, "setup": record["setup"] if record else {},
            "current_plan": record.get("plan") if record else None,
            "chat_messages": messages,
            "instructions": (
                "Update the entire proposed protocol using this ordered conversation. "
                "Later scientist messages correct earlier requests; retain earlier unaffected steps. "
                "Cite the user-message paragraph that actually supplies each detail. "
                "Assistant messages and the prior draft are context, never independent evidence. "
                "Preserve unknown values and ask short questions; do not claim approval or execution."
            ),
        })
        # The readable title follows the proposed protocol; citation identities
        # remain stable, and the rename is committed with this complete turn.
        proposed_name = result.plan.name.strip() or (record.get("name") if record else None) or "Designer conversation"
        result.plan.name = proposed_name
        source.name = proposed_name
        candidate = {**(record or {}), "id": identity, "revision": (record["revision"] + 1) if record else 1,
                     "name": proposed_name,
                     "source": source.model_dump(), "selected_paragraph_ids": selected,
                     "plan": result.plan.model_dump(), "setup": record["setup"] if record else {}}
        preview = _chat_preview(candidate, capabilities)
        reply = _chat_reply(result.plan, preview)
        messages.append({"role": "assistant", "content": reply, "time": now()})
        # Persist source, transcript and plan together, only after successful
        # extraction. A concurrent edit invalidates the pending response.
        if record is None:
            saved = _store.create_session(source.model_dump(), plan=result.plan.model_dump(),
                                          chat_messages=messages, model=result.metadata, identity=identity)
        else:
            saved = _store.update_session(identity, {
                "source": source.model_dump(), "selected_paragraph_ids": selected,
                "name": proposed_name,
                "plan": result.plan.model_dump(), "chat_messages": messages, "model": result.metadata,
            }, revision=request.revision, event="chat_revision")
        return {"session": saved, "reply": reply, "preview": preview}
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/protocols/{identity}/chat-preview")
async def chat_preview(identity: str):
    record = _record(identity)
    if not record.get("plan"):
        raise HTTPException(409, "This conversation has no proposed protocol yet.")
    preview = _chat_preview(record)
    replies = [item["content"] for item in record.get("chat_messages", []) if item.get("role") == "assistant"]
    return {"session": record, "reply": replies[-1] if replies else _chat_reply(ProtocolPlan.model_validate(record["plan"]), preview),
            "preview": preview}


@router.get("/api/protocols")
async def sessions():
    return {"items": [{key: record.get(key) for key in ("id", "name", "revision", "updated", "approval")}
                       for record in _store.list("sessions")]}


@router.get("/api/protocols/{identity}/source-pdf")
async def source_pdf(identity: str):
    source = _record(identity)["source"]
    path = _store.pdf_path(source["source_id"])
    if not source.get("metadata", {}).get("original_pdf") or not path.is_file():
        raise HTTPException(404, "This protocol has no stored source PDF.")
    return FileResponse(path, media_type="application/pdf", filename=source["name"], content_disposition_type="inline")


@router.get("/api/protocols/{identity}")
async def get_protocol(identity: str):
    record = _record(identity)
    sim = record.get("simulation") or {}
    if sim.get("status") == "running" and identity not in _simulations:
        record = _store.annotate(identity, {"simulation": {**sim, "status": "failed",
            "error": "Validation was interrupted by a server restart. Run strict simulation again."},
            "approval": None}, revision=record["revision"])
    return record


@router.patch("/api/protocols/{identity}")
async def edit_protocol(identity: str, request: EditRequest):
    record = _record(identity)
    changes = request.model_dump(exclude_unset=True, exclude={"revision"})
    if "plan" in changes and changes["plan"] is None:
        raise HTTPException(422, "Plan cannot be cleared; create a new protocol instead.")
    if "setup" in changes and changes["setup"] is None:
        raise HTTPException(422, "Setup must be an object.")
    if request.selected_paragraph_ids is not None:
        try:
            IngestedProtocol.model_validate(record["source"]).select(request.selected_paragraph_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    try:
        return _store.update_session(identity, changes, revision=request.revision)
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/protocols/{identity}/apply-reviewed-dead-volumes")
async def apply_reviewed_dead_volumes(identity: str, request: ApplyCatalogDeadVolumesRequest):
    """Fill only missing source residuals from reviewed catalog plate types.

    This records catalog provenance without claiming a scientist decision or
    confirming starting liquid, deck contents, tip inventory, or method safety.
    """
    from .dead_volume import seed_reviewed_source_dead_volumes

    record = _record(identity)
    if not record.get("plan"):
        raise HTTPException(409, "Extract or enter a protocol before applying catalog defaults.")
    context = machine_context(_bravo())
    candidate = ProtocolPlan.model_validate(record["plan"])
    seeded = seed_reviewed_source_dead_volumes(candidate, context)
    if seeded:
        plan = copy.deepcopy(record["plan"])
        for row in seeded:
            index = int(row["path"].split("/")[2])
            plan["materials"][index]["dead_volume_ul"] = row["value"]
        metadata = copy.deepcopy(record.get("model") or {})
        prior = metadata.get("catalog_defaults")
        metadata["catalog_defaults"] = (prior if isinstance(prior, list) else []) + [
            {**row, "catalog_context_hash": context.get("context_hash")} for row in seeded
        ]
        changes = {"plan": plan, "model": metadata}
    else:
        changes = {}
    try:
        return _store.update_session(identity, changes, revision=request.revision,
                                     event="reviewed_catalog_dead_volume_defaults")
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/protocols/{identity}/extract")
async def extract(identity: str, request: ExtractRequest = ExtractRequest()):
    from pybravo.workflow.protocols.llm import extract_protocol_plan
    record = _record(identity)
    if request.revision is not None and request.revision != record["revision"]:
        raise HTTPException(409, "Protocol changed; reload before extraction.")
    try:
        selected = (record["selected_paragraph_ids"] if request.selected_paragraph_ids is None
                    else request.selected_paragraph_ids)
        source = IngestedProtocol.model_validate(record["source"]).select(selected)
        capabilities = machine_context(_bravo())
        result = await extract_protocol_plan(source, context={**capabilities, "setup": record["setup"],
            "current_plan": record.get("plan"), "instructions": request.instructions})
        changes = {"plan": result.plan.model_dump(), "model": result.metadata,
                   "selected_paragraph_ids": [p.id for p in source.paragraphs]}
        return _store.update_session(identity, changes, revision=record["revision"], event="model_extraction")
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/protocols/{identity}/validate")
async def validate(identity: str):
    record = _record(identity)
    capabilities = machine_context(_bravo())
    report = _validate(record, capabilities)
    report.update(context_hash=capabilities["context_hash"],
                  record_hash=record_digest(record, capabilities["context_hash"]), checked_at=now())
    return _store.annotate(identity, {"validation": report, "issues": report.get("issues", [])}, revision=record["revision"])


async def _simulate(identity: str, record: dict, capabilities: dict, workflow: dict, run_id: str):
    from pybravo.deck.labware import InMemoryLabwareCatalog, LabwareDefinition
    from pybravo.profile.profile import BravoProfile
    from pybravo.workflow.executor import WorkflowExecutor
    events: list[dict] = []
    simulation = None
    executor = None
    async def event(payload):
        events.append({**payload, "time": now()})
    try:
        profile = copy.deepcopy(capabilities["profile"])
        # Simulation cannot open accessory serial ports, cameras, or hardware transports.
        profile["connection"]["controller_type"] = "simulation"
        profile["accessories"] = {}
        profile["vision"] = {"enabled": False}
        simulation = Bravo(profile=BravoProfile._from_dict(profile), mode="simulation")
        simulation._labware_catalog = InMemoryLabwareCatalog([
            LabwareDefinition(**row) for row in capabilities["labware"]
        ])
        # Strict validation needs every real task and state transition, but
        # waiting for simulated travel time can exceed the request budget on
        # an otherwise valid plate-transfer workflow.
        simulation.connect()
        simulation.controller.set_move_timing_enabled(False)
        executor = WorkflowExecutor(simulation, workflow["graph"], deck_config=workflow["deck"],
                                    preview_animation=False, strict_validation=True, on_event=event)
        await asyncio.wait_for(executor.execute(), timeout=180)
        failures = [e for e in events if e.get("type") in
                    {"workflow:error", "workflow:task_warning", "workflow:task_aborted"}]
        completed = any(e.get("type") == "workflow:complete" and e.get("status") == "ok" for e in events)
        if failures or not completed:
            raise ValueError(failures[0].get("error", "Strict simulation did not complete") if failures
                             else "Strict simulation did not complete")
        result = {"status": "passed", "run_id": run_id, "events": events, "finished_at": now()}
    except Exception as exc:
        if executor:
            executor.abort()
        result = {"status": "failed", "run_id": run_id, "events": events, "error": str(exc) or type(exc).__name__,
                  "finished_at": now()}
    finally:
        if simulation is not None:
            simulation.disconnect()
    result.update(context_hash=capabilities["context_hash"],
                  record_hash=record_digest(record, capabilities["context_hash"]),
                  workflow_hash=workflow_digest(workflow))
    try:
        _store.annotate(identity, {"simulation": result, "approval": None}, revision=record["revision"])
    except RevisionConflict:
        logger.info("Discarded stale simulation for protocol %s", identity)
    finally:
        _simulations.pop(identity, None)


@router.post("/api/protocols/{identity}/simulate")
async def simulate(identity: str):
    if identity in _simulations:
        raise HTTPException(409, "Strict simulation is already running.")
    record = _record(identity)
    capabilities = machine_context(_bravo())
    report = _validate(record, capabilities)
    if not _valid(report):
        raise HTTPException(409, {"message": "Resolve the validation issues first.", "issues": report["issues"]})
    workflow = _compile(record, capabilities)
    run_id = str(uuid.uuid4())
    record = _store.annotate(identity, {"validation": report, "issues": [], "approval": None,
        "simulation": {"status": "running", "run_id": run_id, "started_at": now()}}, revision=record["revision"])
    _simulations[identity] = asyncio.create_task(_simulate(identity, record, capabilities, workflow, run_id))
    return record


def _require_passed(record: dict, capabilities: dict) -> tuple[str, dict]:
    fingerprint = record_digest(record, capabilities["context_hash"])
    simulation = record.get("simulation") or {}
    if simulation.get("status") != "passed" or simulation.get("record_hash") != fingerprint:
        raise HTTPException(409, "This version and machine configuration need a successful strict simulation.")
    report = _validate(record, capabilities)
    if not _valid(report):
        raise HTTPException(409, "Protocol has unresolved validation issues.")
    workflow = _compile(record, capabilities)
    if simulation.get("workflow_hash") != workflow_digest(workflow):
        raise HTTPException(409, "The generated workflow changed since simulation. Run strict simulation again.")
    return fingerprint, workflow


@router.get("/api/protocols/{identity}/designer-preview")
async def designer_preview(identity: str):
    """Return the simulated compiler graph for inspection, without releasing it.

    The preview is rebuilt from the saved revision and current catalog. It is
    deliberately never persisted as a Designer workflow or approval record.
    """
    record = _record(identity)
    capabilities = machine_context(_bravo())
    fingerprint, workflow = _require_passed(record, capabilities)
    workflow = copy.deepcopy(workflow)
    workflow.update(
        protocol_compiled_preview=True,
        protocol_session_id=identity,
        protocol_revision=record["revision"],
    )
    return {
        "workflow": workflow,
        "preview": {
            "status": "simulation_only" if (record.get("setup") or {}).get("simulation_only_liquid_assumption")
                      else "unapproved",
            "session_id": identity,
            "revision": record["revision"],
            "context_hash": capabilities["context_hash"],
            "record_hash": fingerprint,
            "workflow_hash": workflow_digest(workflow),
            "read_only": True,
            "executable": False,
            "approved": False,
        },
    }


def _require_releasable_liquid_method(record: dict) -> None:
    if (record.get("setup") or {}).get("simulation_only_liquid_assumption") is not None:
        raise HTTPException(
            409,
            "This protocol uses a software-simulation-only liquid assumption and cannot be approved or released. "
            "Replace it with a reviewed liquid method, then validate and simulate the revised protocol.",
        )


@router.post("/api/protocols/{identity}/approve")
async def approve(identity: str, request: ApprovalRequest):
    record = _record(identity)
    if request.revision is not None and request.revision != record["revision"]:
        raise HTTPException(409, "Protocol changed; reload before approval.")
    _require_releasable_liquid_method(record)
    if not request.scientist.strip() or not request.reviewed or not request.deck_confirmed:
        raise HTTPException(422, "Record the reviewer and confirm the protocol and deck review.")
    if request.qualification not in {"qualification_run", "previously_qualified", "scientist_reviewed_simulated"}:
        raise HTTPException(422, "Choose a release basis for this procedure.")
    if request.qualification == "previously_qualified" and not request.notes.strip():
        raise HTTPException(422, "Record the previous qualification reference in the notes.")
    if request.qualification == "scientist_reviewed_simulated" and not request.notes.strip():
        raise HTTPException(422, "Record the rationale for the scientist-reviewed adaptation.")
    capabilities = machine_context(_bravo())
    fingerprint, workflow = _require_passed(record, capabilities)
    report = _validate(record, capabilities)
    paired_fallback = any(
        str(issue.get("code") or "").startswith("distribute_")
        and str(issue.get("code") or "").endswith("_fallback")
        for issue in report.get("issues", [])
    )
    if paired_fallback:
        if not request.method_differences_accepted:
            raise HTTPException(422, "Review and accept the changed aspiration sequence before approval.")
        if not request.notes.strip():
            raise HTTPException(422, "Record why the paired aspiration-dispense fallback is acceptable.")
    if any(issue.get("code") == "method_mismatch" for issue in report.get("issues", [])):
        if not request.method_differences_accepted:
            raise HTTPException(422, "Review and accept every listed method difference before approval.")
        if not request.notes.strip():
            raise HTTPException(422, "Record why the selected method differences are acceptable.")
    approval = {**request.model_dump(), "approved_at": now(), "record_hash": fingerprint,
                "workflow_hash": workflow_digest(workflow), "context_hash": capabilities["context_hash"]}
    return _store.annotate(identity, {"approval": approval}, revision=record["revision"])


def _require_approved(record: dict, capabilities: dict) -> dict:
    _require_releasable_liquid_method(record)
    fingerprint, workflow = _require_passed(record, capabilities)
    approval = record.get("approval") or {}
    if approval.get("record_hash") != fingerprint or approval.get("workflow_hash") != workflow_digest(workflow):
        raise HTTPException(409, "Review and approve this exact protocol version first.")
    return workflow


@router.post("/api/protocols/{identity}/export-workflow")
async def export_workflow(identity: str):
    record = _record(identity)
    workflow = _require_approved(record, machine_context(_bravo()))
    workflow["protocol_session_id"] = identity
    workflow["protocol_revision"] = record["revision"]
    saved = _server()._get_workflow_storage().create_workflow(workflow)
    _store.put("releases", {"id": saved["id"], "session_id": identity, "revision": record["revision"],
        "workflow_hash": workflow_digest(saved), "approval": record["approval"]})
    return {"workflow_id": saved["id"], "name": saved["name"], "url": f"/designer?workflow={saved['id']}"}


@router.post("/api/protocols/{identity}/publish")
async def publish(identity: str, request: PublishRequest):
    record = _record(identity)
    _require_approved(record, machine_context(_bravo()))
    return _store.put("library", {"name": request.name, "source": record["source"], "plan": record["plan"],
        "setup": record["setup"], "selected_paragraph_ids": record["selected_paragraph_ids"],
        "source_session_id": identity, "source_revision": record["revision"], "approval": record["approval"],
        "model": record.get("model"), "history": record["history"]})


def check_execution_release(workflow_id: str, workflow: dict, bravo) -> dict | None:
    """Called before initialize or any motion; stored release survives stripped markers."""
    if workflow.get("protocol_compiled_preview"):
        raise HTTPException(409, "A compiled protocol preview is read-only and cannot run. Review and approve it in Protocol Assistant.")
    release = _store.get("releases", workflow_id)
    identity = workflow.get("protocol_session_id") or (release or {}).get("session_id")
    if not identity:
        if any("_protocol_step_id" in (node.get("properties") or {})
               for node in (workflow.get("graph") or {}).get("nodes", [])):
            raise HTTPException(409, "A compiled protocol graph needs an approved release before execution.")
        return None
    record = _record(identity)
    capabilities = machine_context(bravo)
    expected = _require_approved(record, capabilities)
    if workflow_digest(workflow) != workflow_digest(expected):
        raise HTTPException(409, "This workflow differs from its reviewed protocol. Revalidate and approve it in Protocol Assistant.")
    if release and release["revision"] != record["revision"]:
        raise HTTPException(409, "This exported workflow is an older protocol revision.")
    if getattr(bravo, "_tips_on_head", False):
        raise HTTPException(409, "Start the reviewed protocol with an empty head; tips are managed by its workflow.")
    return {"session_id": identity, "revision": record["revision"], "workflow_id": workflow_id,
            "record_hash": record["approval"]["record_hash"], "started_at": now(), "events": []}


def record_execution(run: dict, event: dict):
    if event.get("type") in {"workflow:positions", "workflow:runtime_state", "workflow:vars_update"}:
        return
    run["events"].append({**event, "time": now()})
    if event.get("type") not in {"workflow:complete", "workflow:error"}:
        return
    record = _record(run["session_id"])
    runs = record.get("runs", []) + [{**run, "finished_at": now(), "result": event}]
    _store.annotate(record["id"], {"runs": runs}, revision=record["revision"])
