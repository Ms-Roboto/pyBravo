"""Local adapter/extraction tests use HTTP fixtures, never robot hardware."""

from __future__ import annotations

import json

import httpx
import pytest

from pybravo.workflow.protocols import llm
from pybravo.workflow.protocols.ingest import ingest_text
from pybravo.workflow.protocols.llm import (
    LocalLLMConfig,
    ProtocolGroundingError,
    ProtocolLLMError,
    StructuredResponse,
    extract_protocol_plan,
    structured_json,
)


def _completion(payload=None, *, content=None, finish_reason="stop"):
    return {"model": "qwen", "choices": [{"finish_reason": finish_reason, "message": {
        "role": "assistant", "content": content if content is not None else json.dumps(payload or {})
    }}], "usage": {"completion_tokens": 20}}


async def test_local_config_and_structured_http_contract(monkeypatch):
    monkeypatch.setenv("PYBRAVO_DRAFTER_BASE_URL", "http://model.local:8000/")
    monkeypatch.setenv("PYBRAVO_DRAFTER_MODEL", "test-qwen")
    monkeypatch.setenv("PYBRAVO_DRAFTER_ENABLE_THINKING", "false")
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=_completion({"missing": None}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await structured_json(
            [{"role": "user", "content": "Extract"}], {"type": "object"}, http_client=client,
        )
    assert str(seen[0].url) == "http://model.local:8000/v1/chat/completions"
    payload = json.loads(seen[0].content)
    assert payload["model"] == "test-qwen"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in payload
    assert result.payload == {"missing": None}
    assert result.metadata["provider"] == "local"
    assert result.metadata["settings"]["enable_thinking"] is False


async def test_http_retry_is_bounded_and_does_not_change_endpoint():
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(503) if len(urls) == 1 else httpx.Response(200, json=_completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await structured_json([], {}, config=LocalLLMConfig(retries=1), http_client=client)
    assert result.metadata["http_attempts"] == 2
    assert len(set(urls)) == 1
    assert "sparky.local:8000" in urls[0]


async def test_timeout_retries_are_bounded():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProtocolLLMError, match="No cloud fallback"):
            await structured_json([], {}, config=LocalLLMConfig(retries=1), http_client=client)
    assert len(calls) == 2


@pytest.mark.parametrize("exception,expected,not_expected", [
    (httpx.ConnectError("Connection refused"), "Could not connect", "request limit"),
    (httpx.ConnectTimeout("Connection timed out"), "Could not connect", "PYBRAVO_DRAFTER_TIMEOUT"),
    (httpx.ReadTimeout("Read timed out"), "120-second request limit", "Could not connect"),
    (TimeoutError(), "PYBRAVO_DRAFTER_TIMEOUT", "Could not connect"),
    (httpx.RemoteProtocolError("Server disconnected"), "RemoteProtocolError", "request limit"),
])
async def test_transport_failures_distinguish_connectivity_from_generation_timeout(exception, expected, not_expected):
    def handle(request):
        raise exception

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProtocolLLMError) as caught:
            await structured_json([], {}, config=LocalLLMConfig(retries=0), http_client=client)
    message = str(caught.value)
    assert expected in message
    assert not_expected not in message
    assert "1 attempt(s)" in message
    assert caught.value.__cause__ is exception


@pytest.mark.parametrize("envelope, expected", [
    (_completion(content="{incomplete", finish_reason="length"), "truncated"),
    (_completion(content="not json"), "malformed JSON"),
    ({"choices": [None]}, "invalid completion envelope"),
    (_completion(content="[]"), "malformed JSON"),
    (_completion(content="{}", finish_reason="tool_calls"), "did not finish"),
])
async def test_invalid_response_never_becomes_a_partial_plan(envelope, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))) as client:
        with pytest.raises(ProtocolLLMError, match=expected):
            await structured_json([], {}, config=LocalLLMConfig(retries=0), http_client=client)


def _plan(paragraph_id, **updates):
    result = {"name": "Unknown volume", "materials": [{"id": "destination", "name": "Plate", "labware_id": None}],
              "steps": [{"id": "step1", "kind": "manual", "description": "Add a small volume",
                         "source_paragraph_ids": [paragraph_id], "volume_ul": None}], "decisions": []}
    result.update(updates)
    return result


async def test_extraction_keeps_unknowns_and_manual_steps(monkeypatch):
    source = ingest_text("Manually add a small volume of buffer to the plate.")
    captured = []

    async def complete(messages, schema, **kwargs):
        captured.append(messages)
        return StructuredResponse(_plan(source.paragraphs[0].id), {"model": "qwen"})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context={"labware": []})
    assert result.plan.steps[0].volume_ul is None
    assert result.plan.materials[0].labware_id is None
    assert result.plan.steps[0].kind == "manual"
    assert result.plan.decisions == []
    assert result.metadata["source_paragraph_ids"] == [source.paragraphs[0].id]
    assert "Unknown values MUST remain null" in captured[0][0]["content"]


async def test_citation_repair_then_failure_is_bounded(monkeypatch):
    source = ingest_text("Manually add buffer.")
    calls = []

    async def complete(messages, schema, **kwargs):
        calls.append(list(messages))
        return StructuredResponse(_plan("made-up"), {"model": "qwen"})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="absent paragraph"):
        await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=1))
    assert len(calls) == 2
    assert "Repair these errors" in calls[-1][-1]["content"]


async def test_model_cannot_manufacture_scientist_decisions(monkeypatch):
    source = ingest_text("Add a small volume of buffer.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_plan(source.paragraphs[0].id, decisions=[{
            "path": "/steps/0/volume_ul", "value": 10, "reason": "assumed", "actor": "scientist",
        }]), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="manufacture scientist"):
        await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=0))


async def test_numeric_evidence_cannot_cite_a_different_step_paragraph(monkeypatch):
    source = ingest_text("Add buffer.\nMix 10 uL.")
    payload = _plan(source.paragraphs[0].id)
    payload["steps"][0]["source_values"] = [{"field": "volume_ul", "value": 10, "unit": "uL", "paragraph_id": source.paragraphs[1].id}]

    async def complete(*args, **kwargs):
        return StructuredResponse(payload, {})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="that step's source"):
        await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=0))


async def test_original_unit_evidence_is_repaired_without_guessing(monkeypatch):
    source = ingest_text("Centrifuge for 2 minutes.")
    calls = []

    async def complete(messages, schema, **kwargs):
        calls.append(list(messages))
        payload = _plan(source.paragraphs[0].id)
        payload["steps"][0]["duration_s"] = 120
        payload["steps"][0]["source_values"] = [{
            "field": "duration_s", "value": 120 if len(calls) == 1 else 2,
            "unit": "s" if len(calls) == 1 else "min", "paragraph_id": source.paragraphs[0].id,
        }]
        return StructuredResponse(payload, {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=1))
    assert len(calls) == 2
    assert "does not contain 120 s" in calls[-1][-1]["content"]
    assert result.plan.steps[0].duration_s == 120
    assert result.plan.steps[0].source_values[0].value == 2
    assert result.plan.steps[0].source_values[0].unit == "min"


async def test_manual_material_is_repaired_without_inventing_physical_setup(monkeypatch):
    source = ingest_text("Manually centrifuge the destination plate.")
    calls = []

    async def complete(messages, schema, **kwargs):
        calls.append(list(messages))
        payload = _plan(source.paragraphs[0].id)
        payload["steps"][0].update({
            "message": "Manually centrifuge the destination plate.",
            "material": "destination" if len(calls) == 1 else None,
        })
        return StructuredResponse(payload, {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=1))
    assert len(calls) == 2
    assert "material does not apply to manual; set it null" in calls[-1][-1]["content"]
    assert result.plan.steps[0].kind == "manual"
    assert result.plan.steps[0].material is None
    assert result.plan.steps[0].message == "Manually centrifuge the destination plate."
    assert result.plan.materials[0].labware_id is None
    assert result.plan.materials[0].deck_slot is None


async def test_malformed_json_can_be_repaired_once(monkeypatch):
    source = ingest_text("Manually add buffer.")
    calls = []

    async def complete(messages, schema, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            raise llm.ProtocolResponseError("malformed JSON")
        return StructuredResponse(_plan(source.paragraphs[0].id), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, config=LocalLLMConfig(repair_attempts=1))
    assert result.metadata["extraction_attempts"] == 2
    assert "complete JSON object" in calls[-1][-1]["content"]


async def test_legacy_local_provider_has_no_cloud_key_requirement(monkeypatch):
    from pybravo.workflow.drafter import llm as legacy
    from pybravo.workflow.drafter.facts import PaperFacts

    monkeypatch.setenv("PYBRAVO_DRAFTER_PROVIDER", "local")
    monkeypatch.setenv("PYBRAVO_DRAFTER_MODEL", "qwen")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []

    async def complete(messages, schema, **kwargs):
        seen.append(kwargs)
        return StructuredResponse({"facts": []}, {})

    monkeypatch.setattr(llm, "structured_json", complete)
    config = legacy._resolve_config()
    assert config.provider == "local"
    assert legacy._build_client(config.provider) is None
    result = await legacy._llm_structured(None, config, system="extract", user="protocol", response_model=PaperFacts)
    assert result.facts == []
    assert seen[0]["config"].model == "qwen"


def test_legacy_explicit_cloud_provider_does_not_silently_fallback(monkeypatch):
    from pybravo.workflow.drafter import llm as legacy

    monkeypatch.setenv("PYBRAVO_DRAFTER_PROVIDER", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(legacy.NoLLMCredentialsError, match="explicitly selected"):
        legacy._resolve_config()


def _tip_context():
    return {"head_type": "HT_96_D_200", "tipbox_choices": [
        {"labware_id": "rack-200", "labware_name": "Approved rack", "tip_definition_id": "tip-200",
         "tip_name": "Approved tip", "rows": 8, "cols": 12, "tip_capacity_ul": 200, "tip_length_mm": 50},
        {"labware_id": "rack-50", "labware_name": "Another rack", "tip_definition_id": "tip-50"},
    ], "tipbox_choices_reason": ""}


def _tips_plan(paragraph_id, *, labware_id="rack-200", tip_definition_id="tip-200", **tip_fields):
    payload = _plan(paragraph_id)
    payload["materials"] = [{"id": "tips", "name": "Tip supply", "role": "tips", "labware_id": labware_id,
                             "tip_definition_id": tip_definition_id, **tip_fields}]
    return payload


async def test_verified_tipbox_recommendation_has_catalog_provenance_not_source_evidence(monkeypatch):
    source = ingest_text("Inspect the plate and recommend compatible tips.")
    captured = []

    async def complete(messages, schema, **kwargs):
        captured.append(messages)
        return StructuredResponse(_tips_plan(source.paragraphs[0].id), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context=_tip_context())
    material = result.plan.materials[0]
    assert (material.labware_id, material.tip_definition_id) == ("rack-200", "tip-200")
    assert material.deck_slot is None and material.available_tips is None
    assert result.plan.decisions == []
    assert result.plan.steps[0].source_values == []
    assert result.plan.steps[0].source_paragraph_ids == [source.paragraphs[0].id]
    recommendation = result.metadata["catalog_recommendations"][0]
    assert recommendation["source"] == "active_head_tipbox_catalog"
    assert recommendation["scientist_confirmed"] is False
    assert recommendation["head_type"] == "HT_96_D_200"
    assert "Confirm the catalog recommendation rack-200" in result.plan.questions[-1].prompt
    assert "never create\nsource_values from catalog" in captured[0][0]["content"]


@pytest.mark.parametrize("labware_id,tip_id", [("invented-rack", "tip-200"), ("rack-200", "invented-tip"),
                                               ("rack-200", "tip-50"), ("rack-200", None)])
async def test_unverified_tipbox_pairs_are_rejected(monkeypatch, labware_id, tip_id):
    source = ingest_text("Recommend compatible tips.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id, labware_id=labware_id, tip_definition_id=tip_id), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="one exact labware_id/tip_definition_id pair"):
        await extract_protocol_plan(source, context=_tip_context(), config=LocalLLMConfig(repair_attempts=0))


@pytest.mark.parametrize("tip_fields", [{"deck_slot": 3}, {"available_tips": ["A1"]},
                                        {"initial_volume_ul": 0}, {"dead_volume_ul": 0}, {"well_volumes_ul": {"A1": 200}}])
async def test_tipbox_recommendations_do_not_establish_deck_or_inventory(monkeypatch, tip_fields):
    source = ingest_text("Recommend compatible tips.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id, **tip_fields), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="does not establish placement or inventory"):
        await extract_protocol_plan(source, context=_tip_context(), config=LocalLLMConfig(repair_attempts=0))


async def test_existing_tip_pair_is_preserved_until_scientist_explicitly_selects_another(monkeypatch):
    source = ingest_text("Inspect the plate.")
    context = _tip_context()
    context["current_plan"] = _tips_plan(source.paragraphs[0].id)

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id, labware_id="rack-50", tip_definition_id="tip-50"), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    with pytest.raises(ProtocolGroundingError, match="preserve the already specified tipbox pair"):
        await extract_protocol_plan(source, context=context, config=LocalLLMConfig(repair_attempts=0))

    selected_source = ingest_text("Use catalog rack-50 with tip-50.")

    async def selected(*args, **kwargs):
        return StructuredResponse(_tips_plan(selected_source.paragraphs[0].id, labware_id="rack-50", tip_definition_id="tip-50"), {})

    monkeypatch.setattr(llm, "structured_json", selected)
    result = await extract_protocol_plan(selected_source, context=context)
    assert result.plan.materials[0].labware_id == "rack-50"
    assert result.metadata["catalog_recommendations"] == []
    assert result.plan.decisions == []


async def test_tipbox_repair_uses_verified_choices_without_fabricating_facts(monkeypatch):
    source = ingest_text("Recommend compatible tips.")
    calls = []

    async def complete(messages, schema, **kwargs):
        calls.append(list(messages))
        return StructuredResponse(_tips_plan(source.paragraphs[0].id,
            labware_id="invented-rack" if len(calls) == 1 else "rack-200"), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context=_tip_context(), config=LocalLLMConfig(repair_attempts=1))
    assert len(calls) == 2
    assert "one exact labware_id/tip_definition_id pair" in calls[-1][-1]["content"]
    assert result.plan.materials[0].labware_id == "rack-200"
    assert result.plan.steps[0].source_values == []


async def test_empty_compatible_tip_choices_leave_ids_unknown(monkeypatch):
    source = ingest_text("Recommend compatible tips.")
    captured = []

    async def complete(messages, schema, **kwargs):
        captured.append(messages)
        return StructuredResponse(_tips_plan(source.paragraphs[0].id, labware_id=None, tip_definition_id=None), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context={"tipbox_choices": [], "tipbox_choices_reason": "No verified pair for this head."})
    assert result.plan.materials[0].labware_id is None
    assert result.metadata["catalog_recommendations"] == []
    assert "No verified pair for this head." in captured[0][1]["content"]


async def test_explicit_unverified_tipbox_ids_remain_in_review_draft_without_recommendation(monkeypatch):
    source = ingest_text("Use rack-384 with tip-st10; the catalog metadata needs checking.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id,
            labware_id="rack-384", tip_definition_id="tip-st10"), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context={"tipbox_choices": []})
    assert result.plan.materials[0].labware_id == "rack-384"
    assert result.metadata["catalog_recommendations"] == []


async def test_catalog_change_does_not_make_unrelated_chat_revision_drop_prior_tip_choice(monkeypatch):
    source = ingest_text("Inspect the plate after transfer.")
    existing = _tips_plan(source.paragraphs[0].id)

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context={"tipbox_choices": [], "current_plan": existing})
    assert result.plan.materials[0].labware_id == "rack-200"
    assert result.metadata["catalog_recommendations"] == []


async def test_scientist_can_supply_one_tipbox_id_without_fabricating_its_mate(monkeypatch):
    source = ingest_text("The rack ID is rack-384; I will check its tip definition later.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id,
            labware_id="rack-384", tip_definition_id=None), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context={"tipbox_choices": []})
    assert result.plan.materials[0].labware_id == "rack-384"
    assert result.plan.materials[0].tip_definition_id is None
    assert result.metadata["catalog_recommendations"] == []


def test_tipbox_guidance_is_bounded_and_drops_unrelated_catalog_fields():
    supplied = {"tipbox_choices": [{"labware_id": f"rack-{i}", "tip_definition_id": f"tip-{i}", "unrelated": "x" * 1000}
                                  for i in range(50)], "tipbox_choices_reason": "x" * 2000}
    result = llm._tipbox_context(supplied)
    assert len(result["tipbox_choices"]) == 32
    assert all("unrelated" not in row for row in result["tipbox_choices"])
    assert len(result["tipbox_choices_reason"]) == 1000
    assert len(supplied["tipbox_choices"]) == 50


def test_bounded_tip_guidance_retains_explicit_and_existing_selections():
    supplied = {"tipbox_choices": [{"labware_id": f"rack-{i}", "tip_definition_id": f"tip-{i}"} for i in range(50)],
                "current_plan": {"materials": [{"id": "tips", "labware_id": "rack-48", "tip_definition_id": "tip-48"}]}}
    result = llm._tipbox_context(supplied, ingest_text("Use rack-49 with tip-49."))
    assert len(result["tipbox_choices"]) == 32
    assert result["tipbox_choices"][0]["labware_id"] == "rack-49"
    assert result["tipbox_choices"][1]["labware_id"] == "rack-48"


def _quadrant_fixture():
    from pybravo.workflow.protocols.models import ProtocolPlan

    source = ingest_text(
        "I have 4 384 well plates and I want to transfer 5ul from each plate "
        "into the 4 quadrants of two 1536 plates. I can't have any cross "
        "contamination. Please help me layout the deck and write the protocol."
    )
    citation = source.paragraphs[0].id
    materials = [
        {"id": f"src{i}", "name": f"Source {i}", "deck_slot": 9, "stack_order": i - 1}
        for i in range(1, 5)
    ]
    materials.extend([
        {"id": "dest1", "name": "1536 destination 1", "deck_slot": 5},
        {"id": "dest2", "name": "1536 destination 2", "deck_slot": 8},
    ])
    materials.extend([
        {"id": f"tip{i}", "name": f"ST10 rack for source {i}", "role": "tips",
         "labware_id": "rack-384", "tip_definition_id": "st_10ul", "deck_slot": 5 - i}
        for i in range(4, 0, -1)
    ])
    steps = []
    for index, source_id in enumerate(("src4", "src3", "src2", "src1")):
        quadrant = {"src1": "A1", "src2": "A2", "src3": "B1", "src4": "B2"}[source_id]
        steps.append({"id": f"destack{index}", "kind": "destack_plate" if index < 3 else "move_plate", "material": source_id,
                      "destination_slot": 6, "source_paragraph_ids": [citation]})
        for destination_id in ("dest1", "dest2"):
            steps.append({"id": f"transfer{index}{destination_id}", "kind": "transfer",
                          "source": source_id, "destination": destination_id,
                          "source_anchor": "A1", "destination_anchor": quadrant,
                          "volume_ul": 5, "source_paragraph_ids": [citation],
                          "source_values": [{"field": "volume_ul", "value": 5, "unit": "uL",
                                             "paragraph_id": citation}]})
        steps.append({"id": f"park{index}", "kind": "move_plate" if index == 0 else "stack_plate",
                      "material": source_id, "destination_slot": 7,
                      "source_paragraph_ids": [citation]})
    plan = ProtocolPlan.model_validate({"name": "Four-source quadrant transfer",
                                        "materials": materials, "steps": steps})
    context = {"head_type": "HT_384_D_70", "tipbox_choices": [
        {"labware_id": "rack-384", "tip_definition_id": "st_10ul", "wells": 384,
         "execution_ready": True},
        {"labware_id": "rack-384", "tip_definition_id": "st_70ul", "wells": 384,
         "execution_ready": False},
    ]}
    return source, plan, context


def test_quadrant_layout_guard_preserves_four_sources_four_racks_two_destinations():
    source, plan, context = _quadrant_fixture()
    assert llm._check_quadrant_materials(plan, source, context) == []
    plan.materials = [material for material in plan.materials if material.id != "tip2"]
    assert any("4 distinct tip-rack materials" in issue for issue in llm._check_quadrant_materials(plan, source, context))
    source, plan, context = _quadrant_fixture()
    plan.steps = [step for step in plan.steps if step.source != "src1"]
    assert any("4 distinct source-plate materials" in issue for issue in llm._check_quadrant_materials(plan, source, context))


def test_quadrant_layout_guard_requires_same_disjoint_quadrants_and_st10():
    source, plan, context = _quadrant_fixture()
    target = next(step for step in plan.steps if step.source == "src4" and step.destination == "dest2")
    target.destination_anchor = "A1"
    assert any("one of A1/A2/B1/B2 on both" in issue for issue in llm._check_quadrant_materials(plan, source, context))
    source, plan, context = _quadrant_fixture()
    rack = next(material for material in plan.materials if material.id == "tip4")
    rack.tip_definition_id = "st_70ul"
    assert any("ST70 is unsuitable" in issue for issue in llm._check_quadrant_materials(plan, source, context))


def test_quadrant_layout_guard_requires_top_first_staging_and_processed_stack():
    source, plan, context = _quadrant_fixture()
    bottom_access = next(step for step in plan.steps if step.id == "destack3")
    bottom_access.kind = "destack_plate"
    assert any("src1 needs move_plate" in issue for issue in llm._check_quadrant_materials(plan, source, context))
    source, plan, context = _quadrant_fixture()
    first_park = next(step for step in plan.steps if step.id == "park0")
    first_park.destination_slot = 5
    assert any("initially empty processed-stack slot" in issue for issue in llm._check_quadrant_materials(plan, source, context))


def test_explicit_st70_is_preserved_for_validator_to_flag():
    source, plan, context = _quadrant_fixture()
    source.paragraphs[0].text += " Use ST70 tips."
    for rack in (material for material in plan.materials if material.role == "tips"):
        rack.tip_definition_id = "st_70ul"
    assert llm._check_quadrant_materials(plan, source, context) == []


def test_quadrant_isolation_questions_show_top_first_rack_pairing_and_alignment():
    source, plan, _ = _quadrant_fixture()
    llm._add_isolated_source_setup_questions(plan, source)
    prompts = {question.path: question.prompt for question in plan.questions}
    assert "fresh_each_source" in prompts["/setup/tip_strategy"]
    assert "src4 → tip4" in prompts["/setup/tip_rack_ids"]
    assert "src1 → tip1" in prompts["/setup/tip_rack_ids"]
    assert "two destinations" in prompts["/setup/tip_reuse_reason"]
    assert "own now-empty rack" in prompts["/setup/tip_disposal_id"]
    assert sum("alignment and teachpoint" in prompt for prompt in prompts.values()) == 2


async def test_requested_deck_layout_may_propose_tipbox_slot_for_review(monkeypatch):
    source = ingest_text("Please help me layout the deck with a compatible tip rack.")

    async def complete(*args, **kwargs):
        return StructuredResponse(_tips_plan(source.paragraphs[0].id, deck_slot=3), {})

    monkeypatch.setattr(llm, "structured_json", complete)
    result = await extract_protocol_plan(source, context=_tip_context())
    assert result.plan.materials[0].deck_slot == 3
    assert result.metadata["catalog_recommendations"][0]["deck_slot_proposal"] == 3
    assert "confirm proposed deck slot 3" in result.plan.questions[-1].prompt


def _catalog_quadrant_context():
    return {
        "head_type": "HT_384_D_70", "has_gripper": True,
        "labware": [
            {"id": "source-384", "name": "Verified 384 microplate", "base_class": "microplate",
             "wells": 384, "rows": 16, "cols": 24, "spacing_x_mm": 4.5,
             "spacing_y_mm": 4.5, "well_volume_ul": 130},
            {"id": "destination-1536", "name": "Verified 1536 microplate", "base_class": "microplate",
             "wells": 1536, "rows": 32, "cols": 48, "spacing_x_mm": 2.25,
             "spacing_y_mm": 2.25, "well_volume_ul": 5.5},
        ],
        "tipbox_choices": [{"labware_id": "st10-rack", "labware_name": "384 ST rack",
                            "tip_definition_id": "st_10ul", "tip_name": "ST10",
                            "rows": 16, "cols": 24, "wells": 384, "tip_capacity_ul": 10,
                            "execution_ready": True}],
    }


async def test_exact_four_source_quadrant_request_uses_catalog_template_without_model(monkeypatch):
    source = ingest_text(
        "I have 4 384 well plates and I want to transfer 5ul from each plate "
        "into the 4 quadrants of two 1536 plates.  i can't have any cross "
        "contamination. Please help me layout the deck and write the protocol "
        "to do the transfer."
    )

    async def model_must_not_be_called(*args, **kwargs):
        raise AssertionError("The exact catalog-backed pattern must not call the local model")

    monkeypatch.setattr(llm, "structured_json", model_must_not_be_called)
    result = await extract_protocol_plan(source, context=_catalog_quadrant_context())
    plan = result.plan
    assert result.metadata["provider"] == "catalog_template"
    assert result.metadata["http_attempts"] == 0
    assert len(plan.materials) == 10
    assert [(material.id, material.deck_slot, material.stack_order) for material in plan.materials[:4]] == [
        ("source_1", 9, 0), ("source_2", 9, 1), ("source_3", 9, 2), ("source_4", 9, 3),
    ]
    assert [(material.id, material.deck_slot, material.tip_definition_id) for material in plan.materials[6:]] == [
        ("tips_source_4", 1, "st_10ul"), ("tips_source_3", 2, "st_10ul"),
        ("tips_source_2", 3, "st_10ul"), ("tips_source_1", 4, "st_10ul"),
    ]
    assert [material.deck_slot for material in plan.materials[4:6]] == [5, 8]
    assert [step.kind for step in plan.steps[::4]] == [
        "destack_plate", "destack_plate", "destack_plate", "move_plate",
    ]
    assert [step.kind for step in plan.steps[3::4]] == [
        "move_plate", "stack_plate", "stack_plate", "stack_plate",
    ]
    transfers = [step for step in plan.steps if step.kind == "transfer"]
    assert len(transfers) == 8
    assert [(step.source, step.destination, step.destination_anchor) for step in transfers] == [
        (f"source_{number}", f"destination_{destination}", quadrant)
        for number, quadrant in ((4, "B2"), (3, "B1"), (2, "A2"), (1, "A1"))
        for destination in (1, 2)
    ]
    assert all(step.volume_ul == 5 and step.source_anchor == "A1" for step in transfers)
    assert all(step.source_values[0].paragraph_id == source.paragraphs[0].id for step in transfers)
    assert all(material.available_tips is None for material in plan.materials if material.role == "tips")
    assert all(material.initial_volume_ul is None for material in plan.materials if material.role == "liquid")
    assert plan.decisions == []
    assert len(result.metadata["catalog_recommendations"]) == 4
    assert {question.path for question in plan.questions} >= {
        "/setup/tip_strategy", "/setup/tip_rack_ids", "/setup/tip_reuse_reason",
        "/setup/tip_disposal_id", "/setup/liquid_class",
    }


async def test_quadrant_template_requires_unique_catalog_and_exact_request(monkeypatch):
    exact = ingest_text(
        "I have 4 384 well plates and I want to transfer 5ul from each plate "
        "into the 4 quadrants of two 1536 plates. i can't have any cross "
        "contamination. Please help me layout the deck and write the protocol "
        "to do the transfer."
    )
    context = _catalog_quadrant_context()
    context["labware"].append({**context["labware"][0], "id": "another-384"})
    calls = []

    async def model_called(*args, **kwargs):
        calls.append(True)
        raise llm.ProtocolResponseError("model attempted")

    monkeypatch.setattr(llm, "structured_json", model_called)
    with pytest.raises(llm.ProtocolResponseError, match="model attempted"):
        await extract_protocol_plan(exact, context=context, config=LocalLLMConfig(repair_attempts=0))
    assert calls == [True]
    calls.clear()
    context["labware"].pop()
    additional_instruction = ingest_text(exact.paragraphs[0].text + " Also mix each destination.")
    with pytest.raises(llm.ProtocolResponseError, match="model attempted"):
        await extract_protocol_plan(additional_instruction, context=context, config=LocalLLMConfig(repair_attempts=0))
    assert calls == [True]
