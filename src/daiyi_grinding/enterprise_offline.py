"""Run frozen offline suites serially and verify their actual evidence.

This module imports only the standard library. Task modules, including learned
inference, run in separate resource-bounded processes. Software expectations,
observed quality holds and pending enterprise measurements are separate results.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK_NAMES = (
    "enterprise_acceptance",
    "offline_scenarios",
    "perception_stress",
    "registration_stress",
    "planning_stress",
    "reinspection_stress",
    "process_sandbox",
    "reliability_stress",
)
MAIN_CHAIN_NAMES = ("main_chain_thin_120483", "main_chain_normal_9904")
ENVIRONMENT = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "MUJOCO_GL": "egl",
    "CUDA_VISIBLE_DEVICES": "",
}
REQUIRED_EVIDENCE = {
    "task",
    "scope",
    "commands",
    "inputs",
    "metrics",
    "failures",
    "artifacts",
    "real_data_pending",
}
REPOSITORY_DIRS = {
    "src",
    "tests",
    "config",
    "assets",
    "external",
    "runs",
    "results",
    "docs",
    "scripts",
    "feishu_docs",
}


class EvidenceError(ValueError):
    """An explicit evidence or suite-contract violation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    """Hash actual file bytes without loading large replay files into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_json_key", f"Duplicate key: {key}")
        result[key] = value
    return result


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise EvidenceError("nonfinite_json", f"Non-finite number: {text}")
    return value


def _constant(text):
    raise EvidenceError("nonfinite_json", f"Non-finite constant: {text}")


def strict_json(text: str):
    """Read JSON while rejecting duplicate keys and non-finite numbers."""
    return json.loads(
        text,
        object_pairs_hook=_pairs,
        parse_float=_float,
        parse_constant=_constant,
    )


def read_json(path: Path):
    """Read a strict UTF-8 JSON artifact."""
    return strict_json(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value):
    """Write finite JSON with a trailing newline."""
    Path(path).write_text(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )


def file_reference(path: Path) -> dict:
    path = Path(path).resolve()
    return {
        "path": str(path),
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
    }


def _error(code, reason, **details):
    return {"code": code, "reason": str(reason), **details}


def _require(condition, message):
    if not condition:
        raise EvidenceError("suite_contract", message)


def _count(value, name):
    _require(type(value) is int and value >= 0, f"{name} must be a count")
    return value


def _source_files(root: Path):
    for name in ("src", "tests", "config"):
        directory = root / name
        if not directory.is_dir():
            raise EvidenceError("source_missing", f"Missing tree: {directory}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and "__pycache__" not in relative.parts
                and ".pytest_cache" not in relative.parts
                and path.suffix not in {".pyc", ".pyo"}
            ):
                yield path
    project = root / "pyproject.toml"
    if not project.is_file():
        raise EvidenceError("source_missing", f"Missing file: {project}")
    yield project


def snapshot_sources(root: Path) -> dict:
    """Freeze all source, test, configuration and project-file contents."""
    root = Path(root).resolve()
    files = {
        str(path.relative_to(root)): sha256(path)
        for path in _source_files(root)
    }
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"root": str(root), "files": files, "tree_sha256": digest}


def changed_sources(before, after) -> list[str]:
    previous, current = before["files"], after["files"]
    return sorted(
        name
        for name in previous.keys() | current.keys()
        if previous.get(name) != current.get(name)
    )


@dataclass
class SourceFreeze:
    """Detect content changes during a child run, including added files."""

    root: Path
    before: dict
    changes: list[str] = field(default_factory=list)
    last_check: float = 0.0

    def check(self, *, force=False) -> bool:
        now = time.monotonic()
        if force or now - self.last_check >= 0.5:
            self.last_check = now
            try:
                current = snapshot_sources(self.root)
                self.changes = sorted(
                    set(self.changes)
                    | set(changed_sources(self.before, current))
                )
            except (OSError, EvidenceError) as error:
                self.changes = sorted(
                    set(self.changes) | {f"snapshot_error: {error}"}
                )
        return not self.changes


@dataclass(frozen=True)
class Task:
    """One actual child command and its independently allocated output."""

    name: str
    output: Path
    argv: tuple[str, ...]
    lock_owner: str = "not_required"
    require_mujoco_fixture: bool = False


def build_tasks(
    output: Path,
    names: tuple[str, ...],
    *,
    root=ROOT,
    python=sys.executable,
    include_main_chain=False,
    suite_id=None,
) -> list[Task]:
    """Construct fixed offline CLIs; never import a task to construct arguments."""
    output, root = Path(output).resolve(), Path(root).resolve()
    suite_id = suite_id or uuid.uuid4().hex[:12]
    result = []
    for name in TASK_NAMES:
        if name not in names:
            continue
        destination = output / "tasks" / name
        if name == "process_sandbox":
            # The existing process CLI deliberately restricts its output root.
            destination = (
                root
                / "runs/enterprise_offline_20261123/process"
                / f"runner_{suite_id}"
            )
        arguments = [
            str(python),
            "-m",
            f"daiyi_grinding.{name}",
            "--output",
            str(destination),
        ]
        fixture = name == "planning_stress" and include_main_chain
        if fixture:
            arguments.append("--mujoco-fixture")
        owner = (
            "module"
            if name == "offline_scenarios" or fixture
            else "not_required"
        )
        result.append(Task(name, destination, tuple(arguments), owner, fixture))
    if include_main_chain:
        common = [
            "--detector",
            "geometric",
            "--tool",
            "belt",
            "--grasp-setup",
            "original",
            "--views",
            "multi",
            "--mode",
            "removal",
            "--max-passes",
            "1",
            "--realtime-factor",
            "0",
        ]
        for name, seed, selection in (
            (
                MAIN_CHAIN_NAMES[0],
                120483,
                ["--workpiece-profile", "097_thin_seam"],
            ),
            (MAIN_CHAIN_NAMES[1], 9904, ["--case", "normal"]),
        ):
            destination = output / "tasks" / name
            arguments = [
                str(python),
                "-m",
                "daiyi_grinding.workstation_run",
                *selection,
                "--seed",
                str(seed),
                *common,
                "--output",
                str(destination),
            ]
            result.append(Task(name, destination, tuple(arguments), "parent"))
    return result


def _utc_now():
    return datetime.now().astimezone().isoformat()


def _kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def execute_task(
    task: Task,
    receipt_directory: Path,
    *,
    root: Path,
    freeze: SourceFreeze,
    timeout: float,
    progress=None,
) -> dict:
    """Run one process group and collect per-child kernel resource usage.

    ``wait4`` returns this child's high-water RSS, not the parent's cumulative
    RUSAGE_CHILDREN statistic. Linux reports ru_maxrss in KiB. File-backed
    stdout/stderr avoid pipe deadlocks and retain output after interruptions.
    """
    receipt_directory.mkdir(parents=True, exist_ok=False)
    stdout_path = receipt_directory / "stdout.log"
    stderr_path = receipt_directory / "stderr.log"
    started = time.monotonic()
    deadline = started + timeout
    environment = {**os.environ, **ENVIRONMENT}
    lock_path = root / "runs/enterprise_offline_20261123/mujoco.lock"
    receipt = {
        "task": task.name,
        "argv": list(task.argv),
        "cwd": str(root),
        "environment": dict(ENVIRONMENT),
        "output": str(task.output),
        "started_at": _utc_now(),
        "timeout_seconds": timeout,
        "lock_owner": task.lock_owner,
        "lock_path": str(lock_path)
        if task.lock_owner != "not_required"
        else None,
        "pid": None,
        "exit_code": None,
        "timed_out": False,
        "source_changed": False,
        "failures": [],
    }
    resource = {
        "method": "os.wait4(child_pid), Linux ru_maxrss in KiB",
        "peak_rss_kib": None,
        "user_cpu_seconds": None,
        "system_cpu_seconds": None,
        "includes_waited_descendants": True,
        "cumulative_parent_resource_used": False,
    }
    lock_stream = None
    process = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            if task.output.exists():
                raise EvidenceError(
                    "output_exists", f"Task output must be new: {task.output}"
                )
            if task.lock_owner == "parent":
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_stream = lock_path.open("a+")
                while True:
                    if not freeze.check():
                        raise EvidenceError("source_changed", freeze.changes)
                    try:
                        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        receipt["lock_acquired_at"] = _utc_now()
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            receipt["timed_out"] = True
                            raise EvidenceError(
                                "lock_timeout", f"MuJoCo lock busy: {lock_path}"
                            )
                        time.sleep(0.05)
            if not freeze.check(force=True):
                raise EvidenceError("source_changed", freeze.changes)
            process = subprocess.Popen(
                task.argv,
                cwd=root,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            receipt["pid"] = process.pid
            stop_at = None
            last_progress = started
            while True:
                waited_pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if waited_pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    receipt["exit_code"] = process.returncode
                    resource.update(
                        peak_rss_kib=int(usage.ru_maxrss),
                        user_cpu_seconds=usage.ru_utime,
                        system_cpu_seconds=usage.ru_stime,
                    )
                    break
                now = time.monotonic()
                source_ok = freeze.check()
                if stop_at is None and (now >= deadline or not source_ok):
                    receipt["timed_out"] = now >= deadline
                    receipt["source_changed"] = not source_ok
                    stop_at = now
                    _kill_group(process.pid, signal.SIGTERM)
                elif stop_at is not None and now - stop_at >= 1.0:
                    _kill_group(process.pid, signal.SIGKILL)
                if progress and now - last_progress >= 30.0:
                    progress(f"{task.name}: running for {now - started:.1f}s")
                    last_progress = now
                time.sleep(0.05)
            if stop_at is not None:
                # Descendants may ignore SIGTERM after their leader has exited.
                _kill_group(process.pid, signal.SIGKILL)
    except (OSError, EvidenceError) as error:
        receipt["failures"].append(
            _error(getattr(error, "code", "spawn_error"), error)
        )
    except BaseException:
        if process is not None and process.returncode is None:
            _kill_group(process.pid, signal.SIGKILL)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            receipt["exit_code"] = process.returncode
            resource["peak_rss_kib"] = int(usage.ru_maxrss)
        receipt["failures"].append(_error("runner_interrupted", task.name))
        raise
    finally:
        if lock_stream is not None:
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
            lock_stream.close()
        receipt["finished_at"] = _utc_now()
        receipt["wall_seconds"] = time.monotonic() - started
        receipt["source_changed"] = receipt[
            "source_changed"
        ] or not freeze.check(force=True)
        if receipt["timed_out"]:
            receipt["failures"].append(_error("task_timeout", task.name))
        if receipt["source_changed"]:
            receipt["failures"].append(
                _error(
                    "source_changed",
                    "Frozen source changed",
                    files=freeze.changes,
                )
            )
        if receipt["exit_code"] not in (0, None):
            receipt["failures"].append(
                _error(
                    "nonzero_exit", task.name, exit_code=receipt["exit_code"]
                )
            )
        receipt["peak_rss_kib"] = resource["peak_rss_kib"]
        write_json(receipt_directory / "resource.json", resource)
        receipt["stdout"] = file_reference(stdout_path)
        receipt["stderr"] = file_reference(stderr_path)
        receipt["resource"] = file_reference(
            receipt_directory / "resource.json"
        )
        write_json(receipt_directory / "receipt.json", receipt)
    return receipt


def _is_hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _looks_path(value):
    return isinstance(value, str) and ("/" in value or bool(Path(value).suffix))


def _references(value, field_name, *, artifact=False):
    """Adapt file lists, named dictionaries, hash maps and prefixed pairs."""
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _references(
                child, f"{field_name}[{index}]", artifact=artifact
            )
    elif isinstance(value, dict):
        consumed = set()
        for key, child in value.items():
            if (key == "path" or key.endswith("_path")) and not isinstance(
                child, (list, dict)
            ):
                digest_key = "sha256" if key == "path" else key[:-5] + "_sha256"
                digest = value.get(digest_key)
                yield {
                    "field": f"{field_name}.{key}",
                    "path": child,
                    "sha256": digest,
                    "record": value,
                }
                consumed.update((key, digest_key))
            elif key.endswith("_sha256"):
                prefix = key[:-7]
                if prefix + "_path" in value or key == "expected_sha256":
                    continue
                path_key = next(
                    (
                        candidate
                        for candidate in (prefix, prefix + "_file")
                        if candidate in value
                    ),
                    None,
                )
                if path_key:
                    yield {
                        "field": f"{field_name}.{path_key}",
                        "path": value[path_key],
                        "sha256": child,
                        "record": value,
                    }
                    consumed.update((key, path_key))
        for key, child in value.items():
            if key in consumed:
                continue
            if _looks_path(key) and isinstance(child, str):
                yield {
                    "field": f"{field_name}.{key}",
                    "path": key,
                    "sha256": child,
                    "record": {},
                }
            elif isinstance(child, (list, dict)):
                yield from _references(
                    child, f"{field_name}.{key}", artifact=artifact
                )
    elif isinstance(value, str) and (artifact or _looks_path(value)):
        yield {
            "field": field_name,
            "path": value,
            "sha256": None,
            "record": {},
        }


def _resolve_reference(text, base: Path, root: Path):
    if not isinstance(text, str) or not text.strip():
        raise EvidenceError("invalid_path", f"Invalid path: {text!r}")
    path = Path(text)
    if path.is_absolute():
        return path.resolve()
    # Repository-relative paths occur alongside output-relative artifact paths.
    if path.parts and (
        path.parts[0] in REPOSITORY_DIRS or path.name == "pyproject.toml"
    ):
        return (root / path).resolve()
    return (base / path).resolve()


def _command_errors(value):
    if isinstance(value, dict):
        for key in ("returncode", "exit_code"):
            if key in value and value[key] != 0:
                yield _error("declared_command_failed", value, field=key)
        for child in value.values():
            yield from _command_errors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _command_errors(child)


@dataclass
class EvidenceCheck:
    """Verified receipts plus material expected outcomes for one task."""

    task: str
    output: Path
    evidence: dict = field(default_factory=dict)
    references: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    cases: list[dict] = field(default_factory=list)
    dispositions: list[dict] = field(default_factory=list)
    case_sources: dict[str, dict] = field(default_factory=dict)

    @property
    def passed(self):
        return not self.errors

    def artifact_path(self, name):
        path = (self.output / name).resolve()
        _require(
            any(
                reference["path"] == str(path)
                and reference["section"] == "artifacts"
                and reference["status"] == "verified"
                for reference in self.references
            ),
            f"Required result is not a verified artifact: {name}",
        )
        return path

    def artifact(self, name):
        path = self.artifact_path(name)
        if path.suffix == ".jsonl":
            return [
                strict_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return read_json(path)

    def case(self, name, outcome, details, source, *, data_kind, role):
        if source not in self.case_sources:
            path = (self.output / source).resolve()
            verified = next(
                (
                    reference
                    for reference in self.references
                    if reference["path"] == str(path)
                    and reference["status"] == "verified"
                ),
                None,
            )
            self.case_sources[source] = (
                {
                    "path": str(path),
                    "sha256": verified["actual_sha256"],
                    "bytes": path.stat().st_size,
                }
                if verified
                else file_reference(path)
            )
        self.cases.append(
            {
                "case_id": str(name),
                "outcome": outcome,
                "details": details,
                "source": self.case_sources[source],
                "data_kind": data_kind,
                "evaluation_role": role,
            }
        )


def _expected_missing_input(task, output, reference, path) -> bool:
    """Admit only a reproduced, explicitly missing synthetic feedback case."""
    if (
        task != "enterprise_acceptance"
        or reference["record"].get("integrity") != "missing"
        or reference["sha256"] is not None
        or path.exists()
    ):
        return False
    case_root = output / "synthetic_suite/controller_feedback_missing"
    if not path.is_relative_to(case_root.resolve()):
        return False
    suite = read_json(output / "synthetic_suite/suite_summary.json")
    case = next(
        (
            row
            for row in suite.get("cases", [])
            if row.get("case") == "controller_feedback_missing"
        ),
        {},
    )
    return (
        case.get("expected_decision_reproduced") is True
        and case.get("expected") == case.get("observed")
        and case.get("expected", {}).get("source_integrity") == "fail"
        and any(
            row.get("path") == str(path)
            and row.get("integrity") == "missing"
            and row.get("role") == reference["record"].get("role")
            for row in case.get("inputs", [])
        )
    )


def validate_evidence(task: Task, *, root=ROOT) -> EvidenceCheck:
    """Verify strict JSON, every declared file and the task's actual contract."""
    root, output = Path(root).resolve(), task.output.resolve()
    check = EvidenceCheck(task.name, output)
    try:
        evidence_path = output / "evidence.json"
        if not evidence_path.is_file():
            raise EvidenceError("missing_evidence", str(evidence_path))
        evidence = read_json(evidence_path)
        _require(isinstance(evidence, dict), "Evidence must be a JSON object")
        missing = REQUIRED_EVIDENCE - evidence.keys()
        _require(not missing, f"Missing evidence fields: {sorted(missing)}")
        _require(
            isinstance(evidence["task"], str) and evidence["task"].strip(),
            "Evidence task must be explicit",
        )
        for name in ("scope", "commands", "inputs", "artifacts"):
            _require(
                isinstance(evidence[name], (str, list, dict))
                and evidence[name],
                f"Evidence {name} must be explicit and nonempty",
            )
        _require(
            isinstance(evidence["metrics"], dict) and evidence["metrics"],
            "Evidence metrics must be an explicit object",
        )
        for name in ("failures", "real_data_pending"):
            _require(
                isinstance(evidence[name], (list, dict)),
                f"Evidence {name} must be a collection",
            )
        check.evidence = evidence
        check.errors.extend(_command_errors(evidence["commands"]))
        for section in ("inputs", "artifacts"):
            references = list(
                _references(
                    evidence[section], section, artifact=section == "artifacts"
                )
            )
            _require(references, f"No file references in {section}")
            for reference in references:
                ledger = {
                    "section": section,
                    "field": reference["field"],
                    "declared_path": reference["path"],
                    "sha256": reference["sha256"],
                    "path": None,
                    "status": "failed",
                }
                try:
                    path = _resolve_reference(reference["path"], output, root)
                    ledger["path"] = str(path)
                    if path == evidence_path:
                        raise EvidenceError(
                            "self_evidence_hash", "Evidence cannot hash itself"
                        )
                    if section == "inputs" and _expected_missing_input(
                        task.name, output, reference, path
                    ):
                        ledger["status"] = "expected_missing_rejected"
                    else:
                        if not path.is_file():
                            raise EvidenceError(
                                "missing_file", f"Expected a file: {path}"
                            )
                        if not _is_hash(reference["sha256"]):
                            raise EvidenceError(
                                "missing_or_invalid_hash", str(path)
                            )
                        actual = sha256(path)
                        ledger["actual_sha256"] = actual
                        if actual != reference["sha256"].lower():
                            raise EvidenceError(
                                "hash_mismatch", f"Hash changed: {path}"
                            )
                        ledger["status"] = "verified"
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    IndexError,
                ) as error:
                    check.errors.append(
                        _error(
                            getattr(error, "code", "reference_error"),
                            error,
                            field=reference["field"],
                        )
                    )
                check.references.append(ledger)
        _validate_contract(task, check)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
    ) as error:
        check.errors.append(
            _error(getattr(error, "code", "invalid_evidence"), error)
        )
    return check


def _validate_acceptance(check):
    metrics, failures = check.evidence["metrics"], check.evidence["failures"]
    total = _count(metrics.get("synthetic_cases"), "synthetic_cases")
    _require(
        total > 0
        and _count(
            metrics.get("synthetic_expected_decisions_reproduced"),
            "synthetic_expected_decisions_reproduced",
        )
        == total,
        "Acceptance synthetic expected decisions were not all reproduced",
    )
    suite = check.artifact("synthetic_suite/suite_summary.json")
    cases = suite.get("cases")
    _require(
        isinstance(cases, list)
        and len(cases) == total
        and suite.get("case_count") == total
        and suite.get("expected_decisions_reproduced") == total,
        "Acceptance suite counts disagree with verified cases",
    )
    known = set()
    for row in cases:
        _require(
            row.get("expected_decision_reproduced") is True
            and row.get("expected") == row.get("observed")
            and row.get("all_enterprise_targets_unknown") is True,
            f"Acceptance expectation failed: {row.get('case')}",
        )
        known.add(row["case"])
        rejected = any(
            status == "fail" for status in row.get("observed", {}).values()
        )
        check.case(
            row["case"],
            "expected_rejection" if rejected else "expected_decision",
            {
                "expected": row["expected"],
                "observed": row["observed"],
                "enterprise_targets_unknown": True,
            },
            "synthetic_suite/suite_summary.json",
            data_kind="synthetic_contract",
            role="development",
        )
    _require(len(known) == total, "Acceptance case IDs are not unique")
    _require(isinstance(failures, list), "Acceptance failures must be a list")
    for failure in failures:
        assessment = failure.get("assessment", "")
        if assessment.startswith("synthetic/") and assessment[10:] in known:
            outcome = "expected_rejection"
        elif (
            failure.get("code") == "criterion_not_met"
            and failure.get("scope")
            == "measured offline criterion; real status is evaluated separately"
        ):
            outcome = "observed_quality_hold"
        elif assessment == "site_manifest_00" and any(
            reference["section"] == "inputs"
            and reference["status"] == "verified"
            and Path(reference["path"]).name == "site_acceptance.example.json"
            for reference in check.references
        ):
            outcome = "pending_real"
        else:
            raise EvidenceError(
                "unexpected_acceptance_failure", json.dumps(failure)
            )
        check.dispositions.append({"outcome": outcome, "details": failure})
    for reference in check.references:
        path = Path(reference["path"]) if reference["path"] else None
        if (
            reference["section"] == "artifacts"
            and reference["status"] == "verified"
            and path.suffix == ".json"
            and path.is_relative_to(check.output / "current")
        ):
            report = read_json(path)
            check.case(
                str(path.relative_to(check.output)),
                "observed_quality_hold"
                if report.get("failures")
                else "assessment_completed",
                {
                    "enterprise_acceptance_passed": report.get(
                        "enterprise_acceptance_passed"
                    ),
                    "failures": report.get("failures", []),
                },
                str(path.relative_to(check.output)),
                data_kind="archived_simulation_assessment",
                role="known_regression",
            )
    _require(
        metrics.get("virtual_800h_is_real_800h") is False
        and metrics.get("real_enterprise_acceptances") == 0,
        "Offline acceptance must not claim real enterprise acceptance",
    )


def _validate_registration(check):
    metrics = check.evidence["metrics"]
    for key, value in (
        ("cases_passed", 4),
        ("cases_rejected_as_expected", 3),
        ("unexpected_failures", 0),
    ):
        _require(
            _count(metrics.get(key), key) == value,
            f"Registration {key} != {value}",
        )
    summary = check.artifact("summary.json")
    cases = summary.get("case_results")
    _require(
        summary.get("all_expectations_met") is True
        and isinstance(cases, list)
        and len(cases) == 7
        and all(row.get("expected_outcome_met") is True for row in cases),
        "Registration cases lack explicit reproduced expectations",
    )
    statuses = Counter(row.get("status") for row in cases)
    _require(
        statuses == {"pass": 4, "rejected_expected": 3},
        "Registration summary disagrees with actual case statuses",
    )
    for row in cases:
        name = row["case"]
        result = check.artifact(f"{name}/case.json")
        _require(
            result.get("status") == row["status"]
            and result.get("expected_outcome_met") is True,
            f"Registration case disagreement: {name}",
        )
        check.case(
            name,
            "expected_rejection"
            if row["status"] == "rejected_expected"
            else "expected_decision",
            {
                key: row.get(key)
                for key in (
                    "status",
                    "expectation",
                    "reason",
                    "independent_holdout",
                    "processing_signed_normal_height",
                )
            },
            f"{name}/case.json",
            data_kind="synthetic_proxy",
            role="development",
        )
    _require(
        isinstance(check.evidence["failures"], list)
        and all(
            row.get("expected") is True
            and row.get("status") == "rejected_expected"
            for row in check.evidence["failures"]
        ),
        "Registration contains an unexpected failure",
    )
    budget = check.artifact("error_budget.json")
    _require(
        metrics.get("error_budget_p95_mm") == budget.get("p95_mm")
        and metrics.get("supports_0_1mm_target")
        == budget.get("supports_target"),
        "Registration error budget disagrees with its verified artifact",
    )


def _validate_reinspection(check):
    metrics, failures = check.evidence["metrics"], check.evidence["failures"]
    _require(
        metrics.get("total_cases") == 12
        and metrics.get("unexpected_failures") == 0
        and metrics.get("all_expectations_met") is True,
        "Reinspection requires 12 cases with all expectations met",
    )
    summary = check.artifact("summary.json")
    _require(summary == metrics, "Reinspection summary and metrics disagree")
    cases = check.artifact("case_results.json").get("cases")
    _require(
        isinstance(cases, list)
        and len(cases) == 12
        and all(
            row.get("expectation_met") is True
            and row.get("status") == row.get("expected_status")
            for row in cases
        ),
        "Reinspection cases lack explicit reproduced expectations",
    )
    _require(
        isinstance(failures, dict)
        and failures.get("unexpected_suite_failure_count") == 0
        and failures.get("unexpected_cases") == []
        and failures.get("expected_rejections") == metrics.get("rejected"),
        "Reinspection failure classification disagrees with its contract",
    )
    _require(
        sum(row["status"] != "verified" for row in cases)
        == metrics["rejected"],
        "Reinspection rejected count disagrees with cases",
    )
    for row in cases:
        name = row["name"]
        result = check.artifact(f"{name}/case.json")
        _require(
            result.get("expectation_met") is True
            and result.get("status") == row["status"],
            f"Reinspection case disagreement: {name}",
        )
        check.case(
            name,
            "expected_decision"
            if row["status"] == "verified"
            else "expected_rejection",
            row,
            f"{name}/case.json",
            data_kind="synthetic_sensor_arrays",
            role="development",
        )


def _validate_planning(task, check):
    metrics = check.evidence["metrics"]
    stress = metrics.get("stress_suite", {})
    count = _count(stress.get("guard_count"), "guard_count")
    _require(
        count > 0 and stress.get("guards_passed") == count,
        "Planning guards were not all verified",
    )
    suite = check.artifact("suite.json")
    cases = suite.get("cases")
    _require(
        isinstance(cases, list)
        and len(cases) == count
        and suite.get("guard_count") == count
        and suite.get("guards_passed") == count
        and all(row.get("guard_passed") is True for row in cases),
        "Planning summary disagrees with explicit guard cases",
    )
    for row in cases:
        check.case(
            row["id"],
            "expected_rejection",
            row,
            "suite.json",
            data_kind="synthetic_software_fixture",
            role="frozen_software_fixture_evaluation",
        )
    if task.require_mujoco_fixture:
        fixture = metrics.get("mujoco_fixture", {})
        actual = check.artifact("mujoco_fixture/result.json")
        _require(
            fixture.get("guard_passed") is True
            and fixture == actual
            and fixture.get("collision_geometry_modified_between_policies")
            is False,
            "Requested MuJoCo planning fixture was not reproduced",
        )
        check.case(
            "mujoco_mid_curve_collision",
            "expected_rejection",
            actual,
            "mujoco_fixture/result.json",
            data_kind="mujoco_collision_fixture",
            role="development",
        )
    _require(
        isinstance(check.evidence["failures"], list),
        "Planning failures must be a list",
    )
    for failure in check.evidence["failures"]:
        _require(
            isinstance(failure, dict)
            and failure.get("kind") == "recorded_hold",
            f"Unexpected planning failure: {failure}",
        )
        check.dispositions.append(
            {"outcome": "observed_quality_hold", "details": failure}
        )


def _validate_scenarios(check):
    evidence, metrics = check.evidence, check.evidence["metrics"]
    generated = _count(metrics.get("generated_episodes"), "generated_episodes")
    _require(
        generated == 10
        and metrics.get("planned_scenarios") == 10
        and metrics.get("generated_frames") == 60
        and metrics.get("geometry_validated_episodes") == generated
        and metrics.get("sensor_validated_episodes") == generated
        and metrics.get("sensor_validated_frames") == 60
        and evidence.get("passed") is True,
        "Default scenarios must actually render and validate 10 episodes/60 frames",
    )
    _require(
        evidence["failures"] == []
        and evidence.get("validation_failures") == [],
        "Scenario generation or validation failed",
    )
    _require(
        metrics.get("normal_false_positive_pixels") == 0
        and metrics.get("normal_predicted_paths") == 0,
        "Scenario normal controls generated false positives or paths",
    )
    cases = evidence.get("results")
    _require(
        isinstance(cases, list) and len(cases) == generated,
        "Scenario results do not match generated episodes",
    )
    for index, row in enumerate(cases):
        check.case(
            row.get("scenario_id", index),
            "generated_and_validated",
            row,
            "evidence.json",
            data_kind="synthetic_cad_render",
            role="frozen_same_cad_new_evaluation"
            if row.get("split") == "scenario_evaluation"
            else "development",
        )
    changeovers = check.artifact("changeover_rehearsal.json").get("cases")
    _require(
        isinstance(changeovers, list)
        and len(changeovers) >= 2
        and all(
            row.get("expected_rejection") is True
            and row.get("main_station_executed") is False
            and row.get("import_result", {}).get("accepted") is False
            for row in changeovers
        ),
        "021/Cao changeover rejection was not explicitly reproduced",
    )
    for row in changeovers:
        check.case(
            f"changeover/{row['input_kind']}",
            "expected_rejection",
            {
                key: row.get(key)
                for key in (
                    "input_kind",
                    "part_id",
                    "cad_id",
                    "rejection_reason",
                    "import_result",
                    "main_station_executed",
                    "new_holdout_claimed",
                )
            },
            "changeover_rehearsal.json",
            data_kind="metadata_and_public_synthetic_interface",
            role="known_regression"
            if row["input_kind"].startswith("cao")
            else "development",
        )


def _validate_process(check):
    metrics, failures = check.evidence["metrics"], check.evidence["failures"]
    _require(
        metrics.get("case_count") == 36
        and metrics.get("policy_run_count") == 72
        and metrics.get("unexpected_failure_count") == 0,
        "Process requires 36 paired cases/72 policy runs without contract errors",
    )
    _require(
        check.artifact("summary.json") == metrics,
        "Process summary and metrics disagree",
    )
    _require(
        isinstance(failures, dict)
        and failures.get("unexpected_suite_failures") == []
        and isinstance(failures.get("process_rejections"), list)
        and isinstance(failures.get("false_releases"), list),
        "Process failure categories are missing or contain contract failures",
    )
    paths = sorted(
        {
            Path(reference["path"])
            for reference in check.references
            if reference["section"] == "artifacts"
            and reference["status"] == "verified"
            and Path(reference["path"]).parent == check.output / "cases"
            and Path(reference["path"]).suffix == ".json"
        }
    )
    _require(len(paths) == 36, "Process must retain every paired case artifact")
    totals = {policy: Counter() for policy in ("adaptive", "fixed")}
    false_releases, rejections, names = [], [], set()
    for path in paths:
        row = read_json(path)
        name = row["case_id"]
        names.add(name)
        _require(
            row.get("contract_failures") == [],
            f"Process case contract failed: {name}",
        )
        for policy in totals:
            result = row[policy]
            _require(
                type(result.get("passed")) is bool
                and type(result.get("audit", {}).get("false_release")) is bool,
                f"Process lacks explicit release audit: {name}/{policy}",
            )
            false = result["audit"]["false_release"]
            totals[policy]["pass_count"] += result["passed"]
            totals[policy]["false_release_count"] += false
            totals[policy]["rejection_count"] += not result["passed"]
            if false:
                _require(
                    row["case"].get("expected_false_release") is True,
                    f"Unexpected process false release: {name}/{policy}",
                )
                false_releases.append((name, policy))
            if not result["passed"]:
                rejections.append((name, policy))
            outcome = (
                "known_false_release"
                if false
                else "expected_decision"
                if result["passed"]
                else "expected_rejection"
            )
            check.case(
                f"{name}/{policy}",
                outcome,
                {
                    "paired_case_id": name,
                    "policy": policy,
                    "stop_reason": result.get("stop_reason"),
                    "passed": result["passed"],
                    "audit": result["audit"],
                    "failures": result.get("failures"),
                    "expected_stop": row["case"].get("expected_stop"),
                    "expected_false_release": row["case"].get(
                        "expected_false_release"
                    ),
                    "worst_positive_residual_mm": result.get(
                        "worst_positive_residual_mm"
                    ),
                    "worst_negative_residual_mm": result.get(
                        "worst_negative_residual_mm"
                    ),
                },
                str(path.relative_to(check.output)),
                data_kind="synthetic_process_proxy",
                role="development",
            )
    _require(len(names) == 36, "Process paired case IDs must be unique")
    for policy, counts in totals.items():
        counts["conforming_release_count"] = (
            counts["pass_count"] - counts["false_release_count"]
        )
        for name, value in counts.items():
            _require(
                metrics.get(policy, {}).get(name) == value,
                f"Process {policy}.{name} disagrees with actual cases",
            )
    _require(
        len(false_releases) == 2
        and sorted(false_releases)
        == sorted(
            (row["case_id"], row["policy"])
            for row in failures["false_releases"]
            if row.get("expected_fault") is True
            and row.get("false_release") is True
        )
        and len(failures["false_releases"]) == 2,
        "The two injected measurement false releases must remain visible",
    )
    _require(
        sorted(rejections)
        == sorted(
            (row["case_id"], row["policy"])
            for row in failures["process_rejections"]
        ),
        "Process rejection catalog disagrees with actual policy runs",
    )


def _validate_reliability(check):
    metrics = check.evidence["metrics"]
    loops, virtual = metrics.get("loops", {}), metrics.get("virtual", {})
    cycles = _count(loops.get("actual_cycles"), "actual_cycles")
    _require(
        cycles >= 100
        and loops.get("mismatches") == 0
        and loops.get("unexpected_exceptions") == 0
        and loops.get("meets_minimum") is True,
        "Reliability requires >=100 actual mock cycles with zero mismatches",
    )
    _require(
        check.evidence["failures"] == []
        and virtual.get("complete") is True
        and virtual.get("meets_target") is True
        and virtual.get("virtual_hours", 0) >= 800
        and virtual.get("real_800h_pending") is True
        and check.evidence["scope"].get("real_800h_pending") is True
        and check.evidence["scope"].get("real_controller_used") is False,
        "Virtual coverage must retain pending real 800-hour acceptance",
    )
    cases = check.artifact("loops/index.jsonl")
    _require(
        len(cases) == cycles and all(row.get("match") is True for row in cases),
        "Reliability aggregate disagrees with explicit loop records",
    )
    for index, row in enumerate(cases):
        check.case(
            row.get("loop_id", index),
            "expected_rejection"
            if not row.get("accepted", False)
            else "mock_execution",
            row,
            "loops/index.jsonl",
            data_kind="mock_controller",
            role="development",
        )


def _validate_perception(check):
    metrics = check.evidence["metrics"]
    frames = _count(metrics.get("known_regression_frames"), "regression_frames")
    pressure = _count(metrics.get("perturbation_count"), "perturbation_count")
    _require(
        frames > 0
        and frames == metrics.get("expected_regression_frames")
        and pressure > 0
        and pressure == metrics.get("expected_perturbation_count")
        and metrics.get("perturbation_failed") == 0
        and metrics.get("comparison", {}).get("frame_count") == frames
        and check.evidence["failures"] == [],
        "Perception comparisons/perturbations are incomplete or failed",
    )
    for name, count in (
        ("comparison.json", frames),
        ("perturbations.json", pressure),
    ):
        cases = check.artifact(name)
        _require(
            isinstance(cases, list)
            and len(cases) == count
            and all(row.get("status") == "evaluated" for row in cases),
            f"Perception {name} disagrees with evaluated records",
        )
        for index, row in enumerate(cases):
            _require(
                set(row.get("results", {}))
                == {"geometric", "learned", "hybrid"},
                f"Perception lacks actual backend results: {name}/{index}",
            )
            check.case(
                f"{name}/{row.get('case')}/{row.get('view')}/{index}",
                "diagnostic_observation",
                {
                    "case": row.get("case"),
                    "view": row.get("view"),
                    "perturbation": row.get("perturbation"),
                    "public_input": row.get(
                        "public_input", row.get("original_input")
                    ),
                    "labels_input": row.get("labels_input"),
                    "actual_backends": row.get("actual_backends"),
                    "paired_observation_hashes": row.get(
                        "paired_observation_hashes"
                    ),
                    "results": {
                        backend: {
                            "binary": result.get("binary"),
                            "object_matching": {
                                key: value
                                for key, value in result.get(
                                    "object_matching", {}
                                ).items()
                                if key != "records"
                            },
                            "synthetic_root_localization": {
                                key: value
                                for key, value in result.get(
                                    "synthetic_root_localization", {}
                                ).items()
                                if key != "records"
                            },
                        }
                        for backend, result in row["results"].items()
                    },
                },
                name,
                data_kind="archived_synthetic_rgbd",
                role="known_regression_perturbed"
                if name == "perturbations.json"
                else "known_regression",
            )


def _main_chain_result(check):
    result = check.artifact("result.json")
    status = check.artifact("status.json")
    config = check.artifact("config.json")
    commands = check.artifact("motion_commands.json")
    cycle = check.artifact("cycle_receipt.json")
    _require(
        status.get("stage") == "complete",
        "Main chain did not complete software execution",
    )
    expected_case = "thin" if check.task == MAIN_CHAIN_NAMES[0] else "normal"
    expected_seed = 120483 if expected_case == "thin" else 9904
    _require(
        result.get("case") == expected_case
        and result.get("seed") == expected_seed,
        "Main-chain identity differs from the requested paired case",
    )
    for key, expected in (
        ("detector", "geometric"),
        ("tool", "belt"),
        ("grasp_setup", "original"),
        ("views", "multi"),
        ("mode", "removal"),
        ("max_passes", 1),
        ("realtime_factor", 0),
    ):
        _require(
            config.get(key) == expected,
            f"Main-chain config {key} differs from the requested control",
        )
    quality = cycle.get("quality", {})
    _require(
        cycle.get("evidence_scope") == "simulation"
        and cycle.get("part") == "097"
        and quality.get("observational_release")
        == result.get("observational_release")
        and quality.get("motion_gate_passed")
        == result.get("motion_gate_passed")
        and quality.get("task_status") == result.get("task_status"),
        "Main-chain cycle receipt disagrees with its simulation disposition",
    )
    if expected_case == "thin":
        execution = result.get("execution", {})
        _require(
            result.get("robot_executed") is True
            and result.get("material_removal_simulated") is True
            and result.get("motion_gate_passed") is True
            and result.get("path_count", 0) > 0
            and result.get("admitted_path_count", 0) > 0
            and isinstance(commands, list)
            and bool(commands)
            and execution.get("simulation_steps", 0) > 0
            and execution.get("simulated_seconds", 0) > 0
            and execution.get("unexpected_contact_count") == 0
            and execution.get("joint_positions_teleported_during_execution")
            is False
            and len(result.get("executions", [])) == 1,
            "Thin main chain lacks a real integrated single-pass motion-gated run",
        )
        recorded = check.artifact("pass_00_execution.json")
        check.artifact_path("replay.json")
        for key in (
            "simulation_steps",
            "simulated_seconds",
            "tracking_trace_p95_mm",
            "surface_tracking_p95_mm",
            "unexpected_contact_count",
            "joint_positions_teleported_during_execution",
        ):
            _require(
                recorded.get(key) == execution.get(key),
                f"Main-chain execution artifact disagrees on {key}",
            )
        limit = (
            config.get("cell", {})
            .get("planning", {})
            .get("tracking_p95_limit_mm")
        )
        _require(
            isinstance(limit, (int, float))
            and math.isfinite(limit)
            and limit > 0
            and all(
                isinstance(execution.get(key), (int, float))
                and math.isfinite(execution[key])
                and 0 <= execution[key] < limit
                for key in ("tracking_trace_p95_mm", "surface_tracking_p95_mm")
            ),
            "Main-chain declared motion gate disagrees with actual tracking",
        )
        with check.artifact_path("pass_00_execution.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            columns = [
                "time_s",
                *[f"q{index}_rad" for index in range(1, 7)],
                *[f"actual_{axis}_m" for axis in "xyz"],
                *[f"target_{axis}_m" for axis in "xyz"],
                "error_mm",
            ]
            _require(
                set(columns).issubset(reader.fieldnames or []),
                "Main-chain feedback lacks timestamps, joints or actual poses",
            )
            first_time = previous_time = None
            rows = 0
            for row in reader:
                numbers = {key: float(row[key]) for key in columns}
                _require(
                    all(math.isfinite(number) for number in numbers.values()),
                    "Main-chain feedback contains non-finite numbers",
                )
                stamp = numbers["time_s"]
                _require(
                    previous_time is None or stamp >= previous_time,
                    "Main-chain feedback clock regressed",
                )
                first_time = stamp if first_time is None else first_time
                previous_time = stamp
                rows += 1
            _require(
                rows >= 2 and previous_time > first_time,
                "Main-chain feedback contains no integrated motion timeline",
            )
    else:
        _require(
            result.get("path_count") == 0
            and result.get("admitted_path_count") == 0
            and result.get("robot_executed") is False
            and result.get("material_removal_simulated") is False
            and commands == []
            and result.get("executions") == []
            and (result.get("execution") or {}).get("simulation_steps", 0) == 0,
            "Normal main-chain control must have zero paths and zero actions",
        )
    return result, config


def _validate_main_chain(check):
    result, config = _main_chain_result(check)
    hold = not result.get("goal_complete") or not result.get(
        "observational_release"
    )
    check.case(
        check.task,
        "observed_quality_hold" if hold else "simulation_completed",
        {
            "software_stage": "complete",
            "task_status": result.get("task_status"),
            "robot_executed": result.get("robot_executed"),
            "material_removal_simulated": result.get(
                "material_removal_simulated"
            ),
            "motion_gate_passed": result.get("motion_gate_passed"),
            "observational_release": result.get("observational_release"),
            "goal_complete": result.get("goal_complete"),
            "stopping_reason": result.get("controller_stopping", {}).get(
                "reason"
            ),
            "execution": result.get("execution", {}),
            "reinspections": result.get("reinspections", []),
            "paired_configuration": {
                key: config.get(key)
                for key in (
                    "detector",
                    "tool",
                    "grasp_setup",
                    "views",
                    "mode",
                    "max_passes",
                    "casting_profile",
                )
            },
            "enterprise_acceptance_passed": False,
        },
        "result.json",
        data_kind="mujoco_synthetic_geometric_removal",
        role="known_regression",
    )
    for failure in check.evidence["failures"]:
        _require(
            failure.get("kind") == "quality_hold",
            f"Unexpected main-chain failure: {failure}",
        )
        check.dispositions.append(
            {"outcome": "observed_quality_hold", "details": failure}
        )


def _validate_contract(task, check):
    validators = {
        "enterprise_acceptance": _validate_acceptance,
        "offline_scenarios": _validate_scenarios,
        "perception_stress": _validate_perception,
        "registration_stress": _validate_registration,
        "reinspection_stress": _validate_reinspection,
        "process_sandbox": _validate_process,
        "reliability_stress": _validate_reliability,
    }
    if task.name == "planning_stress":
        _validate_planning(task, check)
    elif task.name in MAIN_CHAIN_NAMES:
        _validate_main_chain(check)
    elif task.name in validators:
        validators[task.name](check)
    else:
        raise EvidenceError("unknown_task", task.name)


def _main_chain_evidence(task, receipt, source_manifest):
    """Supply a parent evidence envelope around the unchanged workstation CLI."""
    path = task.output / "evidence.json"
    if path.exists() or not task.output.is_dir():
        return
    failures = []
    metrics = {"parent_generated_evidence": True}
    result = {}
    try:
        result = read_json(task.output / "result.json")
        status = read_json(task.output / "status.json")
        metrics.update(
            software_stage=status.get("stage"),
            path_count=result.get("path_count"),
            admitted_path_count=result.get("admitted_path_count"),
            robot_executed=result.get("robot_executed"),
            material_removal_simulated=result.get("material_removal_simulated"),
            motion_gate_passed=result.get("motion_gate_passed"),
            task_status=result.get("task_status"),
            observational_release=result.get("observational_release"),
            goal_complete=result.get("goal_complete"),
            stopping_reason=result.get("controller_stopping", {}).get("reason"),
            simulated_seconds=(result.get("execution") or {}).get(
                "simulated_seconds"
            ),
            remaining_peak_mm=(result.get("execution") or {})
            .get("removal", {})
            .get("remaining_peak_mm"),
        )
        if not result.get("goal_complete") or not result.get(
            "observational_release"
        ):
            failures.append(
                {
                    "kind": "quality_hold",
                    "reason": metrics["stopping_reason"],
                    "task_status": metrics["task_status"],
                    "observational_release": metrics["observational_release"],
                    "software_completion_is_quality_acceptance": False,
                }
            )
    except (OSError, ValueError, TypeError, AttributeError) as error:
        failures.append(_error("main_chain_evidence_error", error))
    artifacts = [
        file_reference(artifact)
        for artifact in sorted(task.output.rglob("*"))
        if artifact.is_file() and artifact != path
    ]
    write_json(
        path,
        {
            "schema": "enterprise-offline-main-chain-evidence-v1",
            "task": task.name,
            "scope": {
                "offline_mujoco_only": True,
                "real_robot_used": False,
                "real_measurements_used": False,
                "material_model": "synthetic geometric removal",
                "evaluation_role": "known_regression",
                "parent_holds_mujoco_lock": True,
                "enterprise_acceptance_passed": False,
                "controller_truth_used": result.get(
                    "controller_stopping", {}
                ).get("truth_used_for_control"),
            },
            "commands": [
                {
                    key: receipt[key]
                    for key in ("argv", "cwd", "environment", "exit_code")
                }
            ],
            "inputs": [file_reference(source_manifest)],
            "metrics": metrics,
            "failures": failures,
            "artifacts": artifacts,
            "real_data_pending": [
                "independent signed before/after measurements on the same 097",
                "camera, TCP, fixture and encoder calibration",
                "ESTUN controller commissioning and feedback",
                "force, removal, wear, temperature and roughness calibration",
            ],
        },
    )


def _lesson(check):
    metrics = check.evidence.get("metrics", {})
    texts = {
        "enterprise_acceptance": (
            "合成决策演练和历史评估完成不能替代双方确认的实物验收；"
            "正常无动作、加工执行、运动门槛、观测放行和企业验收分别计数。"
        ),
        "offline_scenarios": (
            "097 的开发渲染与 021/Cao 的换型拒绝属于不同输入范围。"
            "240 行扩展矩阵只是计划，未渲染的冻结同 CAD 新评价仍待运行；"
            "公开曹训练几何不是新的企业工件测试。"
        ),
        "perception_stress": (
            "二值像素分数、正确类别实例匹配、可观测性和根部定位分别报告。"
            "已见合成 RGB-D 的区域根部代理误差不证明真实 ±0.1 mm。"
        ),
        "registration_stress": (
            f"合成误差预算 P95={metrics.get('error_budget_p95_mm')} mm"
            "（约 0.400 mm），当前预算不支持 ±0.1 mm 目标。"
            "刚体正常区约束、显式单位和同 CAD 身份拒绝须保留。"
        ),
        "planning_stress": (
            "同一连续关节曲线的四点采样会遗漏中段碰撞；"
            "整段拒绝、不同关节分支和预算耗尽不能留下可执行碎片。"
            "软件夹具检查和 MuJoCo 碰撞检查均不证明实机安全。"
        ),
        "reinspection_stress": (
            "signed 双边区间、负超差、负边界风险、非法或缺测锚点均需处置。"
            "有正余量路径和回磨预算不能覆盖这些 hold；"
            "合成补扫只验证覆盖恢复。"
        ),
        "process_sandbox": (
            "隐藏测量偏差在 adaptive 和 fixed 各造成一次误放行。"
            "软件契约复现成功仍保留这两次质量失败，"
            "去除代理、力、柔顺性、磨损、温度和粗糙度均待实测标定。"
        ),
        "reliability_stress": (
            "mock 执行完成与任务质量完成是不同状态。"
            "至少 100 次真实运行的 mock 循环和虚拟 800 小时覆盖"
            "不能冒充同一软件启动的真实 800 小时可靠性记录。"
        ),
    }
    return texts.get(
        check.task,
        "主链软件执行完成、真实仿真积分、运动门槛和质量放行分别核验。"
        "磨后观测或 signed 型面 hold 如实保留，不能称企业达标。",
    )


def _experiences(checks, task_results):
    experiences = []
    receipts = {row["task"]: row for row in task_results}
    for check in checks:
        evidence_path = check.output / "evidence.json"
        source = (
            file_reference(evidence_path) if evidence_path.is_file() else None
        )
        input_scope = {
            "task": check.task,
            "scope": check.evidence.get("scope"),
            "input_evidence": source,
            "input_integrity_ledger": receipts[check.task]["integrity"],
            "verified_input_reference_count": sum(
                reference["section"] == "inputs"
                and reference["status"] == "verified"
                for reference in check.references
            ),
        }
        software_ok = receipts[check.task]["software_contract_passed"]
        for case in check.cases:
            experiences.append(
                {
                    "experience_id": f"{check.task}/{case['case_id']}",
                    "kind": "observed_case",
                    "task": check.task,
                    **case,
                    "input_scope": input_scope,
                    "software_contract_passed": software_ok,
                    "enterprise_acceptance_passed": False,
                    "new_physical_measurement": False,
                }
            )
        for index, disposition in enumerate(check.dispositions):
            experiences.append(
                {
                    "experience_id": f"{check.task}/disposition/{index}",
                    "kind": "observed_disposition",
                    "task": check.task,
                    "outcome": disposition["outcome"],
                    "details": disposition["details"],
                    "source": source,
                    "input_scope": input_scope,
                    "data_kind": "synthetic_or_archived_evidence",
                    "evaluation_role": "known_regression"
                    if check.task == "enterprise_acceptance"
                    else "development",
                    "software_contract_passed": software_ok,
                    "enterprise_acceptance_passed": False,
                }
            )
        if check.evidence:
            experiences.append(
                {
                    "experience_id": f"{check.task}/lesson",
                    "kind": "learned_conclusion",
                    "task": check.task,
                    "outcome": "evidence_limit",
                    "details": {"conclusion": _lesson(check)},
                    "source": source,
                    "input_scope": input_scope,
                    "data_kind": "source_backed_conclusion",
                    "evaluation_role": "development",
                    "software_contract_passed": software_ok,
                    "enterprise_acceptance_passed": False,
                }
            )
            experiences.append(
                {
                    "experience_id": f"{check.task}/pending_real",
                    "kind": "pending_requirement",
                    "task": check.task,
                    "outcome": "pending_real",
                    "details": check.evidence["real_data_pending"],
                    "source": source,
                    "input_scope": input_scope,
                    "data_kind": "pending_real",
                    "evaluation_role": "pending_real",
                    "observed": False,
                    "software_contract_passed": software_ok,
                    "enterprise_acceptance_passed": False,
                }
            )
    return experiences


def _write_experiences(output, experiences):
    with (output / "experience.jsonl").open("w", encoding="utf-8") as stream:
        for row in experiences:
            stream.write(
                json.dumps(
                    row, ensure_ascii=False, allow_nan=False, sort_keys=True
                )
                + "\n"
            )
    fields = [
        "experience_id",
        "kind",
        "task",
        "outcome",
        "data_kind",
        "evaluation_role",
        "software_contract_passed",
        "enterprise_acceptance_passed",
        "source_path",
        "source_sha256",
        "input_scope_json",
        "details_json",
    ]
    with (output / "experience.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in experiences:
            source = row.get("source") or {}
            writer.writerow(
                {
                    **{key: row.get(key) for key in fields if key in row},
                    "source_path": source.get("path"),
                    "source_sha256": source.get("sha256"),
                    "input_scope_json": json.dumps(
                        row.get("input_scope"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    "details_json": json.dumps(
                        row["details"], ensure_ascii=False, allow_nan=False
                    ),
                }
            )


def _catalog(task_results, experiences):
    catalog = {
        "schema": "enterprise-offline-failure-catalog-v1",
        "software_errors": [
            {
                "task": row["task"],
                "receipt": row["receipt"],
                **failure,
            }
            for row in task_results
            for failure in row["failures"]
        ],
        "expected_rejections": [],
        "observed_quality_holds": [],
        "known_false_releases": [],
        "pending_real": [],
    }
    categories = {
        "expected_rejection": "expected_rejections",
        "observed_quality_hold": "observed_quality_holds",
        "known_false_release": "known_false_releases",
        "pending_real": "pending_real",
    }
    for experience in experiences:
        category = categories.get(experience["outcome"])
        if category:
            catalog[category].append(
                {
                    key: experience.get(key)
                    for key in (
                        "experience_id",
                        "task",
                        "outcome",
                        "source",
                        "input_scope",
                        "details",
                        "data_kind",
                        "evaluation_role",
                    )
                }
            )
    return catalog


def _next_experiments(checks):
    descriptions = (
        (
            "real_error_budget",
            "用独立正常区、留出锚点和同件扫描量测相机/转台/夹具/TCP误差。",
            "原始 PCD/PLY、单位、同件身份、CAD 哈希和独立参考。",
            "报告独立 signed 误差及全链预算；不可用合成约 0.400 mm 预算证明 0.1 mm。",
        ),
        (
            "hidden_measurement_bias",
            "用独立同点磨前磨后量测识别隐藏偏差，再冻结测量与放行规则。",
            "独立量具不确定度、原始 signed 数据、力/进给/磨损记录。",
            "两次已见误放行保持回归；新增整件留出确认零意外误放行。",
        ),
        (
            "frozen_same_cad_evaluation",
            "按登记但未渲染的同 CAD 新种子完成独立渲染和冻结模型评价。",
            "冻结代码/模型/配置哈希、未使用的实体组与完整 RGB-D。",
            "实际生成帧、可观测性和类别实例分别计数；240 行计划不计实跑。",
        ),
        (
            "signed_hold_regression",
            "保留负超差、负边界风险、非法锚点和主链观测 hold 的回归。",
            "signed 原始锚点数组和对应主链状态/执行回执。",
            "即使正余量路径及预算仍存在，也不能绕过禁止自动回磨的处置。",
        ),
        (
            "changeover_binding",
            "先补齐 021 或曹基础几何的独立工件/夹具/坐标/碰撞注册。",
            "经确认的制造阶段、基准、装夹位姿、相机和加工许可。",
            "身份与整段路径全部核验后才讨论换型；不能改名冒充 097。",
        ),
        (
            "real_800h",
            "在同一 boot_id 上采集真实单机墙钟健康日志和恢复回执。",
            "真实时间戳、连续心跳、重启/中断日志和控制器反馈。",
            "真实连续 800 小时与虚拟覆盖、执行完成和质量完成分别报告。",
        ),
    )
    sources = [
        {"task": check.task, **file_reference(check.output / "evidence.json")}
        for check in checks
        if (check.output / "evidence.json").is_file()
    ]
    return {
        "schema": "enterprise-offline-next-experiments-v1",
        "experiments_executed": False,
        "real_experiments_authorized_by_this_runner": False,
        "experiments": [
            {
                "id": name,
                "proposal": proposal,
                "required_inputs": inputs,
                "acceptance_contract": contract,
                "status": "pending",
                "sources": sources,
            }
            for name, proposal, inputs, contract in descriptions
        ],
    }


def _write_report(output, summary, experiences):
    lines = [
        "# 统一离线证据运行报告",
        "",
        f"- 软件套件成功：`{str(summary['software_suite_passed']).lower()}`。",
        "- 企业实物验收：`false`；真实 800 小时、标定和同点量测仍待补。",
        f"- 源码运行中未变化：`{str(summary['source_unchanged']).lower()}`。",
        f"- 实际执行子进程：{summary['executed_task_count']}；串行执行。",
        "",
        "| 任务 | 软件契约 | 退出码 | 墙钟 s | 峰值 RSS KiB | 质量/预期处置 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["tasks"]:
        lines.append(
            f"| {row['task']} | {row['software_contract_passed']} | "
            f"{row['exit_code']} | {row['wall_seconds']:.3f} | "
            f"{row['peak_rss_kib']} | {row['retained_disposition_count']} |"
        )
    lines.extend(
        [
            "",
            "每任务真实 argv、环境、起止、日志和资源均见 `receipts/`。",
            "峰值来自独立子进程的 `os.wait4`，单位为 Linux KiB。",
            "process CLI 的受限输出根目录由实际回执中的 output 字段注明。",
            "",
            "## 已见结果与范围",
            "",
        ]
    )
    for row in experiences:
        if row["kind"] == "learned_conclusion":
            source = row["source"]
            lines.append(
                f"- {row['task']}：{row['details']['conclusion']} "
                f"来源 `{source['path']}`，SHA256 `{source['sha256']}`。"
            )
    lines.extend(
        [
            "",
            "逐案例、故障、拒绝和主链 hold 见 `experience.jsonl`/`experience.csv`；",
            "`failure_catalog.json` 区分软件异常、预期拒绝、质量 hold、",
            "已知误放行和真实数据待补。`next_experiments.json` 只列下一步实验。",
            "经验条数包含结论及待补要求，不是独立实物件数。",
            "计划矩阵、未渲染新评价和虚拟时钟均不计为已跑实物案例。",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    output: Path,
    *,
    tasks=None,
    task_timeout=1800.0,
    include_main_chain=False,
    root=ROOT,
    python=sys.executable,
    progress=None,
) -> dict:
    """Execute the authorized offline tasks and publish a verified new bundle."""
    output, root = Path(output).resolve(), Path(root).resolve()
    names = tuple(TASK_NAMES if tasks is None else tasks)
    if (
        not names
        or len(set(names)) != len(names)
        or set(names) - set(TASK_NAMES)
    ):
        raise ValueError(
            "Select unique names from the fixed eight offline tasks"
        )
    if not math.isfinite(task_timeout) or task_timeout <= 0:
        raise ValueError("--task-timeout must be finite and positive")
    if any(
        output.is_relative_to(root / name)
        for name in ("src", "tests", "config")
    ):
        raise ValueError("Output must be outside the frozen source trees")
    if output.exists():
        raise ValueError(f"--output must be a new directory: {output}")
    before = snapshot_sources(root)
    output.mkdir(parents=True, exist_ok=False)
    (output / "tasks").mkdir()
    before_path = output / "sources_before.json"
    write_json(before_path, before)
    freeze = SourceFreeze(root, before)
    suite_id = uuid.uuid4().hex[:12]
    scheduled = build_tasks(
        output,
        names,
        root=root,
        python=python,
        include_main_chain=include_main_chain,
        suite_id=suite_id,
    )
    started_at, started = _utc_now(), time.monotonic()
    results, checks = [], []
    for task in scheduled:
        receipt_directory = output / "receipts" / task.name
        if progress:
            progress(f"{task.name}: starting {list(task.argv)!r}")
        if freeze.check(force=True):
            receipt = execute_task(
                task,
                receipt_directory,
                root=root,
                freeze=freeze,
                timeout=task_timeout,
                progress=progress,
            )
            if task.name in MAIN_CHAIN_NAMES:
                _main_chain_evidence(task, receipt, before_path)
            check = validate_evidence(task, root=root)
        else:
            receipt_directory.mkdir(parents=True, exist_ok=False)
            receipt = {
                "task": task.name,
                "argv": list(task.argv),
                "cwd": str(root),
                "environment": ENVIRONMENT,
                "output": str(task.output),
                "pid": None,
                "exit_code": None,
                "wall_seconds": 0.0,
                "peak_rss_kib": None,
                "failures": [
                    _error("not_run_source_changed", "Frozen source changed")
                ],
            }
            write_json(receipt_directory / "receipt.json", receipt)
            check = EvidenceCheck(task.name, task.output)
        checks.append(check)
        failures = [*receipt["failures"], *check.errors]
        write_json(
            receipt_directory / "integrity.json",
            {
                "task": task.name,
                "references": check.references,
                "errors": check.errors,
            },
        )
        evidence_path = task.output / "evidence.json"
        results.append(
            {
                "task": task.name,
                "output": str(task.output),
                "argv": list(task.argv),
                "executed": receipt["pid"] is not None,
                "exit_code": receipt["exit_code"],
                "wall_seconds": receipt["wall_seconds"],
                "peak_rss_kib": receipt["peak_rss_kib"],
                "software_contract_passed": receipt["exit_code"] == 0
                and not failures,
                "enterprise_acceptance_passed": False,
                "retained_disposition_count": sum(
                    row["outcome"]
                    in {
                        "expected_rejection",
                        "observed_quality_hold",
                        "known_false_release",
                    }
                    for row in [*check.cases, *check.dispositions]
                ),
                "verified_file_reference_count": sum(
                    reference["status"] == "verified"
                    for reference in check.references
                ),
                "metrics": check.evidence.get("metrics", {}),
                "real_data_pending": check.evidence.get("real_data_pending"),
                "source_evidence": file_reference(evidence_path)
                if evidence_path.is_file()
                else None,
                "receipt": file_reference(receipt_directory / "receipt.json"),
                "integrity": file_reference(
                    receipt_directory / "integrity.json"
                ),
                "failures": failures,
            }
        )
        if progress:
            progress(
                f"{task.name}: software_contract_passed="
                f"{results[-1]['software_contract_passed']}, "
                f"exit={receipt['exit_code']}, failures={len(failures)}"
            )
    after = snapshot_sources(root)
    freeze.check(force=True)
    write_json(output / "sources_after.json", after)
    software_passed = not freeze.changes and all(
        row["software_contract_passed"] for row in results
    )
    experiences = _experiences(checks, results)
    summary = {
        "schema": "enterprise-offline-runner-summary-v1",
        "suite_id": suite_id,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "wall_seconds": time.monotonic() - started,
        "status": "complete" if software_passed else "failed",
        "software_suite_passed": software_passed,
        "enterprise_acceptance_passed": False,
        "enterprise_targets_evidenced": False,
        "real_800h_pending": True,
        "virtual_800h_is_real_800h": False,
        "source_unchanged": not freeze.changes,
        "changed_source_files": freeze.changes,
        "source_tree_sha256_before": before["tree_sha256"],
        "source_tree_sha256_after": after["tree_sha256"],
        "requested_tasks": list(names),
        "full_default_suite": set(names) == set(TASK_NAMES),
        "include_main_chain": include_main_chain,
        "requested_task_count": len(scheduled),
        "executed_task_count": sum(row["executed"] for row in results),
        "serial_execution": True,
        "environment": dict(ENVIRONMENT),
        "tasks": results,
        "experience_count": len(experiences),
        "experience_counts_by_kind": dict(
            Counter(row["kind"] for row in experiences)
        ),
        "observed_case_counts_by_task": dict(
            Counter(
                row["task"]
                for row in experiences
                if row["kind"] == "observed_case"
            )
        ),
        "new_physical_cases": 0,
        "planned_matrix_rows_counted_as_executed": 0,
    }
    write_json(output / "summary.json", summary)
    _write_experiences(output, experiences)
    catalog = _catalog(results, experiences)
    if freeze.changes:
        catalog["software_errors"].append(
            _error(
                "source_changed", "Frozen source changed", files=freeze.changes
            )
        )
    write_json(output / "failure_catalog.json", catalog)
    write_json(output / "next_experiments.json", _next_experiments(checks))
    _write_report(output, summary, experiences)
    deliverables = (
        "sources_before.json",
        "sources_after.json",
        "summary.json",
        "report.md",
        "experience.jsonl",
        "experience.csv",
        "failure_catalog.json",
        "next_experiments.json",
    )
    artifacts = [file_reference(output / name) for name in deliverables]
    artifacts.extend(
        file_reference(path)
        for path in sorted((output / "receipts").rglob("*"))
        if path.is_file()
    )
    write_json(
        output / "evidence.json",
        {
            "schema": "enterprise-offline-runner-evidence-v1",
            "task": "unified serial frozen offline evidence runner",
            "scope": {
                "offline_only": True,
                "serial_execution": True,
                "real_robot_used": False,
                "external_messages_sent": False,
                "servers_started": False,
                "new_physical_measurements": 0,
                "enterprise_acceptance_passed": False,
                "main_chain_included": include_main_chain,
            },
            "commands": [
                {
                    "task": row["task"],
                    "argv": row["argv"],
                    "cwd": str(root),
                    "environment": ENVIRONMENT,
                    "executed": row["executed"],
                    "exit_code": row["exit_code"],
                    "receipt": row["receipt"],
                }
                for row in results
            ],
            "inputs": [
                file_reference(before_path),
                file_reference(output / "sources_after.json"),
                *[
                    row["source_evidence"]
                    for row in results
                    if row["source_evidence"] is not None
                ],
            ],
            "metrics": {
                key: summary[key]
                for key in (
                    "software_suite_passed",
                    "enterprise_acceptance_passed",
                    "source_unchanged",
                    "requested_task_count",
                    "executed_task_count",
                    "wall_seconds",
                    "new_physical_cases",
                    "real_800h_pending",
                    "virtual_800h_is_real_800h",
                )
            },
            "failures": catalog["software_errors"],
            "artifacts": artifacts,
            "real_data_pending": {
                check.task: check.evidence.get(
                    "real_data_pending", ["task not verified"]
                )
                for check in checks
            },
        },
    )
    return summary


def main(argv=None):
    """CLI entry point; any unexpected error or missing evidence exits nonzero."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", choices=TASK_NAMES)
    parser.add_argument("--task-timeout", type=float, default=1800.0)
    parser.add_argument("--include-main-chain", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run(
            args.output,
            tasks=args.tasks,
            task_timeout=args.task_timeout,
            include_main_chain=args.include_main_chain,
            progress=lambda message: print(message, flush=True),
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"enterprise_offline: {error}\n")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "software_suite_passed": summary["software_suite_passed"],
                "enterprise_acceptance_passed": False,
                "source_unchanged": summary["source_unchanged"],
                "executed_task_count": summary["executed_task_count"],
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        flush=True,
    )
    return 0 if summary["software_suite_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
