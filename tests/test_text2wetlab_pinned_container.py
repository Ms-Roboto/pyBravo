"""The pinned container runner must validate inputs before executing Docker."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from scripts import verify_text2wetlab_container as runner

TASK = "split-200ul-two-wells"
INSTRUCTION = b"Move 200 uL from a reservoir into two wells.\n"
CODE = b"metadata = {'apiLevel': '2.14'}\n\ndef run(protocol):\n    pass\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _saved_run(root: Path) -> Path:
    task_dir = root / TASK
    task_dir.mkdir(parents=True)
    candidate = task_dir / "protocol.py"
    candidate.write_bytes(CODE)
    (task_dir / "instruction.md").write_bytes(INSTRUCTION)
    trace = {
        "task_sha256": _sha(INSTRUCTION),
        "status": "simulated",
        "static_validation_passed": True,
        "event_validation_passed": True,
        "attempts": [{
            "number": 1, "model": "qwen", "validation": "passed",
            "simulation": "passed", "code_sha256": _sha(CODE),
        }],
    }
    (task_dir / "generation_trace.json").write_text(json.dumps(trace), encoding="utf-8")
    report = {
        "dataset": runner.DATASET,
        "revision": runner.REVISION,
        "cases": [{
            "task": TASK,
            "instruction_sha256": _sha(INSTRUCTION),
            "source_paper_sha256": None,
            "protocol_sha256": _sha(CODE),
            "generation": {"status": "passed"},
        }],
    }
    (root / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return candidate


def _source_files() -> dict[str, bytes]:
    """A small complete task tree; its contents never run on the host."""
    return {
        "instruction.md": INSTRUCTION,
        "task.toml": b"name = 'synthetic'\n",
        "environment/Dockerfile": b"FROM python:3.10-slim\n",
        "environment/solution_hint.py": b"# unused hint\n",
        "tests/test.sh": b"#!/bin/sh\nset -eu\n",
        "tests/grade.py": b"# synthetic grader\n",
        "tests/anti_hack.py": b"# synthetic anti-hack\n",
        "tests/data_hashes.json": b"{}",
        "tests/runlog.py": b"# synthetic runlog\n",
        "tests/judge_layer.py": b"# synthetic judge\n",
        "tests/protocol_lint.py": b"# synthetic lint\n",
        "tests/reference_protocol.py": b"# synthetic reference\n",
        "tests/rubric.json": b"{}",
        "tests/honeypot.py": b"# synthetic honeypot\n",
        "tests/instruction.md": INSTRUCTION,
    }


def _stub_hugging_face(monkeypatch, files: dict[str, bytes], *,
                       listed_oids: dict[str, str] | None = None,
                       revision: str | None = None) -> list[str]:
    prefix = f"tasks/harbor/{TASK}/"
    tree = [{
        "type": "file", "path": prefix + name, "size": len(content),
        "oid": (listed_oids or {}).get(name, _git_blob_oid(content)),
    } for name, content in files.items()]
    fetched: list[str] = []

    def fake_json(url: str):
        fetched.append(url)
        if "/revision/" in url:
            return {"id": runner.DATASET, "sha": revision or runner.REVISION}
        if "/tree/" in url:
            return tree
        raise AssertionError(f"Unexpected HF JSON request: {url}")

    def fake_bytes(url: str) -> bytes:
        fetched.append(url)
        marker = f"/{prefix}"
        assert marker in url, url
        return files[unquote(url.split(marker, 1)[1])]

    monkeypatch.setattr(runner, "_fetch_json", fake_json)
    monkeypatch.setattr(runner, "_fetch_bytes", fake_bytes)
    return fetched


def _docker_spy(monkeypatch, output_dir: Path, candidate: Path, *,
                image_data: bytes = b"", judge_error: str | None = None,
                gate_override: dict | None = None):
    calls: list[tuple[list[str], dict[str, str], int]] = []

    def fake_docker(args: list[str], *, env: dict[str, str], timeout: int):
        calls.append((args, dict(env), timeout))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"sha256:synthetic\n", stderr=b"")
        if args[:2] == ["run", "--rm"] and args[-1].startswith("/data/"):
            return subprocess.CompletedProcess(args, 0, stdout=image_data, stderr=b"")
        if args[-2:] == ["bash", "/tests/test.sh"]:
            # The official wrapper writes its evidence into the fresh verifier mount.
            verifier = output_dir / "verifier"
            assert verifier.is_dir()
            judge_requested = "-e" in args
            reward = {"reward": 0.8 if judge_requested and judge_error is None else 0.0,
                      "sim_pass": 1.0,
                      "lint_violations": 0.0, "hack_detected": 0.0}
            record = {
                "lint": [], "traps": [], "simulation": {"ok": True, "n_commands": 2},
                "judge": ({"error": judge_error, "model": "synthetic-judge"}
                          if judge_error is not None else
                          {"scores": {"accuracy": 0.8}, "model": "synthetic-judge"}
                          if judge_requested else
                          {"error": "missing judge key", "model": "synthetic-judge"}),
                "rewards": reward,
            }
            record.update(gate_override or {})
            if record["lint"]:
                record.pop("traps")
                record.pop("simulation")
                record.pop("judge")
            elif record["traps"]:
                record.pop("simulation")
                record.pop("judge")
            elif record["simulation"].get("ok") is not True:
                record.pop("judge")
            if record.get("simulation", {}).get("ok") is not True:
                reward["sim_pass"] = 0.0
            (verifier / "result.json").write_text(json.dumps(record), encoding="utf-8")
            (verifier / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
            if record.get("simulation", {}).get("ok") is True:
                (verifier / "events.json").write_text("[]", encoding="utf-8")
            (verifier / "protocol.py").write_bytes(candidate.read_bytes())
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner, "_docker", fake_docker)
    return calls


@pytest.mark.parametrize("tamper", ["candidate", "candidate_path", "saved_instruction",
                                    "report_hash", "trace_hash", "revision"])
def test_untrusted_saved_candidate_is_blocked_before_docker(tmp_path, monkeypatch, tamper):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "result"
    docker_calls = _docker_spy(monkeypatch, output, candidate)
    if tamper == "candidate":
        candidate.write_bytes(CODE + b"# changed\n")
    elif tamper == "candidate_path":
        candidate = tmp_path / "same-bytes.py"
        candidate.write_bytes(CODE)
    elif tamper == "saved_instruction":
        (saved / TASK / "instruction.md").write_bytes(b"changed\n")
    elif tamper == "report_hash":
        report_path = saved / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["cases"][0]["protocol_sha256"] = "0" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif tamper == "trace_hash":
        trace_path = saved / TASK / "generation_trace.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace["attempts"][0]["code_sha256"] = "0" * 64
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
    else:
        report_path = saved / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["revision"] = "different-revision"
        report_path.write_text(json.dumps(report), encoding="utf-8")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )
    assert result["status"] == "blocked"
    assert result["official_score"] is None
    assert docker_calls == []
    assert result["blocker"]["phase"] == "candidate_provenance"


@pytest.mark.parametrize("tamper", ["revision_response", "git_blob_oid", "source_bytes"])
def test_untrusted_pinned_dataset_is_blocked_before_docker(tmp_path, monkeypatch, tamper):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    oids = {"tests/anti_hack.py": "0" * 40} if tamper == "git_blob_oid" else None
    revision = "f" * 40 if tamper == "revision_response" else None
    if tamper == "source_bytes":
        files["instruction.md"] = b"changed pinned instruction\n"
    _stub_hugging_face(monkeypatch, files, listed_oids=oids, revision=revision)
    output = tmp_path / "result"
    docker_calls = _docker_spy(monkeypatch, output, candidate)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )
    assert result["status"] == "blocked"
    assert result["official_score"] is None
    assert docker_calls == []
    assert result["blocker"]["phase"] == (
        "candidate_provenance" if tamper == "source_bytes" else "pinned_dataset"
    )


def test_pre_judge_pass_uses_pinned_files_read_only_mounts_and_fresh_logs(
    tmp_path, monkeypatch,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    fetched = _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "fresh-output"
    docker_calls = _docker_spy(monkeypatch, output, candidate)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-forwarded-without-judge")
    monkeypatch.setenv("OPENAI_API_KEY", "never-forwarded")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "pre_judge_passed"
    assert result["pre_judge_gates"]["status"] == "passed"
    assert result["pre_judge_gates"]["lint_violations"] == []
    assert result["pre_judge_gates"]["anti_hack_traps"] == []
    assert result["judge"]["status"] == "unavailable_no_key"
    assert result["verifier_network"] == "none"
    assert result["official_score"] is None
    assert result["saved_candidate_provenance"]["status"] == "passed"
    assert result["candidate_sha256"] == _sha(CODE)
    assert result["graded_copy_sha256"] == _sha(CODE)
    assert candidate.read_bytes() == CODE
    assert result["pinned_sources_sha256"]["tests/anti_hack.py"] == _sha(
        files["tests/anti_hack.py"],
    )
    assert len(fetched) == len(files) + 2
    assert len(docker_calls) == 3
    assert docker_calls[0][0][0] == "build"
    assert docker_calls[1][0][:2] == ["image", "inspect"]
    grader_args = docker_calls[2][0]
    assert grader_args[-2:] == ["bash", "/tests/test.sh"]
    mounts = [grader_args[index + 1] for index, item in enumerate(grader_args[:-1])
              if item == "--mount"]
    assert len(mounts) == 4
    assert any(f"source={output / 'pinned' / TASK / 'tests'},target=/tests,readonly"
               in mount for mount in mounts)
    assert any(f"source={candidate},target=/app/protocol.py,readonly"
               in mount for mount in mounts)
    assert any(f"source={output / 'data'},target=/data,readonly"
               in mount for mount in mounts)
    assert any(f"source={output / 'verifier'},target=/logs/verifier" in mount
               and "readonly" not in mount for mount in mounts)
    assert all("ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
               for _, env, _ in docker_calls)
    assert "-e" not in grader_args
    assert grader_args[grader_args.index("--network") + 1] == "none"
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == result
    assert not (output / TASK / "protocol.py").exists()


def test_existing_output_is_rejected_before_fetch_or_docker(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    fetched = _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "old-output"
    output.mkdir()
    (output / "old-log.txt").write_text("stale", encoding="utf-8")
    docker_calls = _docker_spy(monkeypatch, output, candidate)

    with pytest.raises(ValueError, match="must be new"):
        runner.verify_saved_candidate(
            TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
        )
    assert fetched == []
    assert docker_calls == []
    assert (output / "old-log.txt").read_text(encoding="utf-8") == "stale"


def test_image_data_hash_mismatch_stops_before_grader(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    files["tests/data_hashes.json"] = json.dumps({"kit.dat": _sha(b"pinned data")}).encode()
    _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "data-mismatch"
    docker_calls = _docker_spy(monkeypatch, output, candidate, image_data=b"different data")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "blocked"
    assert result["blocker"]["phase"] == "task_data"
    assert result["official_score"] is None
    assert docker_calls[-1][0][-1] == "/data/kit.dat"
    assert not any(args[-2:] == ["bash", "/tests/test.sh"] for args, _, _ in docker_calls)


def test_verified_image_data_is_mounted_read_only(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    paper = b"Synthetic task data, not protocol content.\n"
    files["tests/data_hashes.json"] = json.dumps({"paper.txt": _sha(paper)}).encode()
    _stub_hugging_face(monkeypatch, files)
    output = tmp_path / "with-data"
    docker_calls = _docker_spy(monkeypatch, output, candidate, image_data=paper)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "pre_judge_passed"
    assert result["task_data_sha256"] == {"paper.txt": _sha(paper)}
    assert (output / "data" / "paper.txt").read_bytes() == paper
    data_args = next(args for args, _, _ in docker_calls if args[-1] == "/data/paper.txt")
    assert data_args[data_args.index("--network") + 1] == "none"
    grader_args = docker_calls[-1][0]
    assert f"type=bind,source={output / 'data'},target=/data,readonly" in grader_args


@pytest.mark.parametrize("gate_override", [
    {"lint": ["disallowed syntax"]},
    {"traps": [{"trap": "synthetic"}]},
    {"simulation": {"ok": False, "error": "synthetic simulator failure"}},
])
def test_pre_judge_failures_remain_separate_from_judge(tmp_path, monkeypatch, gate_override):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    _stub_hugging_face(monkeypatch, _source_files())
    output = tmp_path / "failed-gate"
    _docker_spy(monkeypatch, output, candidate, gate_override=gate_override)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
    )

    assert result["status"] == "pre_judge_failed"
    assert result["pre_judge_gates"]["status"] == "failed"
    assert result["judge"]["status"] == "not_reached"
    assert result["official_score"] is None


def test_judge_requires_explicit_flag_and_forwards_key_by_name_only(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    files = _source_files()
    _stub_hugging_face(monkeypatch, files)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret-123")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    output = tmp_path / "judge-output"
    docker_calls = _docker_spy(monkeypatch, output, candidate)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output, judge=True,
    )

    assert result["status"] == "pre_judge_passed"
    assert result["judge"]["status"] == "local_judge_returned"
    assert result["verifier_network"] == "local_network_for_requested_judge"
    assert result["judge"]["scores"] == {"accuracy": 0.8}
    assert result["official_score"] is None
    grader_args, grader_env, _ = docker_calls[-1]
    assert grader_args[-2:] == ["bash", "/tests/test.sh"]
    assert grader_args[grader_args.index("-e") + 1] == "ANTHROPIC_API_KEY"
    assert grader_env["ANTHROPIC_API_KEY"] == "synthetic-secret-123"
    assert "OPENAI_API_KEY" not in grader_env
    assert "--network" not in grader_args
    assert all("ANTHROPIC_API_KEY" not in env for _, env, _ in docker_calls[:-1])
    assert all("synthetic-secret-123" not in " ".join(args)
               for args, _, _ in docker_calls)
    assert "synthetic-secret-123" not in json.dumps(result)
    assert "synthetic-secret-123" not in (output / "summary.json").read_text(
        encoding="utf-8",
    )


def test_judge_without_key_stops_before_download_or_docker(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    fetched = _stub_hugging_face(monkeypatch, _source_files())
    output = tmp_path / "judge-without-key"
    docker_calls = _docker_spy(monkeypatch, output, candidate)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output, judge=True,
    )
    assert result["status"] == "blocked"
    assert result["blocker"]["phase"] == "judge_configuration"
    assert result["official_score"] is None
    assert fetched == []
    assert docker_calls == []


def test_judge_error_and_returned_summary_redact_key(tmp_path, monkeypatch):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    _stub_hugging_face(monkeypatch, _source_files())
    secret = "synthetic-key-never-log-this"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    output = tmp_path / "judge-error"
    _docker_spy(monkeypatch, output, candidate, judge_error=f"authorization failed: {secret}")

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output, judge=True,
    )

    assert result["status"] == "pre_judge_passed"
    assert result["judge"]["status"] == "error"
    assert result["official_score"] is None
    assert secret not in json.dumps(result)
    assert secret not in (output / "summary.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("tamper", [None, "revision", "blob"])
def test_local_checkout_requires_exact_revision_and_intact_git_blobs(
    tmp_path, monkeypatch, tamper,
):
    saved = tmp_path / "saved"
    candidate = _saved_run(saved)
    checkout = tmp_path / "dataset"
    prefix = f"tasks/harbor/{TASK}/"
    files = _source_files()
    entries = []
    for name, data in files.items():
        target = checkout / prefix / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append(f"100644 blob {_git_blob_oid(data)} {len(data)}\t{prefix}{name}".encode())
    if tamper == "blob":
        (checkout / prefix / "tests/anti_hack.py").write_bytes(b"modified\n")

    def fake_git(root: Path, args: list[str]) -> bytes:
        assert root == checkout
        if args == ["rev-parse", "HEAD"]:
            return (("0" * 40 if tamper == "revision" else runner.REVISION) + "\n").encode()
        assert args == ["ls-tree", "-r", "-z", "--long", "HEAD", "--", prefix]
        return b"\0".join(entries) + b"\0"

    monkeypatch.setattr(runner, "_git", fake_git)
    monkeypatch.setattr(runner, "_fetch_json", lambda url: (_ for _ in ()).throw(
        AssertionError("local checkout should not call Hugging Face")))
    output = tmp_path / "local-result"
    docker_calls = _docker_spy(monkeypatch, output, candidate)

    result = runner.verify_saved_candidate(
        TASK, saved_run_root=saved, candidate=candidate, output_dir=output,
        dataset_root=checkout,
    )

    assert result["pinned_source_mode"] == "git_checkout"
    if tamper is None:
        assert result["status"] == "pre_judge_passed"
        assert len(docker_calls) == 3
    else:
        assert result["status"] == "blocked"
        assert result["blocker"]["phase"] == "pinned_dataset"
        assert docker_calls == []
