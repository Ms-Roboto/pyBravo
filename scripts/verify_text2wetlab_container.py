"""Run a saved Qwen Text2WetLab candidate through its pinned Harbor container.

Downloads the task tree at the fixed dataset commit (or reads a checkout at
that exact commit) and verifies every Git blob before invoking Docker. The
task's Dockerfile and ``tests/test.sh`` are used
unchanged. The default deliberately supplies no judge key; task-specific
anti-hack and simulation evidence is reported separately from judge availability.
The RNA grader has no lint pass and may stop before persisting its gate record
without a judge key, which is reported as incomplete rather than passed.
The default verifier container has no network. ``--judge`` permits network so
the local judge can be reached; that network policy is not Harbor's allowlist,
and even a returned rubric is not an official score.

Example::

    python scripts/verify_text2wetlab_container.py \
      --task split-200ul-two-wells \
      --saved-run-root /tmp/pybravo-text2wetlab-split-baseline \
      --candidate /tmp/pybravo-text2wetlab-split-baseline/split-200ul-two-wells/protocol.py \
      --output-dir /tmp/text2wetlab-split-container-check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import evaluate_text2wetlab as saved_runner  # noqa: E402

DATASET = saved_runner.DATASET
REVISION = saved_runner.REVISION
TASKS = saved_runner.TASKS
RNA_TASK = "opentrons-rna-extraction"
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED = frozenset({
    "environment/Dockerfile", "tests/test.sh", "tests/grade.py",
    "tests/anti_hack.py", "tests/data_hashes.json", "tests/runlog.py",
    "instruction.md",
})
_STANDARD_REQUIRED = frozenset({"tests/judge_layer.py"})
_RNA_REQUIRED = frozenset({
    "environment/data/paper.txt",
    "environment/data/labware/thermo_96_wellplate_200ul.json",
})


def _required(task: str) -> frozenset[str]:
    return _REQUIRED | (_RNA_REQUIRED if task == RNA_TASK else _STANDARD_REQUIRED)


class VerificationError(ValueError):
    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(detail)
        self.phase = phase


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {name: _redact(item, secret) for name, item in value.items()}
    return value


def _fetch_bytes(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def _fetch_json(url: str) -> Any:
    return json.loads(_fetch_bytes(url))


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\\" in value or ":" in value):
        raise VerificationError("pinned_dataset", f"Unsafe task file path: {value!r}")
    return path


def _download_pinned_task(task: str, destination: Path) -> dict[str, str]:
    """Materialize only one task, checking the commit and each Git blob OID."""
    base = f"https://huggingface.co/api/datasets/{DATASET}"
    metadata = _fetch_json(f"{base}/revision/{REVISION}")
    if (not isinstance(metadata, dict) or metadata.get("id") != DATASET
            or metadata.get("sha") != REVISION):
        raise VerificationError("pinned_dataset", "Dataset API did not confirm the pinned revision.")
    prefix = f"tasks/harbor/{task}/"
    rows = _fetch_json(f"{base}/tree/{REVISION}/tasks/harbor/{task}?recursive=true")
    if not isinstance(rows, list):
        raise VerificationError("pinned_dataset", "Pinned task tree is not a file listing.")
    hashes: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise VerificationError("pinned_dataset", "Pinned task tree contains an invalid entry.")
        if row.get("type") == "directory":
            continue
        full_path = row.get("path")
        if row.get("type") != "file" or not isinstance(full_path, str) or not full_path.startswith(prefix):
            raise VerificationError("pinned_dataset", "Pinned task tree contains an unexpected file.")
        relative = str(_safe_relative(full_path[len(prefix):]))
        oid, size = row.get("oid"), row.get("size")
        if (relative in hashes or not isinstance(oid, str) or _HEX_40.fullmatch(oid) is None
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise VerificationError("pinned_dataset", f"Missing Git identity for {relative}.")
        source = (f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/"
                  f"{quote(full_path, safe='/')}")
        data = _fetch_bytes(source)
        if len(data) != size or _git_blob_oid(data) != oid:
            raise VerificationError("pinned_dataset", f"Pinned Git blob differs for {relative}.")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = _sha(data)
    missing = _required(task) - hashes.keys()
    if missing:
        raise VerificationError("pinned_dataset", f"Pinned task tree lacks {sorted(missing)}.")
    return hashes


def _git(checkout: Path, args: list[str]) -> bytes:
    result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                            check=False, timeout=30)
    if result.returncode != 0:
        raise VerificationError("pinned_dataset", "Local dataset is not a readable Git checkout.")
    return result.stdout


def _copy_pinned_checkout_task(task: str, destination: Path, checkout: Path) -> dict[str, str]:
    """Use a local checkout only at the exact revision with intact task blobs."""
    if _git(checkout, ["rev-parse", "HEAD"]).decode("ascii", "replace").strip() != REVISION:
        raise VerificationError("pinned_dataset", "Local dataset checkout is not at the pinned revision.")
    prefix = f"tasks/harbor/{task}/"
    listing = _git(checkout, ["ls-tree", "-r", "-z", "--long", "HEAD", "--", prefix])
    hashes: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid, size = metadata.decode("ascii").split()
            full_path = raw_path.decode("utf-8")
            if (mode not in {"100644", "100755"} or kind != "blob"
                    or not full_path.startswith(prefix) or _HEX_40.fullmatch(oid) is None):
                raise ValueError("invalid tracked task entry")
            relative = str(_safe_relative(full_path[len(prefix):]))
            expected_size = int(size)
            if expected_size < 0 or relative in hashes:
                raise ValueError("invalid tracked task size or duplicate")
            data = (checkout / full_path).read_bytes()
        except (OSError, UnicodeError, ValueError) as exc:
            raise VerificationError("pinned_dataset", f"Local task tree is invalid: {exc}") from exc
        if len(data) != expected_size or _git_blob_oid(data) != oid:
            raise VerificationError("pinned_dataset", f"Local Git blob differs for {relative}.")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = _sha(data)
    missing = _required(task) - hashes.keys()
    if missing:
        raise VerificationError("pinned_dataset", f"Local pinned task tree lacks {sorted(missing)}.")
    return hashes


def _docker(args: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], capture_output=True, check=False,
                          timeout=timeout, env=env)


def _docker_ok(args: list[str], *, env: dict[str, str], timeout: int,
               phase: str, key: str | None) -> bytes:
    result = _docker(args, env=env, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")[-1200:]
        if key:
            detail = detail.replace(key, "[REDACTED]")
        raise VerificationError(phase, f"Docker command failed ({result.returncode}): {detail}")
    return result.stdout


def _materialize_verified_data(task_dir: Path, image: str, data_dir: Path,
                               *, env: dict[str, str], key: str | None) -> dict[str, str]:
    """Read the built image's pinned /data, then bind it read-only for grading."""
    expected = json.loads((task_dir / "tests/data_hashes.json").read_text(encoding="utf-8"))
    if not isinstance(expected, dict):
        raise VerificationError("task_data", "Pinned data_hashes.json is not an object.")
    data_dir.mkdir()
    observed: dict[str, str] = {}
    for name, digest in sorted(expected.items()):
        if (not isinstance(name, str) or not isinstance(digest, str)
                or _HEX_64.fullmatch(digest) is None):
            raise VerificationError("task_data", "Pinned data hash entry is invalid.")
        relative = _safe_relative(name)
        data = _docker_ok(["run", "--rm", "--network", "none", "--entrypoint", "cat", image,
                           f"/data/{relative}"], env=env, timeout=120,
                          phase="task_data", key=key)
        if _sha(data) != digest:
            raise VerificationError("task_data", f"Image task data differs from pinned hash: {name}.")
        target = data_dir / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        observed[name] = digest
    return observed


def _read_verifier(verifier_dir: Path, candidate_sha: str, *, judge_requested: bool,
                   test_exit_code: int, key: str | None) -> dict[str, Any]:
    try:
        record = json.loads((verifier_dir / "result.json").read_text(encoding="utf-8"))
        reward = json.loads((verifier_dir / "reward.json").read_text(encoding="utf-8"))
        graded_sha = _sha((verifier_dir / "protocol.py").read_bytes())
    except (OSError, ValueError) as exc:
        raise VerificationError("verifier_result", f"Pinned verifier evidence is unavailable: {exc}") from exc
    if not isinstance(record, dict) or not isinstance(reward, dict) or record.get("rewards") != reward:
        raise VerificationError("verifier_result", "Pinned result and reward files disagree.")
    if graded_sha != candidate_sha:
        raise VerificationError("verifier_result", "Verifier graded a different candidate digest.")
    lint, traps, simulation = record.get("lint"), record.get("traps"), record.get("simulation")
    if not isinstance(lint, list):
        raise VerificationError("verifier_result", "Pinned verifier omitted its lint result.")
    if traps is not None and not isinstance(traps, list):
        raise VerificationError("verifier_result", "Pinned verifier has an invalid anti-hack result.")
    if simulation is not None and not isinstance(simulation, dict):
        raise VerificationError("verifier_result", "Pinned verifier has an invalid simulator result.")
    if not lint and traps is None:
        raise VerificationError("verifier_result", "Pinned verifier skipped anti-hack after clean lint.")
    if not lint and traps == [] and simulation is None:
        raise VerificationError("verifier_result", "Pinned verifier skipped simulation after clean anti-hack.")
    events_path = verifier_dir / "events.json"
    events_sha = None
    if events_path.is_file():
        try:
            events_bytes = events_path.read_bytes()
            if not isinstance(json.loads(events_bytes), list):
                raise ValueError("events.json is not a list")
            events_sha = _sha(events_bytes)
        except (OSError, ValueError) as exc:
            raise VerificationError("verifier_result", f"Pinned simulator events are invalid: {exc}") from exc
    gates_passed = (lint == [] and traps == [] and isinstance(simulation, dict)
                    and simulation.get("ok") is True
                    and reward.get("sim_pass") == 1.0 and test_exit_code == 0 and events_sha is not None)
    verdict = record.get("judge")
    if not isinstance(verdict, dict):
        judge = {"status": "not_reached"}
    elif "error" in verdict:
        detail = str(verdict["error"])
        if key:
            detail = detail.replace(key, "[REDACTED]")
        judge = {"status": "error" if judge_requested else "unavailable_no_key",
                 "model": verdict.get("model"), "error": detail[:1200]}
    elif judge_requested and isinstance(verdict.get("scores"), dict):
        judge = {"status": "local_judge_returned", "model": verdict.get("model"),
                 "scores": verdict["scores"], "local_reward": reward.get("reward")}
    else:
        judge = {"status": "unexpected_result_without_requested_key",
                 "model": verdict.get("model")}
    return {
        "status": "pre_judge_passed" if gates_passed else "pre_judge_failed",
        "pre_judge_gates": {"status": "passed" if gates_passed else "failed",
                            "lint_violations": lint, "anti_hack_traps": traps,
                            "simulation": simulation if simulation is not None else {"status": "not_run"}},
        "judge": judge,
        "test_script_exit_code": test_exit_code,
        "graded_copy_sha256": graded_sha,
        "verifier_result_path": str(verifier_dir / "result.json"),
        "verifier_events_path": str(events_path) if events_sha else None,
        "verifier_events_sha256": events_sha,
    }


def _read_rna_verifier(verifier_dir: Path, candidate_sha: str, *, judge_requested: bool,
                       test_exit_code: int, stderr: bytes, key: str | None) -> dict[str, Any]:
    """Parse RNA's judge.json format and its no-key pre-report interruption.

    The pinned RNA grader creates its Anthropic client outside the retry block.
    With no key it may write protocol.py and events.json, then fail before
    judge.json/reward.json. Those files show that simulation was reached, but
    do not retain its suspicious-token or trap lists, so no gate pass is claimed.
    """
    try:
        graded_sha = _sha((verifier_dir / "protocol.py").read_bytes())
    except OSError as exc:
        raise VerificationError("verifier_result", "Pinned RNA grader did not retain the candidate.") from exc
    if graded_sha != candidate_sha:
        raise VerificationError("verifier_result", "RNA verifier graded a different candidate digest.")
    events_path = verifier_dir / "events.json"
    events_sha = None
    event_count = None
    if events_path.is_file():
        try:
            events_bytes = events_path.read_bytes()
            events = json.loads(events_bytes)
            if not isinstance(events, list):
                raise ValueError("events.json is not a list")
            events_sha, event_count = _sha(events_bytes), len(events)
        except (OSError, ValueError) as exc:
            raise VerificationError("verifier_result", f"Pinned RNA events are invalid: {exc}") from exc
    retained = {
        "test_script_exit_code": test_exit_code,
        "graded_copy_sha256": graded_sha,
        "verifier_result_path": None,
        "verifier_events_path": str(events_path) if events_sha else None,
        "verifier_events_sha256": events_sha,
    }
    judge_path = verifier_dir / "judge.json"
    reward_path = verifier_dir / "reward.json"
    if not judge_path.is_file() and not reward_path.is_file():
        auth_error = (b"Could not resolve authentication method" in stderr
                      or b"The api_key client option must be set" in stderr)
        if judge_requested or test_exit_code == 0 or not auth_error or events_sha is None:
            raise VerificationError("verifier_result", "RNA grader did not persist its judge and reward record.")
        return {
            **retained,
            "status": "pre_judge_incomplete",
            "pre_judge_gates": {
                "status": "incomplete", "lint_status": "not_applicable",
                "lint_violations": None, "anti_hack_traps": None,
                "anti_hack_inference": "Events were written after the pinned trap check, but the trap list was not retained.",
                "suspicious_tokens": None,
                "simulation": {"ok": True, "event_count": event_count,
                               "evidence": "pinned RNA grader events.json"},
            },
            "judge": {"status": "unavailable_no_key",
                      "error": "Pinned RNA grader stopped before persisting judge.json/reward.json."},
        }
    try:
        record = json.loads(judge_path.read_text(encoding="utf-8"))
        reward = json.loads(reward_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationError("verifier_result", f"Pinned RNA judge evidence is unavailable: {exc}") from exc
    if not isinstance(record, dict) or not isinstance(reward, dict) or record.get("rewards") != reward:
        raise VerificationError("verifier_result", "Pinned RNA judge and reward files disagree.")
    suspicious, traps, simulation = (record.get("suspicious_tokens"), record.get("traps"),
                                    record.get("simulation"))
    if (not isinstance(suspicious, list) or not isinstance(traps, list)
            or not isinstance(simulation, dict)):
        raise VerificationError("verifier_result", "Pinned RNA grader omitted a gate result.")
    passed = (suspicious == [] and traps == [] and simulation.get("ok") is True
              and reward.get("sim_pass") == 1.0 and test_exit_code == 0 and events_sha is not None)
    verdict = record.get("judge")
    if not isinstance(verdict, dict):
        judge = {"status": "not_reached"}
    elif "error" in verdict:
        detail = str(verdict["error"])
        if key:
            detail = detail.replace(key, "[REDACTED]")
        judge = {"status": "error" if judge_requested else "unavailable_no_key",
                 "model": record.get("judge_model"), "error": detail[:1200]}
    elif judge_requested and isinstance(verdict.get("scores"), dict):
        judge = {"status": "local_judge_returned", "model": record.get("judge_model"),
                 "scores": verdict["scores"], "local_reward": reward.get("reward")}
    else:
        judge = {"status": "unexpected_result_without_requested_key",
                 "model": record.get("judge_model")}
    return {
        **retained,
        "status": "pre_judge_passed" if passed else "pre_judge_failed",
        "pre_judge_gates": {
            "status": "passed" if passed else "failed", "lint_status": "not_applicable",
            "lint_violations": None, "anti_hack_traps": traps,
            "suspicious_tokens": suspicious, "simulation": simulation,
        },
        "judge": judge,
        "verifier_result_path": str(judge_path),
    }


def verify_saved_candidate(task: str, *, saved_run_root: Path, candidate: Path,
                           output_dir: Path, judge: bool = False,
                           dataset_root: Path | None = None) -> dict[str, Any]:
    """Verify one unchanged saved candidate; ``official_score`` remains unknown."""
    if task not in TASKS:
        raise ValueError(f"Unknown Text2WetLab task: {task}")
    saved_root = saved_run_root.expanduser().resolve()
    candidate = candidate.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    checkout = dataset_root.expanduser().resolve() if dataset_root is not None else None
    if output == saved_root or output in saved_root.parents or saved_root in output.parents:
        raise ValueError("Output must be separate from the saved candidate run.")
    if checkout is not None and (output == checkout or checkout in output.parents or output in checkout.parents):
        raise ValueError("Output must be separate from the pinned dataset checkout.")
    if output.exists():
        raise ValueError("Output directory must be new, so verifier logs start empty.")
    output.mkdir(parents=True)
    summary: dict[str, Any] = {
        "task": task, "dataset": DATASET, "dataset_revision": REVISION,
        "candidate_path": str(candidate), "saved_run_root": str(saved_root),
        "pinned_source_mode": "git_checkout" if checkout is not None else "remote_commit",
        "verifier_network": "local_network_for_requested_judge" if judge else "none",
        "judge_requested": judge, "official_score": None, "status": "blocked",
        "pre_judge_gates": None, "judge": {"status": "not_reached"},
    }
    key = os.environ.get("ANTHROPIC_API_KEY") if judge else None
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    try:
        if judge and not key:
            raise VerificationError("judge_configuration", "--judge requires ANTHROPIC_API_KEY in the environment.")
        task_dir = output / "pinned" / task
        source_hashes = (_copy_pinned_checkout_task(task, task_dir, checkout)
                         if checkout is not None else _download_pinned_task(task, task_dir))
        summary["pinned_sources_sha256"] = source_hashes
        instruction = (task_dir / "instruction.md").read_bytes()
        paper_file = task_dir / "environment/data/paper.txt"
        source_paper = paper_file.read_bytes() if paper_file.is_file() else None
        provenance = saved_runner._saved_candidate_provenance(task, saved_root, instruction,
                                                              source_paper)
        if provenance.get("status") != "passed":
            raise VerificationError("candidate_provenance", str(provenance.get("detail")))
        if candidate != Path(provenance["candidate_path"]).resolve():
            raise VerificationError("candidate_provenance", "Candidate path is not the saved Qwen candidate.")
        candidate_sha = _sha(candidate.read_bytes())
        if candidate_sha != provenance["candidate_sha256"]:
            raise VerificationError("candidate_provenance", "Candidate digest changed after provenance check.")
        summary["candidate_sha256"] = candidate_sha
        summary["saved_candidate_provenance"] = provenance
        image = f"pybravo-text2wetlab-verify-{task}:{REVISION[:12]}-{candidate_sha[:12]}"
        summary["docker_image"] = image
        _docker_ok(["build", "--tag", image, str(task_dir / "environment")],
                   env=env, timeout=3600, phase="docker_build", key=key)
        summary["docker_image_id"] = _docker_ok(
            ["image", "inspect", "--format", "{{.Id}}", image],
            env=env, timeout=30, phase="docker_build", key=key,
        ).decode("utf-8", "replace").strip()
        data_dir = output / "data"
        summary["task_data_sha256"] = _materialize_verified_data(
            task_dir, image, data_dir, env=env, key=key,
        )
        verifier_dir = output / "verifier"
        verifier_dir.mkdir()
        args = ["run", "--rm",
                "--mount", f"type=bind,source={task_dir / 'tests'},target=/tests,readonly",
                "--mount", f"type=bind,source={candidate},target=/app/protocol.py,readonly",
                "--mount", f"type=bind,source={data_dir},target=/data,readonly",
                "--mount", f"type=bind,source={verifier_dir},target=/logs/verifier",
                ]
        if judge:
            args.extend(["-e", "ANTHROPIC_API_KEY"])
        else:
            args.extend(["--network", "none"])
        args.extend([image, "bash", "/tests/test.sh"])
        run_env = {**env, "ANTHROPIC_API_KEY": key} if judge and key else env
        completed = _docker(args, env=run_env, timeout=900)
        if task == RNA_TASK:
            summary.update(_read_rna_verifier(
                verifier_dir, candidate_sha, judge_requested=judge,
                test_exit_code=completed.returncode, stderr=completed.stderr, key=key,
            ))
        else:
            summary.update(_read_verifier(
                verifier_dir, candidate_sha, judge_requested=judge,
                test_exit_code=completed.returncode, key=key,
            ))
        if _sha(candidate.read_bytes()) != candidate_sha:
            raise VerificationError("candidate_integrity", "Candidate changed during container verification.")
    except (VerificationError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        phase = exc.phase if isinstance(exc, VerificationError) else "runner"
        detail = str(exc)
        if key:
            detail = detail.replace(key, "[REDACTED]")
        summary.update(status="blocked", blocker={"phase": phase, "detail": detail[:1200]})
    summary = _redact(summary, key)
    serialized = json.dumps(summary, indent=2, sort_keys=True)
    (output / "summary.json").write_text(serialized + "\n",
                                         encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=TASKS)
    parser.add_argument("--saved-run-root", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dataset-root", type=Path,
                        help="Optional local Git checkout at the exact pinned dataset commit")
    parser.add_argument("--judge", action="store_true",
                        help="Explicitly pass an existing ANTHROPIC_API_KEY to the pinned judge")
    args = parser.parse_args()
    try:
        result = verify_saved_candidate(args.task, saved_run_root=args.saved_run_root,
                                        candidate=args.candidate, output_dir=args.output_dir,
                                        judge=args.judge, dataset_root=args.dataset_root)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"summary_path": str(args.output_dir.expanduser().resolve() / "summary.json"),
                      "status": result["status"], "judge_status": result["judge"]["status"]}))
    return 0 if (result["status"] == "pre_judge_passed"
                 and (not args.judge or result["judge"]["status"] == "local_judge_returned")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
