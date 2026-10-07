"""The pinned RNA grader has a distinct, partially persisted no-key result."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from scripts import verify_text2wetlab_container as runner

TASK = "opentrons-rna-extraction"
INSTRUCTION = b"Synthetic RNA instruction; use the supplied paper and labware.\n"
PAPER = b"Synthetic RNA paper for provenance only.\n"
LABWARE = b'{"ordering": [], "wells": {}}\n'
CODE = b"metadata = {'apiLevel': '2.15'}\n\ndef run(protocol):\n    pass\n"
LABWARE_NAME = "labware/thermo_96_wellplate_200ul.json"
RUBRIC_IDS = (
    "sample_handling", "binding_and_separation", "washes_and_drying",
    "elution_recovery", "fidelity_to_paper",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _saved_run(root: Path) -> Path:
    task_dir = root / TASK
    task_dir.mkdir(parents=True)
    candidate = task_dir / "protocol.py"
    candidate.write_bytes(CODE)
    (task_dir / "instruction.md").write_bytes(INSTRUCTION)
    (task_dir / "generation_trace.json").write_text(json.dumps({
        "task_sha256": _sha(INSTRUCTION),
        "status": "simulated",
        "static_validation_passed": True,
        "event_validation_passed": True,
        "scientific_source": {"source_sha256": _sha(PAPER)},
        "attempts": [{
            "number": 1, "model": "qwen-synthetic", "validation": "passed",
            "simulation": "passed", "code_sha256": _sha(CODE),
        }],
    }), encoding="utf-8")
    (root / "report.json").write_text(json.dumps({
        "dataset": runner.DATASET,
        "revision": runner.REVISION,
        "cases": [{
            "task": TASK,
            "instruction_sha256": _sha(INSTRUCTION),
            "source_paper_sha256": _sha(PAPER),
            "protocol_sha256": _sha(CODE),
            "generation": {"status": "passed"},
        }],
    }), encoding="utf-8")
    return candidate


def _source_files() -> dict[str, bytes]:
    """Mirror the RNA tree shape: there is no judge_layer.py or protocol_lint.py."""
    image_data = {"paper.txt": PAPER, LABWARE_NAME: LABWARE}
    return {
        "instruction.md": INSTRUCTION,
        "task.toml": b"name = 'synthetic RNA'\n",
        "environment/Dockerfile": b"FROM python:3.10-slim\n",
        "environment/data/paper.txt": PAPER,
        f"environment/data/{LABWARE_NAME}": LABWARE,
        "environment/solution_hint.py": b"# unused hint\n",
        "tests/test.sh": b"#!/bin/bash\npython /tests/grade.py\n",
        "tests/grade.py": b"# synthetic RNA grader\n",
        "tests/anti_hack.py": b"# synthetic anti-hack\n",
        "tests/data_hashes.json": json.dumps({
            name: _sha(content) for name, content in image_data.items()
        }).encode(),
        "tests/runlog.py": b"# synthetic runlog\n",
        "tests/honeypot.py": b"# synthetic honeypot\n",
        "tests/instruction.md": INSTRUCTION,
        "tests/reference_protocol.py": b"# synthetic reference\n",
    }


def _stub_hugging_face(monkeypatch, files: dict[str, bytes], *,
                       bad_oid: str | None = None) -> list[str]:
    prefix = f"tasks/harbor/{TASK}/"
    tree = [{
        "type": "file", "path": prefix + name, "size": len(content),
        "oid": "0" * 40 if name == bad_oid else _blob_oid(content),
    } for name, content in files.items()]
    fetched: list[str] = []

    def fake_json(url: str):
        fetched.append(url)
        if "/revision/" in url:
            return {"id": runner.DATASET, "sha": runner.REVISION}
        if "/tree/" in url:
            return tree
        raise AssertionError(f"Unexpected Hugging Face JSON request: {url}")

    def fake_bytes(url: str) -> bytes:
        fetched.append(url)
        marker = f"/{prefix}"
        assert marker in url, url
        return files[unquote(url.split(marker, 1)[1])]

    monkeypatch.setattr(runner, "_fetch_json", fake_json)
    monkeypatch.setattr(runner, "_fetch_bytes", fake_bytes)
    return fetched


def _docker_spy(monkeypatch, output: Path, candidate: Path, *,
                mode: str, image_data: dict[str, bytes] | None = None):
    calls: list[tuple[list[str], dict[str, str], int]] = []
    data = image_data or {"paper.txt": PAPER, LABWARE_NAME: LABWARE}

    def fake_docker(args: list[str], *, env: dict[str, str], timeout: int):
        calls.append((args, dict(env), timeout))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:synthetic-rna\n", b"")
        if args[:2] == ["run", "--rm"] and args[-1].startswith("/data/"):
            name = args[-1][len("/data/"):]
            return subprocess.CompletedProcess(args, 0, data[name], b"")
        if args[-2:] == ["bash", "/tests/test.sh"]:
            verifier = output / "verifier"
            assert verifier.is_dir()
            (verifier / "protocol.py").write_bytes(candidate.read_bytes())
            if mode in {"missing_key", "judged", "missing_events"}:
                if mode != "missing_events":
                    (verifier / "events.json").write_text("[]", encoding="utf-8")
                if mode in {"missing_key", "missing_events"}:
                    stderr = (b"anthropic.AnthropicError: The api_key client option must be "
                              b"set by setting the ANTHROPIC_API_KEY environment variable")
                    return subprocess.CompletedProcess(args, 1, b"", stderr)
            traps = [{"trap": "synthetic_trap", "detail": "synthetic"}] if mode == "trap" else []
            sim_ok = mode == "judged"
            reward = {
                "reward": 1.0 if sim_ok else 0.0,
                "sim_pass": 1.0 if sim_ok else 0.0,
                "judge_mean": 1.0 if sim_ok else 0.0,
                "suspicious_code": 0.0,
                "judge_error": 0.0,
                "hack_detected": float(bool(traps)),
            }
            record = {
                "judge_model": "claude-sonnet-5-5",
                "suspicious_tokens": [],
                "traps": traps,
                "simulation": ({"ok": True, "n_commands": 2} if sim_ok else
                               {"ok": False, "error": "reward-hacking trap tripped" if traps
                                else "synthetic simulator failure"}),
                "rewards": reward,
            }
            if sim_ok:
                record["judge"] = {
                    "scores": dict.fromkeys(RUBRIC_IDS, 1.0),
                    "items": [{"id": name, "score": 1, "evidence": "synthetic"}
                              for name in RUBRIC_IDS],
                    "summary": "Synthetic judge result.",
                }
                reward.update({f"rubric_{name}": 1.0 for name in RUBRIC_IDS})
            else:
                record["error"] = "simulator gate failed"
            (verifier / "judge.json").write_text(json.dumps(record), encoding="utf-8")
            (verifier / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(runner, "_docker", fake_docker)
    return calls


def _mounts(args: list[str]) -> list[str]:
    return [args[index + 1] for index, item in enumerate(args[:-1]) if item == "--mount"]


def test_rna_no_key_partial_evidence_is_incomplete_and_keeps_data_read_only(
    tmp_path, monkeypatch,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    fetched = _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "rna-no-key"
    calls = _docker_spy(monkeypatch, output, candidate, mode="missing_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded-either")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "pre_judge_incomplete"
    assert result["pre_judge_gates"]["status"] == "incomplete"
    assert result["judge"]["status"] == "unavailable_no_key"
    assert result["official_score"] is None
    assert result["candidate_sha256"] == _sha(CODE)
    assert result["graded_copy_sha256"] == _sha(CODE)
    assert result["verifier_events_sha256"] == _sha(b"[]")
    assert result["saved_candidate_provenance"]["source_paper_sha256"] == _sha(PAPER)
    assert result["pinned_sources_sha256"]["environment/data/paper.txt"] == _sha(PAPER)
    assert result["pinned_sources_sha256"][f"environment/data/{LABWARE_NAME}"] == _sha(LABWARE)
    assert result["task_data_sha256"] == {"paper.txt": _sha(PAPER), LABWARE_NAME: _sha(LABWARE)}
    assert candidate.read_bytes() == CODE
    assert not (output / "verifier" / "judge.json").exists()
    assert not (output / "verifier" / "reward.json").exists()
    assert len(fetched) == len(files) + 2
    assert len(calls) == 5  # build, inspect, two /data reads, pinned test.sh
    grader_args, grader_env, _ = calls[-1]
    assert grader_args[-2:] == ["bash", "/tests/test.sh"]
    assert grader_args[grader_args.index("--network") + 1] == "none"
    assert "-e" not in grader_args
    mounts = _mounts(grader_args)
    assert f"type=bind,source={output / 'pinned' / TASK / 'tests'},target=/tests,readonly" in mounts
    assert f"type=bind,source={candidate},target=/app/protocol.py,readonly" in mounts
    assert f"type=bind,source={output / 'data'},target=/data,readonly" in mounts
    assert f"type=bind,source={output / 'verifier'},target=/logs/verifier" in mounts
    assert all("ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
               for _, env, _ in calls)
    assert all(args[args.index("--network") + 1] == "none" for args, _, _ in calls
               if args[:2] == ["run", "--rm"] and args[-1].startswith("/data/"))
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == result


def test_rna_full_judge_record_can_pass_pre_judge_gates_without_official_score(
    tmp_path, monkeypatch,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    _stub_hugging_face(monkeypatch, _source_files())
    output = tmp_path / "rna-judged"
    calls = _docker_spy(monkeypatch, output, candidate, mode="judged")
    secret = "synthetic-key-never-log"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output, judge=True,
    )

    assert result["status"] == "pre_judge_passed"
    assert result["pre_judge_gates"]["status"] == "passed"
    assert result["pre_judge_gates"]["anti_hack_traps"] == []
    assert result["pre_judge_gates"]["simulation"]["ok"] is True
    assert result["judge"]["status"] == "local_judge_returned"
    assert result["judge"]["scores"] == dict.fromkeys(RUBRIC_IDS, 1.0)
    assert result["official_score"] is None
    assert result["verifier_result_path"] == str(output / "verifier" / "judge.json")
    assert result["verifier_events_sha256"] == _sha(b"[]")
    grader_args, grader_env, _ = calls[-1]
    assert grader_args[grader_args.index("-e") + 1] == "ANTHROPIC_API_KEY"
    assert grader_env["ANTHROPIC_API_KEY"] == secret
    assert "OPENAI_API_KEY" not in grader_env
    assert "--network" not in grader_args
    assert all("ANTHROPIC_API_KEY" not in env for _, env, _ in calls[:-1])
    assert secret not in json.dumps(result)
    assert secret not in (output / "summary.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["trap", "simulation_failed"])
def test_rna_complete_trap_or_simulation_failure_is_not_a_judge_pass(
    tmp_path, monkeypatch, mode,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    _stub_hugging_face(monkeypatch, _source_files())
    output = tmp_path / mode
    _docker_spy(monkeypatch, output, candidate, mode=mode)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "pre_judge_failed"
    assert result["pre_judge_gates"]["status"] == "failed"
    assert result["judge"]["status"] == "not_reached"
    assert result["official_score"] is None
    assert result.get("verifier_events_sha256") is None
    if mode == "trap":
        assert result["pre_judge_gates"]["anti_hack_traps"]
    else:
        assert result["pre_judge_gates"]["anti_hack_traps"] == []
        assert result["pre_judge_gates"]["simulation"]["ok"] is False


def test_rna_partial_no_key_without_events_cannot_claim_pre_judge_pass(
    tmp_path, monkeypatch,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    _stub_hugging_face(monkeypatch, _source_files())
    output = tmp_path / "no-events"
    _docker_spy(monkeypatch, output, candidate, mode="missing_events")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] != "pre_judge_passed"
    assert result["official_score"] is None
    assert result.get("verifier_events_sha256") is None
    assert result["blocker"]["phase"] == "verifier_result"


@pytest.mark.parametrize("tamper", ["missing_custom_labware", "bad_blob_oid"])
def test_rna_unverified_pinned_tree_is_blocked_before_docker(
    tmp_path, monkeypatch, tamper,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    labware_path = f"environment/data/{LABWARE_NAME}"
    if tamper == "missing_custom_labware":
        files.pop(labware_path)
    _stub_hugging_face(monkeypatch, files,
                       bad_oid=labware_path if tamper == "bad_blob_oid" else None)
    output = tmp_path / tamper
    calls = _docker_spy(monkeypatch, output, candidate, mode="missing_key")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "blocked"
    assert result["blocker"]["phase"] == "pinned_dataset"
    assert result["official_score"] is None
    assert calls == []
