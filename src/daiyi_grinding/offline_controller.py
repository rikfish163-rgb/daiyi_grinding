"""Offline ESTUN controller mock with explicit receipts and safe recovery.

The module never opens a physical socket, imports MuJoCo or sends a robot
command. It validates one accepted ``motion_commands.json`` bundle, then
replays synthetic feedback through an explicit mock state machine. The
candidate ``estun_path_preview.csv`` is bound by hash but is never treated as
a controller program.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from . import project

PLAN_SCHEMA = "daiyi-offline-controller-plan-v1"
RECEIPT_SCHEMA = "daiyi-controller-evidence-v1"
FEEDBACK_SCHEMA = "daiyi-controller-feedback-v1"
CHECKPOINT_SCHEMA = "daiyi-controller-checkpoint-v1"
QUALITY_SCHEMA = "daiyi-independent-quality-v1"
PROGRAM_SCHEMA = "daiyi-mock-controller-program-v1"
MOCK_CONTROLLER_ID = "mock:estun-ier220-2700-offline-v1"
DEFAULT_ROBOT_XML = project.ROOT / "assets/ier220_workstation/scene.xml"
SIX_AXES = ("j1", "j2", "j3", "j4", "j5", "j6")
CANONICAL_UNITS = {
    "joint_angles": "rad",
    "pose_translation": "m",
    "point": "m",
    "preview_positions": "mm",
}
SUPPORT_PHASES = {
    "grasp",
    "lift",
    "transfer",
    "return",
    "retract",
    "release",
    "home",
}
MACHINING_PHASES = {"approach", "trace", "retract"}
FEEDBACK_STATUSES = {"executing", "segment_complete", "completed", "fault"}
TERMINAL_STATES = {"FINISHED", "CANCELLED", "REJECTED"}
RECOVERABLE_STATES = {"PLANNED", "CONFIRMED", "EXECUTING", "HELD"}
MAX_PLAN_SECONDS = 86_400.0
MAX_FEEDBACK_LAG_SECONDS = 5.0
MAX_FEEDBACK_GAP_SECONDS = 5.0
MAX_POSITION_M = 10.0
MAX_TRANSLATION_M = 1_000.0


class ControllerError(RuntimeError):
    """A rejected offline controller operation with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PlanRejected(ControllerError):
    """A plan, source file or coordinate binding is not executable."""


class StateRejected(ControllerError):
    """The requested operation is invalid in the current state."""


class FeedbackRejected(StateRejected):
    """A feedback batch is stale, duplicated with changes or out of order."""


class CompletionRejected(StateRejected):
    """An execution callback does not prove a complete execution."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(
            "completion_incomplete",
            "完成回调不能证明获准段执行和反馈均已齐全",
        )


class RecoveryRejected(StateRejected):
    """A held controller was not resumed with explicit, revalidated evidence."""


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanRejected("duplicate_json_key", f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise PlanRejected("nonfinite_json", f"JSON 包含非有限数：{value}")


def _json_value(path, role):
    path = Path(path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise PlanRejected(
            "missing_source", f"{role} 不存在或不可读取：{path}"
        ) from error
    try:
        value = json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanRejected(
            "invalid_json", f"{role} 不是有效 UTF-8 JSON：{path}"
        ) from error
    return value, content, hashlib.sha256(content).hexdigest()


def _canonical_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _pretty_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def sha256_file(path):
    """Hash one local file without loading large feedback files in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(value, name):
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise PlanRejected("missing_identity", f"{name} 缺失或含首尾空白")
    return value


def _finite(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanRejected("invalid_number", f"{name} 须为有限数值")
    try:
        number = float(value)
    except OverflowError as error:
        raise PlanRejected(
            "nonfinite_number", f"{name} 超出有限数值范围"
        ) from error
    if not math.isfinite(number):
        raise PlanRejected("nonfinite_number", f"{name} 不能为非有限数")
    if minimum is not None and number < minimum:
        raise PlanRejected("invalid_number", f"{name} 小于允许下界")
    if maximum is not None and number > maximum:
        raise PlanRejected("invalid_number", f"{name} 大于允许上界")
    return number


def _integer(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanRejected("invalid_integer", f"{name} 须为整数")
    if value < minimum:
        raise PlanRejected("invalid_integer", f"{name} 小于允许下界")
    return value


def _vector(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise PlanRejected("invalid_vector", f"{name} 须为 {length} 维数组")
    return tuple(
        _finite(item, f"{name}[{index}]") for index, item in enumerate(value)
    )


def _pose(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PlanRejected("invalid_pose", f"{name} 须为 4x4 齐次矩阵")
    rows = []
    for index, row in enumerate(value):
        rows.append(_vector(row, 4, f"{name}[{index}]"))
    if any(
        abs(rows[3][index] - (1.0 if index == 3 else 0.0)) > 1e-9
        for index in range(4)
    ):
        raise PlanRejected("invalid_pose", f"{name} 底行不是 [0,0,0,1]")
    rotation = [row[:3] for row in rows[:3]]
    for first in range(3):
        for second in range(3):
            dot = sum(
                rotation[first][k] * rotation[second][k] for k in range(3)
            )
            expected = 1.0 if first == second else 0.0
            if abs(dot - expected) > 1e-6:
                raise PlanRejected("invalid_rotation", f"{name} 旋转矩阵不正交")
    determinant = (
        rotation[0][0]
        * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1]
        * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2]
        * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if abs(determinant - 1.0) > 1e-6:
        raise PlanRejected("invalid_rotation", f"{name} 旋转行列式不为 +1")
    return tuple(rows)


@dataclass(frozen=True)
class JointLimits:
    """Six-axis ranges parsed from the imported scene XML, without MuJoCo."""

    lower: tuple[float, ...]
    upper: tuple[float, ...]
    joint_ranges: tuple[tuple[float, float], ...]
    ctrl_ranges: tuple[tuple[float, float] | None, ...]
    source_path: str
    source_sha256: str

    def contains(self, q):
        return all(
            self.lower[index] - 1e-9 <= q[index] <= self.upper[index] + 1e-9
            for index in range(6)
        )

    def to_dict(self):
        return {
            "lower_rad": list(self.lower),
            "upper_rad": list(self.upper),
            "joint_range_rad": [list(item) for item in self.joint_ranges],
            "ctrl_range_rad": [
                None if item is None else list(item)
                for item in self.ctrl_ranges
            ],
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source": "ESTUN iER220-2700 imported scene XML joint/ctrl ranges",
        }


def load_joint_limits(xml_path=DEFAULT_ROBOT_XML):
    """Read the six hinge ranges and matching actuator ctrl ranges."""
    path = Path(xml_path).resolve()
    try:
        content = path.read_bytes()
    except OSError as error:
        raise PlanRejected(
            "missing_robot_xml", f"机器人 XML 不存在：{path}"
        ) from error
    digest = hashlib.sha256(content).hexdigest()
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise PlanRejected(
            "invalid_robot_xml", f"机器人 XML 无法解析：{path}"
        ) from error
    compiler = root.find("compiler")
    if compiler is None or compiler.get("angle") != "radian":
        raise PlanRejected(
            "robot_unit_mismatch",
            "离线限位解析要求 XML 显式声明 compiler angle=radian",
        )
    joint_ranges = {}
    for joint in root.iter("joint"):
        name = joint.get("name")
        if name not in SIX_AXES:
            continue
        if name in joint_ranges or joint.get("type", "hinge") != "hinge":
            raise PlanRejected(
                "invalid_joint_limits", f"{name} 须为唯一的 hinge 关节"
            )
        raw = joint.get("range")
        if raw is None:
            raise PlanRejected("joint_limits_missing", f"{name} 未声明 range")
        parts = raw.split()
        if len(parts) != 2:
            raise PlanRejected("invalid_joint_limits", f"{name} range 格式错误")
        try:
            low = _finite(float(parts[0]), f"{name}.range[0]")
            high = _finite(float(parts[1]), f"{name}.range[1]")
        except ValueError as error:
            raise PlanRejected(
                "invalid_joint_limits", f"{name} range 须为数值"
            ) from error
        if low >= high:
            raise PlanRejected("invalid_joint_limits", f"{name} range 非递增")
        joint_ranges[name] = (low, high)
    missing = [name for name in SIX_AXES if name not in joint_ranges]
    if missing:
        raise PlanRejected(
            "joint_limits_missing", f"XML 缺少六轴限位：{missing}"
        )
    ctrl_ranges = {}
    for actuator in root.iter():
        if actuator.tag not in {"position", "motor", "general"}:
            continue
        name = actuator.get("joint")
        raw = actuator.get("ctrlrange")
        if name not in SIX_AXES or raw is None:
            continue
        if actuator.tag != "position" or name in ctrl_ranges:
            raise PlanRejected(
                "actuator_unit_mismatch",
                f"{name} 的角度 ctrlrange 须来自唯一 position 执行器",
            )
        parts = raw.split()
        if len(parts) != 2:
            raise PlanRejected(
                "invalid_joint_limits", f"{name} ctrlrange 格式错误"
            )
        try:
            low = _finite(float(parts[0]), f"{name}.ctrlrange[0]")
            high = _finite(float(parts[1]), f"{name}.ctrlrange[1]")
        except ValueError as error:
            raise PlanRejected(
                "invalid_joint_limits", f"{name} ctrlrange 须为数值"
            ) from error
        if low >= high:
            raise PlanRejected(
                "invalid_joint_limits", f"{name} ctrlrange 非递增"
            )
        ctrl_ranges[name] = (low, high)
    ordered_joint = tuple(joint_ranges[name] for name in SIX_AXES)
    ordered_ctrl = tuple(ctrl_ranges.get(name) for name in SIX_AXES)
    lower = []
    upper = []
    for joint, ctrl in zip(ordered_joint, ordered_ctrl):
        lower.append(max(joint[0], ctrl[0]) if ctrl else joint[0])
        upper.append(min(joint[1], ctrl[1]) if ctrl else joint[1])
    if any(low >= high for low, high in zip(lower, upper)):
        raise PlanRejected(
            "invalid_joint_limits", "joint/ctrlrange 交集须有正宽度"
        )
    return JointLimits(
        lower=tuple(lower),
        upper=tuple(upper),
        joint_ranges=ordered_joint,
        ctrl_ranges=ordered_ctrl,
        source_path=str(path),
        source_sha256=digest,
    )


@dataclass(frozen=True)
class PlanIdentity:
    """Part, fixture, tool, frame and units bound to one plan instance."""

    part_id: str
    serial: str
    cad_revision: str
    fixture_id: str
    fixture_revision: str
    tool_id: str
    frame: str
    units: dict[str, str]

    def __post_init__(self):
        for name in (
            "part_id",
            "serial",
            "cad_revision",
            "fixture_id",
            "fixture_revision",
            "tool_id",
            "frame",
        ):
            _text(getattr(self, name), name)
        if (
            not isinstance(self.units, dict)
            or dict(self.units) != CANONICAL_UNITS
        ):
            raise PlanRejected(
                "unit_mismatch",
                "计划单位须明确为 rad / m / m / mm，控制器不做隐式换算",
            )
        object.__setattr__(self, "units", dict(self.units))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise PlanRejected("missing_identity", "工件身份须为对象")
        units = value.get("units")
        if units is None:
            raise PlanRejected("missing_units", "工件身份缺少显式单位")
        if not isinstance(units, dict):
            raise PlanRejected("unit_mismatch", "工件单位须为显式对象")
        return cls(
            part_id=_text(value.get("part_id"), "part_id"),
            serial=_text(value.get("serial"), "serial"),
            cad_revision=_text(value.get("cad_revision"), "cad_revision"),
            fixture_id=_text(value.get("fixture_id"), "fixture_id"),
            fixture_revision=_text(
                value.get("fixture_revision"), "fixture_revision"
            ),
            tool_id=_text(value.get("tool_id"), "tool_id"),
            frame=_text(value.get("frame"), "frame"),
            units=dict(units),
        )

    def to_dict(self):
        return {
            "part_id": self.part_id,
            "serial": self.serial,
            "cad_revision": self.cad_revision,
            "fixture_id": self.fixture_id,
            "fixture_revision": self.fixture_revision,
            "tool_id": self.tool_id,
            "frame": self.frame,
            "units": dict(self.units),
        }


@dataclass(frozen=True)
class Command:
    """One accepted joint command. Angles are rad; pose and point are m."""

    index: int
    t: float
    q: tuple[float, ...]
    pose: tuple[tuple[float, ...], ...]
    phase: str
    attached: bool
    point: tuple[float, ...] | None
    detection_id: int
    segment_id: int

    def normalized(self):
        return {
            "t": self.t,
            "q": list(self.q),
            "pose": [list(row) for row in self.pose],
            "phase": self.phase,
            "attached": self.attached,
            "point": None if self.point is None else list(self.point),
            "detection_id": self.detection_id,
            "segment_id": self.segment_id,
        }


@dataclass(frozen=True)
class PlanSegment:
    """A complete support or preflight-passed machining command block."""

    segment_id: int
    kind: str
    admitted: bool
    command_indices: tuple[int, ...]
    command_blocks: tuple[tuple[int, ...], ...]
    command_count: int
    t_start: float
    t_end: float
    command_sha256: str
    phases: tuple[str, ...]

    def to_dict(self):
        return {
            "segment_id": self.segment_id,
            "kind": self.kind,
            "admitted": self.admitted,
            "command_count": self.command_count,
            "command_blocks": [
                [block[0], block[-1]] for block in self.command_blocks
            ],
            "t_start_s": self.t_start,
            "t_end_s": self.t_end,
            "command_sha256": self.command_sha256,
            "phases": sorted(set(self.phases)),
        }


@dataclass(frozen=True)
class Artifact:
    """One hashed input. Only accepted commands are executable."""

    role: str
    path: str
    sha256: str
    executable: bool
    source: str
    bytes: int

    def to_dict(self):
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "executable": self.executable,
            "source": self.source,
            "bytes": self.bytes,
        }


def _identity_from_result(result, directory):
    """Derive a reproducible simulation identity, recording its provenance."""
    if not isinstance(result, dict):
        raise PlanRejected("invalid_result", "result.json 须为对象")
    part_id = _text(result.get("cad_workpiece"), "result.cad_workpiece")
    frame = _text(result.get("coordinate_frame"), "result.coordinate_frame")
    tool = result.get("tool")
    if not isinstance(tool, dict):
        raise PlanRejected("missing_identity", "result.tool 缺失")
    tool_id = _text(tool.get("key"), "result.tool.key")
    profile = result.get("workpiece_profile")
    cad_revision = (
        _text(profile.get("sha256"), "result.workpiece_profile.sha256")
        if isinstance(profile, dict)
        else part_id
    )
    serial = result.get("serial")
    serial_source = "result.json:serial"
    if not isinstance(serial, str) or not serial.strip():
        serial = f"sim-run:{Path(directory).name}"
        serial_source = "derived from run directory name; no physical serial"
    grasp = result.get("grasp_setup")
    fixture_id = (
        f"grasp_setup:{grasp}"
        if isinstance(grasp, str) and grasp.strip()
        else "fixture:unverified-simulation"
    )
    yaw = result.get("fixture_loading_yaw_deg")
    fixture_revision = (
        f"loading_yaw_deg:{yaw}"
        if yaw is not None
        else "fixture-revision:unverified"
    )
    identity = PlanIdentity(
        part_id=part_id,
        serial=serial,
        cad_revision=cad_revision,
        fixture_id=fixture_id,
        fixture_revision=fixture_revision,
        tool_id=tool_id,
        frame=frame,
        units=dict(CANONICAL_UNITS),
    )
    sources = {
        "part_id": "result.json:cad_workpiece",
        "serial": serial_source,
        "cad_revision": "result.json:workpiece_profile.sha256",
        "fixture_id": "result.json:grasp_setup",
        "fixture_revision": "result.json:fixture_loading_yaw_deg",
        "tool_id": "result.json:tool.key",
        "frame": "result.json:coordinate_frame",
        "units": "offline controller fixed catalog; no conversion performed",
    }
    return identity, sources


def _parse_commands(values, limits):
    if not isinstance(values, list):
        raise PlanRejected("invalid_commands", "motion_commands.json 须为数组")
    commands = []
    previous = -math.inf
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise PlanRejected("invalid_commands", f"命令 {index} 须为对象")
        stamp = _finite(value.get("t"), f"command[{index}].t", minimum=0)
        if stamp <= previous:
            raise PlanRejected("timeline_order", f"命令 {index} 时间未严格递增")
        if stamp > MAX_PLAN_SECONDS:
            raise PlanRejected("timeline_too_long", "计划时长超过 24 小时")
        previous = stamp
        q = _vector(value.get("q"), 6, f"command[{index}].q")
        if not limits.contains(q):
            raise PlanRejected(
                "joint_limit",
                f"命令 {index} 超出 XML 六轴限位：{list(q)}",
            )
        pose = _pose(value.get("pose"), f"command[{index}].pose")
        translation = [pose[axis][3] for axis in range(3)]
        if any(abs(item) > MAX_TRANSLATION_M for item in translation):
            raise PlanRejected(
                "coordinate_range", f"命令 {index} 世界平移超出合理范围"
            )
        phase = _text(value.get("phase"), f"command[{index}].phase")
        attached = value.get("attached")
        if not isinstance(attached, bool):
            raise PlanRejected(
                "invalid_commands", f"命令 {index}.attached 须为布尔值"
            )
        point_value = value.get("point")
        point = None
        if point_value is not None:
            point = _vector(point_value, 3, f"command[{index}].point")
            if any(abs(item) > MAX_POSITION_M for item in point):
                raise PlanRejected(
                    "coordinate_range",
                    f"命令 {index} 工件坐标点超出合理范围",
                )
        detection = _integer(
            value.get("detection_id"), f"command[{index}].detection_id"
        )
        segment = _integer(
            value.get("segment_id"), f"command[{index}].segment_id"
        )
        commands.append(
            Command(
                index=index,
                t=stamp,
                q=q,
                pose=pose,
                phase=phase,
                attached=attached,
                point=point,
                detection_id=detection,
                segment_id=segment,
            )
        )
    return tuple(commands)


def _parse_preflight(value, commands, support_segments, command_file_hash):
    if not isinstance(value, dict):
        raise PlanRejected("invalid_preflight", "preflight.json 须为对象")
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise PlanRejected("invalid_preflight", "preflight.rows 须为数组")
    checked = _integer(
        value.get("checked_commands"), "preflight.checked_commands"
    )
    if commands:
        if checked != len(commands):
            raise PlanRejected(
                "preflight_command_mismatch",
                "预检覆盖命令数与获准命令文件不一致",
            )
    elif checked != 0:
        raise PlanRejected(
            "preflight_command_mismatch", "空命令计划却声明了已检查命令"
        )
    passed = set()
    blocked = set()
    row_summary = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PlanRejected("invalid_preflight", f"预检行 {index} 须为对象")
        segment = _integer(
            row.get("segment_id"),
            f"preflight.rows[{index}].segment_id",
            minimum=1,
        )
        if segment in passed or segment in blocked:
            raise PlanRejected(
                "duplicate_preflight_segment", f"预检段 {segment} 重复"
            )
        status = _text(row.get("status"), f"preflight.rows[{index}].status")
        if status == "passed":
            passed.add(segment)
        elif status in {"blocked", "failed", "skipped"}:
            blocked.add(segment)
        else:
            raise PlanRejected(
                "invalid_preflight_status", f"未知预检状态：{status}"
            )
        row_summary.append(
            {
                "segment_id": segment,
                "status": status,
                "reason": row.get("reason"),
                "detection_id": row.get("detection_id"),
                "coverage": row.get("coverage"),
                "command_count": row.get("command_count"),
                "command_sha256": row.get("command_sha256"),
            }
        )
    machining = {
        command.segment_id
        for command in commands
        if command.segment_id not in support_segments
    }
    if machining - passed:
        raise PlanRejected(
            "unapproved_segment",
            f"命令包含未通过整段预检的段：{sorted(machining - passed)}",
        )
    if passed - machining:
        raise PlanRejected(
            "preflight_passed_segment_missing_commands",
            f"预检通过段缺少完整命令：{sorted(passed - machining)}",
        )
    if blocked & machining:
        raise PlanRejected(
            "blocked_segment_execution", "被拦截段不得出现在控制命令中"
        )
    digest = hashlib.sha256(
        _canonical_bytes([command.normalized() for command in commands])
    ).hexdigest()
    if (
        "commands_sha256" in value
        and value["commands_sha256"] != digest
    ):
        raise PlanRejected(
            "preflight_binding_mismatch", "预检未绑定同一完整命令列表"
        )
    if (
        "command_file_sha256" in value
        and value["command_file_sha256"] != command_file_hash
    ):
        raise PlanRejected(
            "preflight_binding_mismatch", "预检绑定的命令文件哈希不一致"
        )
    for row in rows:
        if row["status"] != "passed":
            continue
        items = [
            command for command in commands
            if command.segment_id == row["segment_id"]
        ]
        if row.get("coverage") != 1.0 or isinstance(
            row.get("coverage"), bool
        ):
            raise PlanRejected(
                "partial_preflight", "获准段预检覆盖须明确为 1.0"
            )
        detection = _integer(
            row.get("detection_id"), "preflight.detection_id", minimum=1
        )
        if any(command.detection_id != detection for command in items):
            raise PlanRejected(
                "preflight_detection_mismatch", "段命令与预检检测编号不一致"
            )
        if "command_count" in row and _integer(
            row["command_count"], "preflight.command_count"
        ) != len(items):
            raise PlanRejected(
                "partial_segment", "段命令数量与预检绑定不一致"
            )
        segment_digest = hashlib.sha256(
            _canonical_bytes([command.normalized() for command in items])
        ).hexdigest()
        if (
            "command_sha256" in row
            and row["command_sha256"] != segment_digest
        ):
            raise PlanRejected(
                "preflight_binding_mismatch", "获准段命令哈希与预检不一致"
            )
    return passed, blocked, row_summary


def _segment_rows(commands, support_segments, admitted_segments):
    grouped = {}
    for command in commands:
        grouped.setdefault(command.segment_id, []).append(command)
    segments = []
    for segment_id in sorted(grouped):
        items = grouped[segment_id]
        indices = tuple(item.index for item in items)
        kind = "support" if segment_id in support_segments else "machining"
        blocks = []
        block = [indices[0]]
        for index in indices[1:]:
            if index == block[-1] + 1:
                block.append(index)
            else:
                blocks.append(tuple(block))
                block = [index]
        blocks.append(tuple(block))
        if kind == "machining" and len(blocks) != 1:
            raise PlanRejected(
                "fragmented_segment", f"加工段 {segment_id} 命令索引不连续"
            )
        if kind == "support":
            for item in items:
                if item.phase not in SUPPORT_PHASES:
                    raise PlanRejected(
                        "support_phase",
                        f"支撑段 {segment_id} 含加工阶段：{item.phase}",
                    )
                if item.point is not None or item.detection_id != 0:
                    raise PlanRejected(
                        "support_machining",
                        "支撑段不得绑定加工点或检测编号",
                    )
        else:
            if len(items) < 2:
                raise PlanRejected(
                    "partial_segment", f"加工段 {segment_id} 命令不足"
                )
            if any(item.phase not in MACHINING_PHASES for item in items):
                raise PlanRejected(
                    "machining_phase",
                    f"加工段 {segment_id} 含未知加工阶段",
                )
            phases = [item.phase for item in items]
            next_index = indices[-1] + 1
            support_retract = (
                next_index < len(commands)
                and commands[next_index].segment_id in support_segments
                and commands[next_index].phase == "retract"
            )
            if (
                not {"approach", "trace"} <= set(phases)
                or phases[0] != "approach"
                or not (phases[-1] == "retract" or support_retract)
                or phases != sorted(
                    phases, key={"approach": 0, "trace": 1, "retract": 2}.get
                )
            ):
                raise PlanRejected(
                    "incomplete_machining_segment",
                    f"加工段 {segment_id} 缺少有序进给、加工或相邻完整退刀",
                )
            for item in items:
                if item.point is None or item.detection_id <= 0:
                    raise PlanRejected(
                        "missing_machining_binding",
                        f"加工段 {segment_id} 命令缺少工件点或检测编号",
                    )
                if not item.attached:
                    raise PlanRejected(
                        "missing_workpiece_attachment",
                        f"加工段 {segment_id} 命令未绑定携件状态",
                    )
        digest = hashlib.sha256(
            _canonical_bytes([item.normalized() for item in items])
        ).hexdigest()
        segments.append(
            PlanSegment(
                segment_id=segment_id,
                kind=kind,
                admitted=(
                    kind == "machining" and segment_id in admitted_segments
                ),
                command_indices=indices,
                command_blocks=tuple(blocks),
                command_count=len(items),
                t_start=items[0].t,
                t_end=items[-1].t,
                command_sha256=digest,
                phases=tuple(item.phase for item in items),
            )
        )
    return tuple(segments)


def _validate_preview(path, identity, tool_id):
    """Bind the candidate preview by hash without accepting it as a program."""
    path = Path(path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise PlanRejected(
            "missing_source", f"候选预览 CSV 不存在：{path}"
        ) from error
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = csv.DictReader(
            io.StringIO(content.decode("utf-8-sig"), newline="")
        )
        headers = reader.fieldnames
        if (
            headers is None
            or len(headers) != len(set(headers))
            or not {"frame", "x_mm", "y_mm", "z_mm"} <= set(headers)
        ):
            raise PlanRejected(
                "invalid_preview",
                "候选 CSV 须含唯一 frame/x_mm/y_mm/z_mm 字段",
            )
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise PlanRejected(
            "invalid_preview", "候选 CSV 编码或格式无效"
        ) from error
    frames = set()
    segments = set()
    for index, row in enumerate(rows):
        frame = _text(row.get("frame"), f"preview[{index}].frame")
        frames.add(frame)
        for axis_field in ("x_mm", "y_mm", "z_mm"):
            try:
                coordinate = float(row.get(axis_field))
            except (TypeError, ValueError) as error:
                raise PlanRejected(
                    "invalid_preview",
                    f"候选 CSV {axis_field} 不是数值",
                ) from error
            _finite(coordinate, f"preview[{index}].{axis_field}")
        if "segment" in headers and row.get("segment") not in (None, ""):
            try:
                segments.add(int(row["segment"]))
            except (TypeError, ValueError) as error:
                raise PlanRejected(
                    "invalid_preview", "候选 CSV segment 不是整数"
                ) from error
        if "tool" in headers and row.get("tool") and row["tool"] != tool_id:
            raise PlanRejected(
                "preview_tool_mismatch",
                f"候选 CSV 工具 {row['tool']} 与计划 {tool_id} 不一致",
            )
    if frames and frames != {identity.frame}:
        raise PlanRejected(
            "preview_frame_mismatch",
            f"候选 CSV 坐标系 {sorted(frames)} 与计划 {identity.frame} 不一致",
        )
    return (
        {
            "path": str(path.resolve()),
            "sha256": digest,
            "bytes": len(content),
            "rows": len(rows),
            "frames": sorted(frames),
            "segments": sorted(segments),
            "executable": False,
            "role": "candidate_path_preview",
            "scope": "候选预览可含未获准路径，绝不作为控制器程序",
        },
        digest,
        len(content),
    )


def _artifact(role, path, digest, size, executable, source):
    return Artifact(
        role=role,
        path=str(Path(path).resolve()),
        sha256=digest,
        executable=executable,
        source=source,
        bytes=size,
    )


@dataclass
class ControllerPlan:
    """A hashed, validated offline plan. It is not a signed controller program."""

    identity: PlanIdentity
    commands: tuple[Command, ...]
    segments: tuple[PlanSegment, ...]
    admitted_segments: tuple[int, ...]
    blocked_segments: tuple[int, ...]
    support_segments: tuple[int, ...]
    artifacts: tuple[Artifact, ...]
    limits: JointLimits
    manifest: dict[str, Any]
    plan_sha256: str
    issued_at_s: float
    expires_at_s: float
    identity_sources: dict[str, str]
    preview: dict[str, Any] | None
    result_evidence: dict[str, Any] | None
    source_segment_count: int
    preflight_rows: tuple[dict[str, Any], ...]
    manifest_path: Path | None = field(default=None, compare=False)
    plan_directory: Path | None = field(default=None, compare=False)

    @property
    def duration_s(self):
        return self.commands[-1].t if self.commands else 0.0

    @property
    def executable(self):
        return bool(self.commands and self.admitted_segments)

    def manifest_bytes(self):
        return _canonical_bytes(self.manifest)

    def write_manifest(self, path):
        path = Path(path).resolve()
        content = self.manifest_bytes()
        if path.exists():
            existing = path.read_bytes()
            if existing != content:
                raise PlanRejected(
                    "plan_manifest_changed",
                    "输出目录中的计划清单与本次加载内容不一致",
                )
        else:
            _atomic_write(path, content)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.plan_sha256:
            raise PlanRejected("plan_hash_mismatch", "写入的计划清单哈希不一致")
        self.manifest_path = path
        return digest

    def revalidate(
        self, now_s=None, *, check_expiry=True, verify_manifest=True
    ):
        """Rehash every source and, by default, reject an expired plan."""
        now_s = time.time() if now_s is None else _finite(now_s, "now_s")
        bound = {
            "identity": self.identity.to_dict(),
            "issued_at_s": self.issued_at_s,
            "expires_at_s": self.expires_at_s,
            "units": dict(self.identity.units),
            "frame": self.identity.frame,
            "command_count": len(self.commands),
            "duration_s": self.duration_s,
            "commands_sha256": hashlib.sha256(
                _canonical_bytes(
                    [command.normalized() for command in self.commands]
                )
            ).hexdigest(),
            "segments": [segment.to_dict() for segment in self.segments],
            "admitted_segments": list(self.admitted_segments),
            "blocked_segments": list(self.blocked_segments),
            "support_segments": list(self.support_segments),
            "limits": self.limits.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "identity_sources": dict(self.identity_sources),
            "source_segment_count": self.source_segment_count,
            "preflight_rows": list(self.preflight_rows),
            "preview": self.preview,
            "result_evidence": self.result_evidence,
        }
        if (
            hashlib.sha256(self.manifest_bytes()).hexdigest()
            != self.plan_sha256
            or any(self.manifest.get(key) != value for key, value in bound.items())
        ):
            raise PlanRejected(
                "plan_hash_mismatch", "内存中的计划与已绑定清单不一致"
            )
        checked = []
        if verify_manifest and self.manifest_path is not None:
            try:
                content = self.manifest_path.read_bytes()
            except OSError as error:
                raise PlanRejected(
                    "missing_source", "计划清单文件缺失"
                ) from error
            actual = hashlib.sha256(content).hexdigest()
            if actual != self.plan_sha256:
                raise PlanRejected(
                    "source_hash_mismatch",
                    f"计划清单哈希不一致：{self.manifest_path}",
                )
            checked.append(
                {
                    "role": "plan_manifest",
                    "path": str(self.manifest_path),
                    "sha256": actual,
                }
            )
        for artifact in self.artifacts:
            path = Path(artifact.path)
            try:
                digest = sha256_file(path)
            except OSError as error:
                raise PlanRejected(
                    "missing_source", f"{artifact.role} 缺失：{path}"
                ) from error
            if digest != artifact.sha256:
                raise PlanRejected(
                    "source_hash_mismatch",
                    f"{artifact.role} 哈希不一致：{path}",
                )
            checked.append(
                {
                    "role": artifact.role,
                    "path": artifact.path,
                    "sha256": digest,
                }
            )
        if check_expiry:
            if now_s < self.issued_at_s:
                raise PlanRejected(
                    "plan_not_yet_valid", "控制时钟早于计划签发时间"
                )
            if now_s >= self.expires_at_s:
                raise PlanRejected(
                    "plan_expired",
                    f"计划已于 {self.expires_at_s:.3f} 过期，不能确认或执行",
                )
        return {
            "plan_sha256": self.plan_sha256,
            "checked_artifacts": len(checked),
            "artifacts": checked,
            "now_s": now_s,
            "issued_at_s": self.issued_at_s,
            "expires_at_s": self.expires_at_s,
            "seconds_remaining": max(0.0, self.expires_at_s - now_s),
        }

    def command(self, index):
        return self.commands[index]

    def segment(self, segment_id):
        for segment in self.segments:
            if segment.segment_id == segment_id:
                return segment
        raise KeyError(segment_id)

    def summary(self):
        return {
            "schema": PLAN_SCHEMA,
            "plan_sha256": self.plan_sha256,
            "identity": self.identity.to_dict(),
            "identity_sources": dict(self.identity_sources),
            "issued_at_s": self.issued_at_s,
            "expires_at_s": self.expires_at_s,
            "expired_at_load": time.time() >= self.expires_at_s,
            "command_count": len(self.commands),
            "duration_s": self.duration_s,
            "source_segment_count": self.source_segment_count,
            "admitted_segments": list(self.admitted_segments),
            "blocked_segments": list(self.blocked_segments),
            "support_segments": list(self.support_segments),
            "segments": [segment.to_dict() for segment in self.segments],
            "preflight_rows": list(self.preflight_rows),
            "preflight_binding": self.manifest["preflight_binding"],
            "limits": self.limits.to_dict(),
            "preview": self.preview,
            "result_evidence": self.result_evidence,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "executable": self.executable,
            "real_command_mapping": False,
            "controller_certificate": False,
            "scope": (
                "offline structural and limits validation of accepted "
                "motion_commands; no MuJoCo collision or tracking rerun"
            ),
        }


def _build_plan(
    *,
    identity,
    commands,
    segments,
    admitted,
    blocked,
    support,
    artifacts,
    limits,
    issued_at_s,
    expires_at_s,
    identity_sources,
    preview,
    result_evidence,
    source_segment_count,
    preflight_rows,
    preflight_binding,
    plan_directory,
):
    manifest = {
        "schema": PLAN_SCHEMA,
        "identity": identity.to_dict(),
        "identity_sources": dict(identity_sources),
        "issued_at_s": issued_at_s,
        "expires_at_s": expires_at_s,
        "units": dict(identity.units),
        "frame": identity.frame,
        "command_count": len(commands),
        "duration_s": commands[-1].t if commands else 0.0,
        "commands_sha256": hashlib.sha256(
            _canonical_bytes([item.normalized() for item in commands])
        ).hexdigest(),
        "segments": [segment.to_dict() for segment in segments],
        "admitted_segments": list(admitted),
        "blocked_segments": list(blocked),
        "support_segments": list(support),
        "source_segment_count": source_segment_count,
        "preflight_rows": list(preflight_rows),
        "preflight_binding": preflight_binding,
        "limits": limits.to_dict(),
        "preview": preview,
        "result_evidence": result_evidence,
        "artifacts": [artifact.to_dict() for artifact in artifacts],
        "execution_source": {
            "role": "accepted_motion_commands",
            "real_command_mapping": False,
            "candidate_preview_executable": False,
            "controller_certificate": False,
        },
        "scope": (
            "offline controller mock input; accepted commands are the only "
            "execution source and no physical mapping is assumed"
        ),
    }
    digest = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    return ControllerPlan(
        identity=identity,
        commands=commands,
        segments=segments,
        admitted_segments=admitted,
        blocked_segments=blocked,
        support_segments=support,
        artifacts=artifacts,
        limits=limits,
        manifest=manifest,
        plan_sha256=digest,
        issued_at_s=issued_at_s,
        expires_at_s=expires_at_s,
        identity_sources=identity_sources,
        preview=preview,
        result_evidence=result_evidence,
        source_segment_count=source_segment_count,
        preflight_rows=preflight_rows,
        plan_directory=Path(plan_directory).resolve(),
    )


def load_plan(
    directory,
    *,
    identity=None,
    issued_at_s=None,
    expires_at_s=None,
    valid_for_s=86_400.0,
    now_s=None,
    xml_path=DEFAULT_ROBOT_XML,
    support_segment_ids=(0,),
    expected_plan_sha256=None,
    require_preview=False,
    commands_path=None,
    preflight_path=None,
    preview_path=None,
    result_path=None,
):
    """Load one accepted motion command bundle and bind its full identity.

    Args:
        directory: Run or rehearsal directory.
        identity: ``PlanIdentity`` or a complete identity object. When omitted,
            it is derived from ``result.json`` and the run directory name.
        issued_at_s: Plan issue epoch seconds. Defaults to the command file
            modification time. The original run has no signed issuance time.
        expires_at_s: Expiry epoch seconds. Defaults to issue plus
            ``valid_for_s``.
        valid_for_s: Default validity window in seconds.
        now_s: Reserved clock value for deterministic diagnostics.
        xml_path: Imported scene XML holding the six-axis limits.
        support_segment_ids: Non-machining command segments such as segment 0.
        expected_plan_sha256: Caller's pinned hash; mismatch rejects the plan.
        require_preview: Require and hash the candidate preview CSV.
        commands_path: Override for ``motion_commands.json``.
        preflight_path: Override for ``preflight.json``.
        preview_path: Override for ``estun_path_preview.csv``.
        result_path: Override for ``result.json``.

    Returns:
        A validated ``ControllerPlan``. An expired plan is loaded for review
        but cannot be confirmed or executed.
    """
    directory = Path(directory).resolve()
    commands_file = Path(
        commands_path or directory / "motion_commands.json"
    ).resolve()
    preflight_file = Path(
        preflight_path or directory / "preflight.json"
    ).resolve()
    preview_file = Path(
        preview_path or directory / "estun_path_preview.csv"
    ).resolve()
    result_file = Path(result_path or directory / "result.json").resolve()
    support = tuple(
        _integer(value, "support_segment_id", minimum=0)
        for value in support_segment_ids
    )
    if len(set(support)) != len(support):
        raise PlanRejected("duplicate_support_segment", "支撑段编号重复")
    result = None
    result_content = None
    source_manifest = None
    source_manifest_file = directory / "source_hashes.json"
    if source_manifest_file.is_file():
        declared, source_content, source_hash = _json_value(
            source_manifest_file, "source_hashes.json"
        )
        if not isinstance(declared, dict):
            raise PlanRejected(
                "invalid_source_manifest", "历史源码哈希清单须为对象"
            )
        source_manifest = {
            "path": str(source_manifest_file),
            "sha256": source_hash,
            "declared_hashes": declared,
            "scope": "archived producer provenance; no current producer code executed",
        }
    if result_file.is_file():
        result_value, result_content, result_hash = _json_value(
            result_file, "result.json"
        )
        if not isinstance(result_value, dict):
            raise PlanRejected("invalid_result", "result.json 须为对象")
        result = result_value
    if identity is None:
        if result is None:
            raise PlanRejected(
                "missing_identity", "无 result.json 时须显式提供工件身份"
            )
        identity_value, identity_sources = _identity_from_result(
            result, directory
        )
    elif isinstance(identity, PlanIdentity):
        identity_value = identity
        identity_sources = {
            "identity": "explicit caller object",
            "units": "explicit caller units",
        }
    else:
        identity_value = PlanIdentity.from_dict(identity)
        identity_sources = {
            "identity": "explicit caller object",
            "units": "explicit caller units",
        }
    limits = load_joint_limits(xml_path)
    command_values, command_content, command_hash = _json_value(
        commands_file, "motion_commands.json"
    )
    commands = _parse_commands(command_values, limits)
    preflight_value, preflight_content, preflight_hash = _json_value(
        preflight_file, "preflight.json"
    )
    passed, blocked, preflight_rows = _parse_preflight(
        preflight_value, commands, set(support), command_hash
    )
    admitted = tuple(segment_id for segment_id in sorted(passed))
    segments = _segment_rows(commands, set(support), set(admitted))
    seen_segments = {segment.segment_id for segment in segments}
    missing_support = set(support) - seen_segments
    if missing_support:
        raise PlanRejected(
            "support_segment_missing",
            f"计划缺少声明的支撑段：{sorted(missing_support)}",
        )
    if preview_file.is_file():
        preview, preview_hash, preview_size = _validate_preview(
            preview_file, identity_value, identity_value.tool_id
        )
        preview["unapproved_candidate_segments"] = sorted(
            set(preview["segments"]) - set(admitted)
        )
        preview["admitted_segments_in_commands"] = list(admitted)
    else:
        if require_preview:
            raise PlanRejected(
                "missing_source", f"候选预览 CSV 不存在：{preview_file}"
            )
        preview = None
        preview_hash = None
        preview_size = 0
    if issued_at_s is None:
        try:
            issued_at_s = commands_file.stat().st_mtime
        except OSError as error:
            raise PlanRejected(
                "missing_source", "无法读取命令文件时间"
            ) from error
    issued_at_s = _finite(issued_at_s, "issued_at_s", minimum=0)
    if expires_at_s is None:
        valid_for_s = _finite(valid_for_s, "valid_for_s", minimum=1e-6)
        expires_at_s = issued_at_s + valid_for_s
    expires_at_s = _finite(expires_at_s, "expires_at_s", minimum=0)
    if expires_at_s <= issued_at_s:
        raise PlanRejected("invalid_plan_window", "计划有效期必须晚于签发时间")
    artifacts = [
        _artifact(
            "accepted_motion_commands",
            commands_file,
            command_hash,
            len(command_content),
            True,
            "workstation_run.export_commands accepted commands",
        ),
        _artifact(
            "motion_preflight",
            preflight_file,
            preflight_hash,
            len(preflight_content),
            False,
            "workstation_motion.preflight for the same command list",
        ),
    ]
    if preview is not None:
        artifacts.append(
            _artifact(
                "candidate_path_preview",
                preview_file,
                preview_hash,
                preview_size,
                False,
                "workstation_run.estun_path_preview.csv candidate only",
            )
        )
    if result is not None:
        artifacts.append(
            _artifact(
                "historical_run_result",
                result_file,
                result_hash,
                len(result_content),
                False,
                "workstation_run result; historical evidence only",
            )
        )
    if source_manifest is not None:
        artifacts.append(
            _artifact(
                "historical_source_manifest",
                source_manifest_file,
                source_hash,
                len(source_content),
                False,
                "historical producer hash declarations; provenance only",
            )
        )
    artifacts.append(
        _artifact(
            "robot_joint_limits",
            Path(limits.source_path),
            limits.source_sha256,
            Path(limits.source_path).stat().st_size,
            False,
            "imported ESTUN scene XML; parsed without MuJoCo",
        )
    )
    result_evidence = None
    if result is not None:
        result_evidence = {
            "path": str(result_file),
            "sha256": result_hash,
            "task_status": result.get("task_status"),
            "goal_complete": result.get("goal_complete"),
            "observational_release": result.get("observational_release"),
            "motion_gate_passed": result.get("motion_gate_passed"),
            "path_count": result.get("path_count"),
            "admitted_path_count": result.get("admitted_path_count"),
            "historical_source_manifest": source_manifest,
            "scope": "historical simulation result; not re-executed",
        }
    plan = _build_plan(
        identity=identity_value,
        commands=commands,
        segments=segments,
        admitted=admitted,
        blocked=tuple(sorted(blocked)),
        support=tuple(sorted(set(support) & seen_segments)),
        artifacts=tuple(artifacts),
        limits=limits,
        issued_at_s=issued_at_s,
        expires_at_s=expires_at_s,
        identity_sources=identity_sources,
        preview=preview,
        result_evidence=result_evidence,
        source_segment_count=len(preflight_rows),
        preflight_rows=tuple(preflight_rows),
        preflight_binding={
            "file_hash_verified": "command_file_sha256" in preflight_value,
            "normalized_hash_verified": "commands_sha256" in preflight_value,
            "legacy_unbound_preflight": (
                "command_file_sha256" not in preflight_value
                and "commands_sha256" not in preflight_value
            ),
            "scope": (
                "archived row/count admission for mock only; no current "
                "collision, tracking or quality revalidation"
                if "command_file_sha256" not in preflight_value
                and "commands_sha256" not in preflight_value
                else "exact command hash binding; preflight itself not rerun"
            ),
        },
        plan_directory=directory,
    )
    if expected_plan_sha256 is not None and (
        not isinstance(expected_plan_sha256, str)
        or len(expected_plan_sha256) != 64
        or plan.plan_sha256 != expected_plan_sha256
    ):
        raise PlanRejected("plan_hash_mismatch", "计划哈希与调用方钉扎值不一致")
    return plan


def load_plan_from_run(directory, **kwargs):
    """Historical-run convenience wrapper that requires the preview CSV."""
    kwargs.setdefault("require_preview", True)
    return load_plan(directory, **kwargs)


def write_case_files(
    directory,
    *,
    commands,
    preflight,
    preview_rows=None,
    result=None,
):
    """Write a synthetic input bundle for interface rehearsal or tests."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    content = _pretty_bytes(commands)
    _atomic_write(directory / "motion_commands.json", content)
    bound_preflight = {
        **preflight,
        "command_file_sha256": hashlib.sha256(content).hexdigest(),
    }
    _atomic_write(directory / "preflight.json", _pretty_bytes(bound_preflight))
    if preview_rows is not None:
        output = io.StringIO(newline="")
        fields = [
            "segment",
            "detection_id",
            "view",
            "frame",
            "x_mm",
            "y_mm",
            "z_mm",
            "nx",
            "ny",
            "nz",
            "tool",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in preview_rows:
            writer.writerow(row)
        _atomic_write(
            directory / "estun_path_preview.csv",
            output.getvalue().encode("utf-8"),
        )
    if result is not None:
        _atomic_write(directory / "result.json", _pretty_bytes(result))
    return directory


def _quality_status(value):
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        raise StateRejected("invalid_quality", "质量状态须为文本")
    if value in {"pass", "passed", "verified", "complete", "accepted"}:
        return "pass"
    if value in {"fail", "failed", "rejected", "needs_rework"}:
        return "fail"
    return "unknown"


def normalize_quality_evidence(evidence, identity):
    """Validate a quality report independent of the execution callback."""
    if not isinstance(evidence, dict):
        raise StateRejected("invalid_quality", "质量证据须为对象")
    try:
        evidence = json.loads(_canonical_bytes(evidence))
    except (TypeError, ValueError, OverflowError) as error:
        raise StateRejected(
            "invalid_quality", "质量证据须为有限、可序列化的 JSON"
        ) from error
    if evidence.get("schema") not in {
        QUALITY_SCHEMA,
        "daiyi-simulation-quality-v1",
    }:
        raise StateRejected("invalid_quality", "质量证据 schema 不匹配")
    for field_name in (
        "part_id",
        "serial",
        "cad_revision",
        "fixture_id",
        "fixture_revision",
        "tool_id",
        "frame",
    ):
        if evidence.get(field_name) != getattr(identity, field_name):
            raise StateRejected(
                "quality_identity_mismatch", f"质量证据 {field_name} 不一致"
            )
    if evidence.get("units") != CANONICAL_UNITS:
        raise StateRejected("quality_unit_mismatch", "质量证据单位不一致")
    status = evidence.get("task_status")
    if status not in {"complete", "partial", "failed", "unknown"}:
        raise StateRejected("invalid_quality", "质量证据任务状态无效")
    covered_value = evidence.get("covered_segments", [])
    if not isinstance(covered_value, list):
        raise StateRejected("invalid_quality", "covered_segments 须为数组")
    covered = tuple(
        sorted(
            {
                _integer(value, "covered_segment", minimum=1)
                for value in covered_value
            }
        )
    )
    if len(covered) != len(covered_value):
        raise StateRejected("invalid_quality", "质量覆盖段编号不得重复")
    results = evidence.get("segment_results", {})
    if not isinstance(results, dict):
        raise StateRejected("invalid_quality", "segment_results 须为对象")
    normalized_results = {}
    for key, value in results.items():
        try:
            segment = int(key)
        except (TypeError, ValueError) as error:
            raise StateRejected(
                "invalid_quality", "segment_results 键须为段编号"
            ) from error
        if segment < 1 or str(segment) != key:
            raise StateRejected(
                "invalid_quality", "segment_results 须为规范正整数键"
            )
        normalized_results[segment] = _quality_status(value)
    regions = evidence.get("regions")
    region_failures = []
    if regions is not None:
        if not isinstance(regions, list):
            raise StateRejected("invalid_quality", "regions 须为数组")
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                raise StateRejected(
                    "invalid_quality", f"质量区域 {index} 须为对象"
                )
            status_value = _quality_status(region.get("status"))
            risk = False
            for key in ("overgrind", "negative_limit_risk"):
                if key in region:
                    if not isinstance(region[key], bool):
                        raise StateRejected(
                            "invalid_quality", f"区域 {key} 须为布尔值"
                        )
                    risk = risk or region[key]
            observations = region.get("observations")
            invalid_views = False
            if observations is not None:
                if not isinstance(observations, list):
                    raise StateRejected(
                        "invalid_quality", "区域 observations 须为数组"
                    )
                invalid_views = any(
                    isinstance(view, bool)
                    or not isinstance(view, int)
                    or view not in range(6)
                    for view in observations
                )
            if status_value != "pass" or risk or invalid_views:
                region_failures.append(
                    {
                        "index": index,
                        "id": region.get("id"),
                        "status": region.get("status"),
                        "status_reason": region.get("status_reason"),
                        "overgrind": region.get("overgrind", False),
                        "negative_limit_risk": region.get(
                            "negative_limit_risk", False
                        ),
                        "invalid_view": invalid_views,
                    }
                )
    for key in ("overgrind", "negative_limit_risk"):
        if key in evidence and not (
            isinstance(evidence[key], bool)
            or isinstance(evidence[key], int) and evidence[key] >= 0
        ):
            raise StateRejected(
                "invalid_quality", f"{key} 须为非负计数或布尔值"
            )
    source_kind = evidence.get("source_kind", "unknown")
    return {
        **evidence,
        "schema": QUALITY_SCHEMA,
        "covered_segments": list(covered),
        "segment_results": {
            str(key): value for key, value in sorted(normalized_results.items())
        },
        "region_failures": region_failures,
        "source_kind": source_kind,
        "independent": (
            evidence.get("independent") is True
            and source_kind != "historical_simulation_result"
        ),
        "real_evidence": evidence.get("real_evidence") is True,
    }


class MockController:
    """Deterministic offline state machine. No robot or network is touched."""

    def __init__(
        self,
        plan,
        workdir,
        *,
        clock: Callable[[], float] = time.time,
        request_id=None,
        execution_id=None,
        controller_id=MOCK_CONTROLLER_ID,
        checkpoint_every=64,
        feedback_timeout_s=MAX_FEEDBACK_GAP_SECONDS,
    ):
        if not isinstance(controller_id, str) or not controller_id.startswith(
            "mock:"
        ):
            raise StateRejected(
                "non_mock_controller", "控制器身份必须显式以 mock: 开头"
            )
        self._setup(
            plan,
            workdir,
            clock=clock,
            request_id=request_id,
            execution_id=execution_id,
            controller_id=controller_id,
            checkpoint_every=checkpoint_every,
            feedback_timeout_s=feedback_timeout_s,
        )
        self.workdir.mkdir(parents=True, exist_ok=True)
        if any(self.workdir.iterdir()):
            raise StateRejected(
                "workdir_not_fresh", "控制器工作目录已有事件日志"
            )
        self.plan.write_manifest(self.workdir / "plan_manifest.json")
        self._write_program()
        self.feedback_path.touch(exist_ok=False)
        self._record("plan_loaded", {"plan_sha256": plan.plan_sha256})
        self._write_checkpoint()

    def _setup(
        self,
        plan,
        workdir,
        *,
        clock,
        request_id,
        execution_id,
        controller_id,
        checkpoint_every,
        feedback_timeout_s,
    ):
        # Each controller binds its own manifest path, including during resume.
        self.plan = replace(plan)
        self.workdir = Path(workdir).resolve()
        self.events_path = self.workdir / "events.jsonl"
        self.feedback_path = self.workdir / "feedback.jsonl"
        self.checkpoint_path = self.workdir / "checkpoint.json"
        self.quality_path = self.workdir / "quality_evidence.json"
        self.program_path = self.workdir / "mock_program.json"
        self.clock = clock
        suffix = plan.plan_sha256[:16]
        self.request_id = _text(
            f"mock-request-{suffix}" if request_id is None else request_id,
            "request_id",
        )
        self.execution_id = _text(
            f"mock-execution-{suffix}-01" if execution_id is None else execution_id,
            "execution_id",
        )
        self.controller_id = controller_id
        self.checkpoint_every = max(1, int(checkpoint_every))
        self.feedback_timeout_s = _finite(
            feedback_timeout_s, "feedback_timeout_s", minimum=1e-6
        )
        self.last_control_clock_s = _finite(
            self.clock(), "controller.clock_s", minimum=0
        )
        self.last_feedback_clock_s = None
        self.state = "PLANNED"
        self.operator = None
        self.event_count = 0
        self.feedback_batches = 0
        self.feedback_samples = 0
        self.covered_commands = set()
        self.segment_coverage = {}
        self.last_sequence = 0
        self.last_t_s = -math.inf
        self.last_command_index = -1
        self.seen_sequence = {}
        self.completion_claimed = False
        self.early_completion_claim = False
        self.total_contacts = 0
        self.fault = None
        self.fault_code = None
        self.cancel_reason = None
        self.reject_reason = None
        self.recovery_count = 0
        self.execution_attempts = 0
        self.started_at_s = None
        self.quality = None
        self.quality_sha256 = None
        self.program_sha256 = None
        self.sequence_gaps = 0

    @property
    def next_command_index(self):
        for index in range(len(self.plan.commands)):
            if index not in self.covered_commands:
                return index
        return len(self.plan.commands)

    @property
    def next_sequence(self):
        return self.last_sequence + 1

    def _program_value(self):
        commands = next(
            artifact
            for artifact in self.plan.artifacts
            if artifact.role == "accepted_motion_commands"
        )
        return {
            "schema": PROGRAM_SCHEMA,
            "kind": "explicit_offline_mock",
            "controller_id": self.controller_id,
            "plan_sha256": self.plan.plan_sha256,
            "command_artifact": commands.to_dict(),
            "real_command_mapping": False,
            "network": False,
            "physical_io": False,
            "execution": "state machine only; no joint command is transmitted",
        }
    def _write_program(self):
        content = _canonical_bytes(self._program_value())
        _atomic_write(self.program_path, content)
        self.program_sha256 = hashlib.sha256(content).hexdigest()

    def _record(self, event_type, details=None):
        self.event_count += 1
        record = {
            "event": self.event_count,
            "type": event_type,
            "state": self.state,
            "clock_s": self.last_control_clock_s,
            "plan_sha256": self.plan.plan_sha256,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "controller_id": self.controller_id,
            "details": details or {},
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def _write_checkpoint(self):
        checkpoint = {
            "schema": CHECKPOINT_SCHEMA,
            "plan_sha256": self.plan.plan_sha256,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "controller_id": self.controller_id,
            "operator": self.operator,
            "state": self.state,
            "event_count": self.event_count,
            "feedback_batches": self.feedback_batches,
            "feedback_samples": self.feedback_samples,
            "covered_commands": sorted(self.covered_commands),
            "segment_coverage": {
                str(key): value
                for key, value in sorted(self.segment_coverage.items())
            },
            "last_sequence": self.last_sequence,
            "last_t_s": (None if self.last_t_s == -math.inf else self.last_t_s),
            "last_command_index": self.last_command_index,
            "seen_sequence": {
                str(key): value
                for key, value in sorted(self.seen_sequence.items())
            },
            "completion_claimed": self.completion_claimed,
            "early_completion_claim": self.early_completion_claim,
            "total_contacts": self.total_contacts,
            "sequence_gaps": self.sequence_gaps,
            "fault": self.fault,
            "fault_code": self.fault_code,
            "cancel_reason": self.cancel_reason,
            "reject_reason": self.reject_reason,
            "recovery_count": self.recovery_count,
            "execution_attempts": self.execution_attempts,
            "started_at_s": self.started_at_s,
            "last_control_clock_s": self.last_control_clock_s,
            "last_feedback_clock_s": self.last_feedback_clock_s,
            "feedback_timeout_s": self.feedback_timeout_s,
            "program_sha256": self.program_sha256,
            "feedback_sha256": sha256_file(self.feedback_path),
            "events_sha256": sha256_file(self.events_path),
            "quality_sha256": self.quality_sha256,
            "quality_status": (
                self._quality_report()["status"]
                if self.quality is not None
                else "missing"
            ),
            "resume_policy": "hold",
            "saved_at_s": self.last_control_clock_s,
        }
        _atomic_write(self.checkpoint_path, _pretty_bytes(checkpoint))

    def _ensure_valid(self, now_s=None):
        now_s = self.clock() if now_s is None else now_s
        try:
            now_s = _finite(now_s, "controller.clock_s", minimum=0)
            if now_s < self.last_control_clock_s:
                raise PlanRejected(
                    "controller_clock_regression",
                    "控制时钟回退，必须保持 hold 并核对同次启动时间",
                )
            self.last_control_clock_s = now_s
            checked = self.plan.revalidate(now_s=now_s, check_expiry=True)
            if sha256_file(self.program_path) != self.program_sha256:
                raise PlanRejected(
                    "program_hash_mismatch", "mock 程序哈希与本次计划不一致"
                )
            if self.quality is not None and not self._quality_integrity():
                raise PlanRejected(
                    "quality_hash_mismatch", "质量证据哈希发生变化"
                )
            return checked
        except OSError as error:
            self.hold("missing_source", str(error), cause="plan_validation")
            raise PlanRejected("missing_source", str(error)) from error
        except PlanRejected as error:
            self.hold(error.code, str(error), cause="plan_validation")
            raise

    def _require_state(self, *allowed):
        if self.state not in allowed:
            raise StateRejected(
                "invalid_state",
                f"当前状态 {self.state} 不允许此操作；允许 {sorted(allowed)}",
            )

    def confirm(self, operator):
        self._require_state("PLANNED")
        self.operator = _text(operator, "operator")
        self._ensure_valid()
        self.state = "CONFIRMED"
        self._record(
            "confirmed",
            {"operator": self.operator, "request_id": self.request_id},
        )
        self._write_checkpoint()
        return self.summary()

    def start(self):
        self._require_state("CONFIRMED")
        if not self.plan.executable:
            self.hold(
                "no_admitted_segments",
                "计划没有完整获准的加工段，不能执行",
                cause="plan_validation",
            )
            raise PlanRejected(
                "no_admitted_segments", "计划没有完整获准的加工段"
            )
        self._ensure_valid()
        self.execution_attempts += 1
        self.started_at_s = self.last_control_clock_s
        self.last_feedback_clock_s = self.started_at_s
        self.fault = None
        self.fault_code = None
        self.state = "EXECUTING"
        self._record(
            "started",
            {
                "execution_attempt": self.execution_attempts,
                "operator": self.operator,
            },
        )
        self._write_checkpoint()
        return self.summary()

    def _feedback_reject(self, code, message):
        raise FeedbackRejected(code, message)

    def check_feedback_timeout(self, now_s=None):
        """Poll the mock deadline; no sleep, network or device I/O occurs."""
        self._require_state("EXECUTING")
        self._ensure_valid(now_s)
        reference = self.last_feedback_clock_s
        if (
            not self.completion_claimed
            and reference is not None
            and self.last_control_clock_s - reference > self.feedback_timeout_s
        ):
            message = "mock 反馈超过允许间隔，掉线后不得自动继续"
            self.hold("feedback_timeout", message, cause="feedback")
            raise FeedbackRejected("feedback_timeout", message)
        return {"state": self.state, "deadline_checked": True}

    def _validate_feedback_batch(self, batch):
        if not isinstance(batch, dict):
            self._feedback_reject("invalid_feedback", "反馈批次须为 JSON 对象")
        if batch.get("schema") not in {
            FEEDBACK_SCHEMA,
            "daiyi-mock-feedback-v1",
        }:
            self._feedback_reject(
                "invalid_feedback_schema", "反馈 schema 不匹配"
            )
        if batch.get("source_kind") not in {"synthetic", "mock_offline"}:
            self._feedback_reject(
                "feedback_source_mismatch",
                "离线 mock 不接受真实控制器来源的反馈",
            )
        if batch.get("controller_id") != self.controller_id:
            self._feedback_reject(
                "feedback_controller_mismatch", "反馈控制器身份不一致"
            )
        for key, expected in (
            ("request_id", self.request_id),
            ("execution_id", self.execution_id),
            ("plan_sha256", self.plan.plan_sha256),
            ("part_id", self.plan.identity.part_id),
            ("serial", self.plan.identity.serial),
            ("cad_revision", self.plan.identity.cad_revision),
            ("fixture_revision", self.plan.identity.fixture_revision),
            ("fixture_id", self.plan.identity.fixture_id),
            ("tool_id", self.plan.identity.tool_id),
            ("frame", self.plan.identity.frame),
        ):
            if batch.get(key) != expected:
                self._feedback_reject(
                    "feedback_binding_mismatch", f"反馈 {key} 绑定不一致"
                )
        if batch.get("units") != CANONICAL_UNITS:
            self._feedback_reject(
                "feedback_unit_mismatch", "反馈单位与计划不一致"
            )
        sequence = _integer(
            batch.get("sequence"), "feedback.sequence", minimum=1
        )
        samples = batch.get("samples")
        if not isinstance(samples, list) or not samples:
            self._feedback_reject(
                "missing_feedback_samples", "反馈批次没有样本"
            )
        if not isinstance(batch.get("completed"), bool):
            self._feedback_reject(
                "invalid_feedback", "反馈 completed 须明确为布尔值"
            )
        return sequence, samples

    def ingest_feedback(self, batch):
        """Accept one ordered batch. Exact replay is idempotent."""
        self._require_state("EXECUTING")
        self.check_feedback_timeout()
        try:
            prepared = self._prepare_feedback(batch)
        except (PlanRejected, FeedbackRejected) as error:
            self.hold(error.code, str(error), cause="feedback")
            raise FeedbackRejected(error.code, str(error)) from error
        except (TypeError, ValueError, OverflowError) as error:
            self.hold(
                "invalid_feedback", "反馈不是有效有限 JSON", cause="feedback"
            )
            raise FeedbackRejected(
                "invalid_feedback", "反馈不是有效有限 JSON"
            ) from error
        if prepared["idempotent"]:
            self._record(
                "feedback_duplicate",
                {"sequence": prepared["sequence"], "idempotent": True},
            )
            self._write_checkpoint()
            return {
                "accepted": False,
                "idempotent": True,
                "sequence": prepared["sequence"],
                "state": self.state,
            }
        # Commit coverage only after the entire batch has passed validation
        # and its journal record has been flushed.
        with self.feedback_path.open("a", encoding="utf-8") as stream:
            stream.write(prepared["content"].decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        self._apply_feedback(prepared)
        self.last_feedback_clock_s = self.last_control_clock_s
        batch_fault = prepared["fault"]
        self._record(
            "feedback_accepted" if batch_fault is None else "feedback_fault",
            {
                "sequence": prepared["sequence"],
                "samples": prepared["sample_count"],
                "covered_commands": len(self.covered_commands),
                "unexpected_contacts": prepared["contacts"],
                "fault": batch_fault,
                "completion_claimed": prepared["completed"],
            },
        )
        self._write_checkpoint()
        if batch_fault is not None:
            self.hold(batch_fault, "反馈触发默认 hold", cause="feedback")
        return {
            "accepted": True,
            "idempotent": False,
            "sequence": prepared["sequence"],
            "state": self.state,
            "fault": batch_fault,
        }

    def _prepare_feedback(self, batch):
        """Validate a complete payload without mutating progress or logs."""
        content = _canonical_bytes(batch)
        batch = json.loads(content)
        sequence, samples = self._validate_feedback_batch(batch)
        payload_hash = hashlib.sha256(content).hexdigest()
        if sequence in self.seen_sequence:
            if self.seen_sequence[sequence] == payload_hash:
                return {
                    "idempotent": True,
                    "sequence": sequence,
                }
            self._feedback_reject(
                "feedback_duplicate_conflict",
                f"反馈序号 {sequence} 重复但内容不同",
            )
        if sequence <= self.last_sequence:
            self._feedback_reject(
                "feedback_out_of_order",
                f"反馈序号 {sequence} 小于已接受 {self.last_sequence}",
            )
        if sequence > self.last_sequence + 1:
            self._feedback_reject(
                "feedback_sequence_gap", "反馈批次序号缺失，不能跳过"
            )
        new_indices = []
        projected_covered = set(self.covered_commands)
        local_last_t = self.last_t_s
        local_last_index = self.last_command_index
        batch_contacts = 0
        batch_fault = None
        for sample_index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                self._feedback_reject(
                    "invalid_feedback_sample", "反馈样本须为对象"
                )
            stamp = _finite(
                sample.get("t_s"),
                f"feedback.samples[{sample_index}].t_s",
                minimum=0,
            )
            if stamp <= local_last_t:
                self._feedback_reject(
                    "feedback_time_regression",
                    f"反馈时间 {stamp} 未严格递增",
                )
            if stamp > self.plan.duration_s + MAX_FEEDBACK_LAG_SECONDS:
                self._feedback_reject(
                    "feedback_time_beyond_plan",
                    "反馈时间超出计划时长允许的尾部容差",
                )
            command_index = _integer(
                sample.get("command_index"),
                f"feedback.samples[{sample_index}].command_index",
            )
            if command_index >= len(self.plan.commands):
                self._feedback_reject(
                    "feedback_command_out_of_range",
                    "反馈命令索引超出计划",
                )
            if command_index < local_last_index:
                self._feedback_reject(
                    "feedback_out_of_order",
                    "反馈命令索引倒退",
                )
            if command_index == local_last_index:
                self._feedback_reject(
                    "feedback_command_duplicate",
                    "同一命令不得通过新批次重复计入覆盖",
                )
            if command_index != local_last_index + 1:
                self._feedback_reject(
                    "feedback_command_gap", "反馈缺少前序命令，不能跳过"
                )
            command = self.plan.commands[command_index]
            if not (
                command.t - 1e-9
                <= stamp
                <= command.t + MAX_FEEDBACK_LAG_SECONDS
            ):
                self._feedback_reject(
                    "feedback_command_time_mismatch",
                    "反馈时间未对应同一命令的计划时间与允许延迟",
                )
            if sample.get("segment_id") != command.segment_id:
                self._feedback_reject(
                    "feedback_segment_mismatch",
                    "反馈段编号与命令不一致",
                )
            status = sample.get("status")
            if status not in FEEDBACK_STATUSES:
                self._feedback_reject("invalid_feedback_status", "反馈状态无效")
            if (
                status == "segment_complete"
                and command_index
                != self.plan.segment(command.segment_id).command_indices[-1]
            ):
                self._feedback_reject(
                    "feedback_segment_completion_mismatch",
                    "段完成状态出现在该段末命令之前",
                )
            if status == "completed" and (
                command_index != len(self.plan.commands) - 1
                or batch["completed"] is not True
            ):
                self._feedback_reject(
                    "completion_status_mismatch",
                    "完成状态须对应末命令和同一完成回调",
                )
            contacts = _integer(
                sample.get("unexpected_contacts"),
                f"feedback.samples[{sample_index}].unexpected_contacts",
            )
            batch_contacts += contacts
            fault = sample.get("fault")
            if fault is not None:
                fault = _text(fault, "feedback.fault")
            if status == "fault" and fault is None:
                self._feedback_reject(
                    "missing_fault_reason", "故障反馈缺少原因"
                )
            measured = _vector(
                sample.get("q_actual"),
                6,
                f"feedback.samples[{sample_index}].q_actual",
            )
            if not self.plan.limits.contains(measured):
                self._feedback_reject(
                    "feedback_joint_limit", "反馈关节位置超出 XML 限位"
                )
            if command_index not in projected_covered:
                projected_covered.add(command_index)
                new_indices.append(command_index)
            local_last_t = stamp
            local_last_index = command_index
            if contacts:
                batch_fault = "unexpected_contact"
            if fault is not None:
                batch_fault = f"reported_fault:{fault}"
        completed = batch.get("completed") is True
        early = completed and len(projected_covered) != len(self.plan.commands)
        if early:
            batch_fault = "incomplete_completion_callback"
        elif completed and samples[-1]["status"] != "completed":
            self._feedback_reject(
                "completion_status_mismatch", "末反馈没有明确完成状态"
            )
        return {
            "idempotent": False,
            "sequence": sequence,
            "content": content,
            "payload_hash": payload_hash,
            "new_indices": new_indices,
            "last_t_s": local_last_t,
            "last_command_index": local_last_index,
            "sample_count": len(samples),
            "contacts": batch_contacts,
            "fault": batch_fault,
            "completed": completed,
            "early_completion_claim": early,
        }

    def _apply_feedback(self, prepared):
        """Apply already validated progress; also used for journal replay."""
        for command_index in prepared["new_indices"]:
            self.covered_commands.add(command_index)
            segment = self.plan.commands[command_index].segment_id
            self.segment_coverage[segment] = (
                self.segment_coverage.get(segment, 0) + 1
            )
        self.feedback_batches += 1
        self.feedback_samples += prepared["sample_count"]
        self.last_sequence = prepared["sequence"]
        self.seen_sequence[prepared["sequence"]] = prepared["payload_hash"]
        self.last_t_s = prepared["last_t_s"]
        self.last_command_index = prepared["last_command_index"]
        self.total_contacts += prepared["contacts"]
        self.early_completion_claim = (
            self.early_completion_claim or prepared["early_completion_claim"]
        )
        if prepared["completed"] and prepared["fault"] is None:
            self.completion_claimed = True

    def run_mock(self, batches: Iterable[dict], *, stop_on_fault=True):
        """Consume synthetic batches, converting process errors to hold."""
        if self.state != "EXECUTING":
            raise StateRejected(
                "invalid_state", "仅 EXECUTING 状态可运行 mock 反馈"
            )
        unexpected = False
        error = None
        try:
            for batch in batches:
                self.ingest_feedback(batch)
                if stop_on_fault and self.state != "EXECUTING":
                    break
        except (
            PlanRejected,
            StateRejected,
        ) as caught:
            error = {"code": caught.code, "message": str(caught)}
            if self.state == "EXECUTING":
                self.hold(caught.code, str(caught), cause="mock_process")
        except Exception as caught:  # noqa: BLE001 - deliberate fault boundary
            unexpected = True
            error = {
                "code": "mock_process_exception",
                "message": f"{type(caught).__name__}: {caught}",
            }
            self.hold(
                "mock_process_exception",
                error["message"],
                cause="mock_process",
            )
        if self.state == "EXECUTING" and not self.completion_claimed:
            message = "mock 反馈流在完整完成回调之前结束"
            self.hold("feedback_stream_ended", message, cause="feedback")
            error = {"code": "feedback_stream_ended", "message": message}
        return {
            "state": self.state,
            "finished": self.state == "FINISHED",
            "error": error,
            "unexpected_exception": unexpected,
            "completion": self.completion_report(),
        }

    def quality_binding(self):
        """Return the identity a quality producer must explicitly bind."""
        return {
            **self.plan.identity.to_dict(),
            "plan_sha256": self.plan.plan_sha256,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "execution_attempt": self.execution_attempts,
        }

    def _quality_integrity(self):
        if self.quality is None or self.quality_sha256 is None:
            return False
        try:
            return (
                sha256_file(self.quality_path) == self.quality_sha256
                and hashlib.sha256(_pretty_bytes(self.quality)).hexdigest()
                == self.quality_sha256
            )
        except (OSError, TypeError, ValueError):
            return False

    def _quality_report(self):
        if self.quality is None:
            return {
                "status": "missing",
                "complete": False,
                "source_kind": None,
                "covered_segments": [],
                "missing_segments": list(self.plan.admitted_segments),
                "reasons": ["quality_evidence_missing"],
            }
        evidence = self.quality
        admitted = set(self.plan.admitted_segments)
        covered = set(evidence["covered_segments"])
        missing = sorted(admitted - covered)
        results = evidence["segment_results"]
        failed = sorted(
            segment
            for segment in covered & admitted
            if results.get(str(segment)) != "pass"
        )
        region_failures = evidence.get("region_failures", [])
        reasons = []
        if not self._quality_integrity():
            reasons.append("quality_hash_mismatch")
        if evidence["task_status"] != "complete":
            reasons.append(f"task_status:{evidence['task_status']}")
        if not evidence["independent"]:
            reasons.append("quality_not_independent")
        if evidence.get("source_kind") not in {
            "mock_synthetic_quality",
            "simulation_reinspection",
            "real_measurement",
            "historical_simulation_result",
        }:
            reasons.append("quality_source_unknown")
        if evidence.get("execution_attempt") != self.execution_attempts:
            reasons.append("quality_execution_attempt_mismatch")
        if missing:
            reasons.append("quality_missing_segments")
        if failed:
            reasons.append("quality_segment_failures")
        if region_failures:
            reasons.append("quality_region_failures")
        if evidence.get("observational_release") is not True:
            reasons.append("observational_release_unproven")
        if evidence.get("goal_complete") is not True:
            reasons.append("goal_complete_unproven")
        for key in ("overgrind", "negative_limit_risk"):
            if evidence.get(key, 0):
                reasons.append(key)
        if evidence.get("automatic_regrind_allowed") is False:
            reasons.append("automatic_regrind_allowed_false")
        complete = not reasons
        return {
            "status": evidence["task_status"],
            "complete": complete,
            "source_kind": evidence.get("source_kind"),
            "real_evidence": evidence.get("real_evidence") is True,
            "independent": evidence["independent"],
            "sha256": self.quality_sha256,
            "execution_attempt": evidence.get("execution_attempt"),
            "covered_segments": sorted(covered),
            "missing_segments": missing,
            "failed_segments": failed,
            "region_failures": region_failures,
            "reasons": reasons,
        }

    def completion_report(self):
        """Independent execution, feedback and quality completeness."""
        machining_missing = []
        support_missing = []
        coverage = {}
        for segment in self.plan.segments:
            covered = self.segment_coverage.get(segment.segment_id, 0)
            missing = segment.command_count - covered
            coverage[str(segment.segment_id)] = {
                "kind": segment.kind,
                "covered": covered,
                "expected": segment.command_count,
                "complete": missing == 0,
            }
            if missing:
                if segment.kind == "machining":
                    machining_missing.append(segment.segment_id)
                else:
                    support_missing.append(segment.segment_id)
        missing_command_count = len(self.plan.commands) - len(
            self.covered_commands
        )
        feedback_complete = (
            missing_command_count == 0
            and not machining_missing
            and not support_missing
            and self.sequence_gaps == 0
        )
        fault_clear = self.fault_code is None
        execution_complete = (
            feedback_complete
            and self.completion_claimed
            and fault_clear
            and self.total_contacts == 0
            and not self.early_completion_claim
            and self.state in {"EXECUTING", "FINISHED"}
        )
        quality = self._quality_report()
        blocked = list(self.plan.blocked_segments)
        task_quality_complete = (
            execution_complete
            and quality["complete"]
            and not blocked
            and not quality["missing_segments"]
        )
        reasons = []
        if not feedback_complete:
            reasons.append("feedback_incomplete")
        if not self.completion_claimed:
            reasons.append("completion_callback_missing")
        if self.fault_code is not None:
            reasons.append(f"held:{self.fault_code}")
        if blocked:
            reasons.append("blocked_source_segments")
        if not quality["complete"]:
            reasons.append("quality_not_complete")
        return {
            "state": self.state,
            "execution_complete": execution_complete,
            "task_quality_complete": task_quality_complete,
            "feedback_complete": feedback_complete,
            "completion_callback": self.completion_claimed,
            "early_completion_claim": self.early_completion_claim,
            "feedback_batches": self.feedback_batches,
            "feedback_samples": self.feedback_samples,
            "covered_command_count": len(self.covered_commands),
            "expected_command_count": len(self.plan.commands),
            "missing_command_count": missing_command_count,
            "missing_segments": machining_missing,
            "missing_support_segments": support_missing,
            "blocked_source_segments": blocked,
            "admitted_segments": list(self.plan.admitted_segments),
            "segment_coverage": coverage,
            "quality": quality,
            "unexpected_contacts": self.total_contacts,
            "fault": self.fault,
            "fault_code": self.fault_code,
            "reasons": reasons,
            "scope": (
                "offline mock execution; completion callback is not task "
                "quality and is not real-controller acceptance"
            ),
            "real_acceptance_pending": True,
        }

    def set_quality_evidence(self, evidence):
        """Attach independent quality evidence and refresh the receipt."""
        self._require_state("EXECUTING", "FINISHED", "HELD")
        try:
            normalized = self._validated_quality(evidence)
        except (PlanRejected, StateRejected, TypeError, ValueError) as error:
            code = getattr(error, "code", "invalid_quality")
            if self.state == "EXECUTING":
                self.hold(code, str(error), cause="quality")
            raise StateRejected(code, str(error)) from error
        self.quality = normalized
        _atomic_write(self.quality_path, _pretty_bytes(normalized))
        self.quality_sha256 = sha256_file(self.quality_path)
        self._record(
            "quality_attached",
            {
                "status": normalized["task_status"],
                "covered_segments": normalized["covered_segments"],
            },
        )
        self._write_checkpoint()
        if self.state in TERMINAL_STATES:
            self._write_receipt()
        return self._quality_report()

    def _validated_quality(self, evidence):
        normalized = normalize_quality_evidence(evidence, self.plan.identity)
        _integer(
            normalized.get("execution_attempt"), "quality.execution_attempt",
            minimum=1,
        )
        for key, expected in self.quality_binding().items():
            if normalized.get(key) != expected:
                raise StateRejected(
                    "quality_binding_mismatch", f"质量证据 {key} 不一致"
                )
        if set(normalized["covered_segments"]) - set(self.plan.admitted_segments):
            raise StateRejected(
                "quality_unknown_segment", "质量证据包含未获准段"
            )
        if set(normalized["segment_results"]) - {
            str(segment) for segment in self.plan.admitted_segments
        }:
            raise StateRejected(
                "quality_unknown_segment", "质量结论包含未获准段"
            )
        return normalized

    def set_quality_from_run_result(self, result_path=None):
        """Attach a historical simulation result as non-real quality evidence."""
        path = Path(result_path or self.plan.plan_directory / "result.json")
        value, _, digest = _json_value(path, "result.json")
        if not isinstance(value, dict):
            raise StateRejected("invalid_quality", "result.json 须为对象")
        artifact = next(
            (
                artifact for artifact in self.plan.artifacts
                if artifact.role == "historical_run_result"
            ),
            None,
        )
        if (
            artifact is None
            or str(path.resolve()) != artifact.path
            or digest != artifact.sha256
        ):
            raise StateRejected(
                "quality_binding_mismatch", "历史结果未绑定本次计划来源"
            )
        evidence = {
            **self.quality_binding(),
            "schema": QUALITY_SCHEMA,
            "source_kind": "historical_simulation_result",
            "independent": False,
            "real_evidence": False,
            "task_status": value.get("task_status", "unknown"),
            "covered_segments": [],
            "segment_results": {},
            "observational_release": value.get("observational_release"),
            "goal_complete": value.get("goal_complete"),
            "input_artifact": {"path": str(path.resolve()), "sha256": digest},
            "scope": "historical simulation result; not independent measurement",
        }
        return self.set_quality_evidence(evidence)

    def finish(self):
        """Close execution only when feedback and callback are complete."""
        self._require_state("EXECUTING")
        self._ensure_valid()
        report = self.completion_report()
        if not report["execution_complete"]:
            self.hold(
                "incomplete_execution",
                "完成回调不能证明全部获准命令均有反馈",
                cause="completion",
            )
            report = self.completion_report()
            self._write_receipt()
            raise CompletionRejected(report)
        self.state = "FINISHED"
        self._record(
            "finished",
            {
                "feedback_batches": self.feedback_batches,
                "covered_commands": len(self.covered_commands),
            },
        )
        report = self.completion_report()
        self._write_checkpoint()
        self._write_receipt()
        return report

    def hold(self, code, message, *, cause="operator"):
        """Default safe recovery state; no further execution is automatic."""
        if self.state in TERMINAL_STATES:
            raise StateRejected(
                "invalid_state", f"{self.state} 状态不能转入 HELD"
            )
        self.state = "HELD"
        self.fault_code = code
        self.fault = message
        self._record(
            "held",
            {"code": code, "message": message, "cause": cause},
        )
        self._write_checkpoint()
        self._write_receipt()
        return self.summary()

    def cancel(self, reason):
        self._require_state("PLANNED", "CONFIRMED", "EXECUTING", "HELD")
        self.cancel_reason = _text(reason, "cancel_reason")
        self.state = "CANCELLED"
        self._record("cancelled", {"reason": self.cancel_reason})
        self._write_checkpoint()
        self._write_receipt()
        return self.summary()

    def reject(self, reason):
        self._require_state("PLANNED", "CONFIRMED", "HELD")
        self.reject_reason = _text(reason, "reject_reason")
        self.state = "REJECTED"
        self._record("rejected", {"reason": self.reject_reason})
        self._write_checkpoint()
        self._write_receipt()
        return self.summary()

    def recover(
        self,
        *,
        explicit_mock_recovery=False,
        operator="",
        acknowledge_hold=False,
    ):
        """Revalidate sources and require new confirmation, never auto-run."""
        self._require_state("HELD")
        if explicit_mock_recovery is not True:
            self._record(
                "recovery_rejected",
                {"code": "explicit_mock_recovery_required"},
            )
            self._write_checkpoint()
            raise RecoveryRejected(
                "explicit_mock_recovery_required",
                "恢复须显式声明本次为 mock 恢复",
            )
        try:
            operator = _text(operator, "operator")
        except PlanRejected as error:
            self._record("recovery_rejected", {"code": error.code})
            self._write_checkpoint()
            raise RecoveryRejected(error.code, str(error)) from error
        if acknowledge_hold is not True:
            self._record(
                "recovery_rejected",
                {"code": "hold_not_acknowledged"},
            )
            self._write_checkpoint()
            raise RecoveryRejected(
                "hold_not_acknowledged", "恢复须确认已处理 hold 原因"
            )
        try:
            self._ensure_valid()
        except PlanRejected as error:
            self._record(
                "recovery_rejected",
                {"code": error.code, "message": str(error)},
            )
            self._write_checkpoint()
            raise RecoveryRejected(error.code, str(error)) from error
        self.recovery_count += 1
        self.operator = operator
        self.fault = None
        self.fault_code = None
        self.state = "PLANNED"
        self._record(
            "recovered",
            {
                "operator": operator,
                "recovery_count": self.recovery_count,
                "requires_confirmation": True,
            },
        )
        self._write_checkpoint()
        self._write_receipt()
        return self.summary()

    @classmethod
    def resume(cls, checkpoint_path, plan, *, clock=time.time):
        """Restart into HELD; an interrupted run never resumes automatically."""
        checkpoint_path = Path(checkpoint_path).resolve()
        try:
            checkpoint, _, _ = _json_value(checkpoint_path, "checkpoint.json")
        except PlanRejected as error:
            raise StateRejected(error.code, str(error)) from error
        if not isinstance(checkpoint, dict):
            raise StateRejected("invalid_checkpoint", "检查点须为对象")
        if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
            raise StateRejected("invalid_checkpoint", "检查点 schema 不匹配")
        if checkpoint.get("plan_sha256") != plan.plan_sha256:
            raise StateRejected(
                "checkpoint_plan_mismatch", "检查点与计划哈希不一致"
            )
        previous = checkpoint.get("state")
        if previous not in TERMINAL_STATES | RECOVERABLE_STATES:
            raise StateRejected("invalid_checkpoint", "检查点状态无效")
        for key in ("request_id", "execution_id", "controller_id"):
            try:
                _text(checkpoint.get(key), f"checkpoint.{key}")
            except PlanRejected as error:
                raise StateRejected(error.code, str(error)) from error
        controller = cls.__new__(cls)
        controller._setup(
            plan,
            checkpoint_path.parent,
            clock=clock,
            request_id=checkpoint.get("request_id"),
            execution_id=checkpoint.get("execution_id"),
            controller_id=checkpoint.get("controller_id", ""),
            checkpoint_every=64,
            feedback_timeout_s=checkpoint.get("feedback_timeout_s"),
        )
        if not controller.controller_id.startswith("mock:"):
            raise StateRejected("non_mock_controller", "检查点不是 mock 控制器")
        if not controller.events_path.exists():
            raise StateRejected("missing_source", "检查点事件日志缺失")
        controller.plan.write_manifest(controller.workdir / "plan_manifest.json")
        for name, digest in (
            ("mock_program.json", checkpoint.get("program_sha256")),
            ("feedback.jsonl", checkpoint.get("feedback_sha256")),
            ("events.jsonl", checkpoint.get("events_sha256")),
        ):
            try:
                actual = sha256_file(controller.workdir / name)
            except OSError as error:
                raise StateRejected(
                    "missing_source", f"恢复所需文件缺失：{name}"
                ) from error
            if actual != digest:
                raise StateRejected(
                    "checkpoint_artifact_mismatch", f"检查点 {name} 哈希不一致"
                )
        controller.program_sha256 = checkpoint["program_sha256"]
        if (
            hashlib.sha256(_canonical_bytes(controller._program_value())).hexdigest()
            != controller.program_sha256
        ):
            raise StateRejected(
                "program_hash_mismatch", "恢复程序内容未绑定同一命令与计划"
            )
        try:
            controller._load_feedback_log()
            controller._load_checkpoint_fields(checkpoint)
            controller._validate_event_log(checkpoint)
        except PlanRejected as error:
            raise StateRejected(
                "invalid_checkpoint", str(error)
            ) from error
        if previous in TERMINAL_STATES:
            controller.state = previous
            if (
                previous == "FINISHED"
                and not controller.completion_report()["execution_complete"]
            ):
                raise StateRejected(
                    "checkpoint_completion_mismatch",
                    "检查点完成状态没有完整反馈与完成回调支持",
                )
            controller._record("resumed_terminal", {"previous_state": previous})
        else:
            controller.state = "HELD"
            controller.fault_code = (
                checkpoint.get("fault_code") or "interrupted_restart"
            )
            controller.fault = (
                checkpoint.get("fault")
                or "controller process restarted; automatic execution disabled"
            )
            controller._record(
                "interrupted_restart",
                {
                    "previous_state": previous,
                    "resume_policy": "hold",
                    "message": controller.fault,
                },
            )
        controller._write_checkpoint()
        controller._write_receipt()
        return controller

    def _load_checkpoint_fields(self, checkpoint):
        derived = {
            "feedback_batches": self.feedback_batches,
            "feedback_samples": self.feedback_samples,
            "covered_commands": sorted(self.covered_commands),
            "segment_coverage": {
                str(key): value
                for key, value in sorted(self.segment_coverage.items())
            },
            "last_sequence": self.last_sequence,
            "last_command_index": self.last_command_index,
            "last_t_s": (
                None if self.last_t_s == -math.inf else self.last_t_s
            ),
            "completion_claimed": self.completion_claimed,
            "early_completion_claim": self.early_completion_claim,
            "total_contacts": self.total_contacts,
            "sequence_gaps": self.sequence_gaps,
            "seen_sequence": {
                str(key): value for key, value in sorted(self.seen_sequence.items())
            },
        }
        if _canonical_bytes({
            key: checkpoint.get(key) for key in derived
        }) != _canonical_bytes(derived):
            raise StateRejected(
                "checkpoint_feedback_mismatch",
                "检查点计数、时间、完成状态或覆盖与完整反馈日志不一致",
            )
        self.event_count = _integer(
            checkpoint.get("event_count"), "checkpoint.event_count"
        )
        self.recovery_count = _integer(
            checkpoint.get("recovery_count"), "checkpoint.recovery_count"
        )
        self.execution_attempts = _integer(
            checkpoint.get("execution_attempts"),
            "checkpoint.execution_attempts",
        )
        self.started_at_s = checkpoint.get("started_at_s")
        if self.started_at_s is not None:
            self.started_at_s = _finite(
                self.started_at_s, "checkpoint.started_at_s", minimum=0
            )
        self.last_control_clock_s = _finite(
            checkpoint.get("last_control_clock_s"),
            "checkpoint.last_control_clock_s", minimum=0,
        )
        self.last_feedback_clock_s = checkpoint.get("last_feedback_clock_s")
        if self.last_feedback_clock_s is not None:
            self.last_feedback_clock_s = _finite(
                self.last_feedback_clock_s,
                "checkpoint.last_feedback_clock_s", minimum=0,
            )
            if self.last_feedback_clock_s > self.last_control_clock_s:
                raise StateRejected(
                    "invalid_checkpoint", "反馈接收时间晚于检查点控制时间"
                )
        self.operator = checkpoint.get("operator")
        self.quality_sha256 = checkpoint.get("quality_sha256")
        self.cancel_reason = checkpoint.get("cancel_reason")
        self.reject_reason = checkpoint.get("reject_reason")
        if self.quality_sha256 is not None:
            if (
                not self.quality_path.is_file()
                or sha256_file(self.quality_path) != self.quality_sha256
            ):
                raise StateRejected(
                    "checkpoint_artifact_mismatch", "恢复质量证据哈希不一致"
                )
            value, _, _ = _json_value(
                self.quality_path, "quality_evidence.json"
            )
            self.quality = self._validated_quality(value)
        elif self.quality_path.exists():
            raise StateRejected(
                "checkpoint_artifact_mismatch", "存在未绑定的质量证据文件"
            )

    def _load_feedback_log(self):
        if not self.feedback_path.exists():
            raise StateRejected("missing_source", "反馈日志缺失")
        with self.feedback_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    batch = json.loads(
                        line,
                        object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_constant,
                    )
                    prepared = self._prepare_feedback(batch)
                    if prepared["idempotent"]:
                        raise StateRejected(
                            "invalid_feedback_log", "日志包含重复批次"
                        )
                except (
                    json.JSONDecodeError, ControllerError, TypeError,
                    ValueError, OverflowError,
                ) as error:
                    raise StateRejected(
                        "invalid_feedback_log",
                        f"反馈日志第 {line_number} 行无效",
                    ) from error
                self._apply_feedback(prepared)

    def _validate_event_log(self, checkpoint):
        previous_clock = -math.inf
        last_event = None
        last_started = None
        last_feedback = None
        attempts = 0
        recoveries = 0
        with self.events_path.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    event = json.loads(
                        line, object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=_reject_constant,
                    )
                    stamp = _finite(event.get("clock_s"), "event.clock_s")
                    if (
                        _integer(event.get("event"), "event.index") != number
                        or stamp < previous_clock
                        or event.get("state") not in (
                            TERMINAL_STATES | RECOVERABLE_STATES
                        )
                        or any(
                            event.get(key) != expected
                            for key, expected in (
                                ("plan_sha256", self.plan.plan_sha256),
                                ("request_id", self.request_id),
                                ("execution_id", self.execution_id),
                                ("controller_id", self.controller_id),
                            )
                        )
                    ):
                        raise ValueError("事件顺序、时间或身份不一致")
                except (
                    json.JSONDecodeError, PlanRejected, TypeError, ValueError,
                ) as error:
                    raise StateRejected(
                        "invalid_event_log", "恢复事件日志验证失败"
                    ) from error
                previous_clock = stamp
                last_event = event
                if event.get("type") == "started":
                    attempts += 1
                    last_started = last_feedback = stamp
                elif event.get("type") in {
                    "feedback_accepted", "feedback_fault"
                }:
                    last_feedback = stamp
                elif event.get("type") == "recovered":
                    recoveries += 1
        if (
            last_event is None
            or last_event["event"] != self.event_count
            or last_event.get("state") != checkpoint.get("state")
            or previous_clock != self.last_control_clock_s
            or checkpoint.get("saved_at_s") != self.last_control_clock_s
            or self.started_at_s != last_started
            or self.last_feedback_clock_s != last_feedback
            or self.execution_attempts != attempts
            or self.recovery_count != recoveries
        ):
            raise StateRejected(
                "checkpoint_event_mismatch", "检查点状态或时钟与事件日志不一致"
            )

    def _feedback_samples(self):
        samples = []
        if not self.feedback_path.exists():
            return samples
        with self.feedback_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                batch = json.loads(line)
                for sample in batch.get("samples", []):
                    samples.append(
                        {
                            "t_s": sample.get("t_s"),
                            "command_index": sample.get("command_index"),
                            "segment_id": sample.get("segment_id"),
                            "status": sample.get("status"),
                            "q_actual": sample.get("q_actual"),
                            "unexpected_contacts": sample.get(
                                "unexpected_contacts", 0
                            ),
                            "fault": sample.get("fault"),
                        }
                    )
        return samples

    def _artifact_reference(self, path):
        path = Path(path).resolve()
        return {
            "path": str(path),
            "sha256": sha256_file(path),
        }

    def _write_receipt(self):
        report = self.completion_report()
        samples = self._feedback_samples()
        feedback_evidence = {
            "schema": FEEDBACK_SCHEMA,
            "source_kind": "synthetic",
            "controller_id": self.controller_id,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "plan_sha256": self.plan.plan_sha256,
            "program_sha256": self.program_sha256,
            "part_id": self.plan.identity.part_id,
            "serial": self.plan.identity.serial,
            "cad_revision": self.plan.identity.cad_revision,
            "fixture_revision": self.plan.identity.fixture_revision,
            "fixture_id": self.plan.identity.fixture_id,
            "tool_id": self.plan.identity.tool_id,
            "frame": self.plan.identity.frame,
            "units": dict(CANONICAL_UNITS),
            "completed": (
                self.state == "FINISHED" and report["execution_complete"]
            ),
            "samples": samples,
            "mock_identity": {
                "controller_id": self.controller_id,
                "real_controller": False,
                "network_used": False,
                "physical_io": False,
                "real_command_mapping": False,
                "feedback_generated_offline": True,
            },
            "scope": (
                "synthetic offline feedback from the explicit mock; "
                "not a physical controller trace"
            ),
        }
        feedback_path = self.workdir / "feedback_evidence.json"
        _atomic_write(feedback_path, _pretty_bytes(feedback_evidence))
        report_path = self.workdir / "completion_report.json"
        _atomic_write(report_path, _pretty_bytes(report))
        plan_artifact = next(
            artifact
            for artifact in self.plan.artifacts
            if artifact.role == "accepted_motion_commands"
        )
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "source_kind": "synthetic",
            "evidence_scope": "offline_mock",
            "controller_id": self.controller_id,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "plan_sha256": self.plan.plan_sha256,
            "part_id": self.plan.identity.part_id,
            "serial": self.plan.identity.serial,
            "cad_revision": self.plan.identity.cad_revision,
            "fixture_revision": self.plan.identity.fixture_revision,
            "fixture_id": self.plan.identity.fixture_id,
            "tool_id": self.plan.identity.tool_id,
            "frame": self.plan.identity.frame,
            "units": dict(CANONICAL_UNITS),
            "plan": self._artifact_reference(
                self.workdir / "plan_manifest.json"
            ),
            "program": self._artifact_reference(self.program_path),
            "feedback": self._artifact_reference(feedback_path),
            "command_source": plan_artifact.to_dict(),
            "completed": (
                self.state == "FINISHED" and report["execution_complete"]
            ),
            "feedback_completed": report["feedback_complete"],
            "execution_complete": report["execution_complete"],
            "task_quality_complete": report["task_quality_complete"],
            "quality_status": report["quality"]["status"],
            "quality": report["quality"],
            "quality_evidence": (
                {
                    "path": str(self.quality_path),
                    "sha256": self.quality_sha256,
                }
                if self.quality is not None
                else None
            ),
            "missing_segments": report["missing_segments"],
            "missing_support_segments": report["missing_support_segments"],
            "blocked_source_segments": report["blocked_source_segments"],
            "unexpected_contacts": report["unexpected_contacts"],
            "feedback_samples": len(samples),
            "feedback_batches": self.feedback_batches,
            "recovery_count": self.recovery_count,
            "execution_attempts": self.execution_attempts,
            "calibrations": {
                "camera_calibrated": False,
                "tcp_calibrated": False,
                "fixture_validated": False,
                "coordinate_mapping_confirmed": False,
                "force_control_calibrated": False,
                "removal_process_calibrated": False,
            },
            "fit_and_force_calibrated": False,
            "mock_identity": {
                "controller_id": self.controller_id,
                "real_controller": False,
                "network_used": False,
                "physical_io": False,
                "real_command_mapping": False,
            },
            "completion_report": self._artifact_reference(report_path),
            "events": self._artifact_reference(self.events_path),
            "feedback_journal": self._artifact_reference(self.feedback_path),
            "checkpoint": self._artifact_reference(self.checkpoint_path),
            "scope": (
                "offline state-machine receipt; no command was sent to an "
                "ESTUN controller and no real mapping is assumed"
            ),
            "real_acceptance_pending": True,
        }
        _atomic_write(
            self.workdir / "controller_receipt.json", _pretty_bytes(receipt)
        )
        return receipt

    def summary(self):
        return {
            "state": self.state,
            "plan_sha256": self.plan.plan_sha256,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "controller_id": self.controller_id,
            "workdir": str(self.workdir),
            "feedback_batches": self.feedback_batches,
            "feedback_samples": self.feedback_samples,
            "last_sequence": self.last_sequence,
            "covered_command_count": len(self.covered_commands),
            "next_command_index": self.next_command_index,
            "fault_code": self.fault_code,
            "fault": self.fault,
            "recovery_count": self.recovery_count,
            "execution_attempts": self.execution_attempts,
            "completion": self.completion_report(),
        }


class MockFeedbackSource:
    """Generate explicit synthetic feedback in accepted command order."""

    def __init__(
        self,
        plan,
        request_id,
        execution_id,
        *,
        controller_id=MOCK_CONTROLLER_ID,
        batch_size=16,
        start_command_index=0,
        start_sequence=1,
        fault_at=None,
        exception_at=None,
    ):
        self.plan = plan
        self.request_id = request_id
        self.execution_id = execution_id
        self.controller_id = controller_id
        self.batch_size = max(1, int(batch_size))
        self.start_command_index = max(0, int(start_command_index))
        self.start_sequence = max(1, int(start_sequence))
        self.fault_at = fault_at
        self.exception_at = exception_at

    def _batch(self, sequence, samples, completed):
        return {
            "schema": FEEDBACK_SCHEMA,
            "source_kind": "synthetic",
            "controller_id": self.controller_id,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "plan_sha256": self.plan.plan_sha256,
            "part_id": self.plan.identity.part_id,
            "serial": self.plan.identity.serial,
            "cad_revision": self.plan.identity.cad_revision,
            "fixture_revision": self.plan.identity.fixture_revision,
            "fixture_id": self.plan.identity.fixture_id,
            "tool_id": self.plan.identity.tool_id,
            "frame": self.plan.identity.frame,
            "units": dict(CANONICAL_UNITS),
            "sequence": sequence,
            "samples": samples,
            "completed": completed,
            "mock_identity": {
                "real_controller": False,
                "network_used": False,
                "physical_io": False,
                "feedback_generated_offline": True,
            },
        }

    def __iter__(self):
        indices = list(range(self.start_command_index, len(self.plan.commands)))
        for batch_number, offset in enumerate(
            range(0, len(indices), self.batch_size)
        ):
            sequence = self.start_sequence + batch_number
            group = indices[offset : offset + self.batch_size]
            samples = []
            fault = None
            for index in group:
                if self.exception_at == index:
                    raise RuntimeError(
                        f"mock process exception at command {index}"
                    )
                command = self.plan.commands[index]
                final = index == len(self.plan.commands) - 1
                status = "completed" if final else "executing"
                segment = self.plan.segment(command.segment_id)
                if index == segment.command_indices[-1] and not final:
                    status = "segment_complete"
                sample_fault = None
                if self.fault_at == index:
                    status = "fault"
                    sample_fault = "injected_mock_fault"
                    fault = sample_fault
                samples.append(
                    {
                        "t_s": command.t,
                        "command_index": index,
                        "segment_id": command.segment_id,
                        "status": status,
                        "q_actual": list(command.q),
                        "unexpected_contacts": 0,
                        "fault": sample_fault,
                    }
                )
                if self.fault_at == index:
                    break
            completed = (
                fault is None
                and bool(samples)
                and samples[-1]["command_index"] == len(self.plan.commands) - 1
            )
            yield self._batch(sequence, samples, completed)
            if fault is not None:
                return


def run_mock_cycle(
    plan,
    workdir,
    *,
    clock=time.time,
    batch_size=16,
    fault_at=None,
    exception_at=None,
    quality=None,
    confirm_operator="offline-operator",
):
    """Run one complete mock plan-to-finish cycle."""
    controller = MockController(plan, workdir, clock=clock)
    controller.confirm(confirm_operator)
    controller.start()
    source = MockFeedbackSource(
        plan,
        controller.request_id,
        controller.execution_id,
        batch_size=batch_size,
        fault_at=fault_at,
        exception_at=exception_at,
    )
    result = controller.run_mock(source)
    if quality is not None:
        controller.set_quality_evidence(quality)
    finish_error = None
    if controller.state == "EXECUTING":
        try:
            controller.finish()
        except CompletionRejected as error:
            finish_error = {
                "code": error.code,
                "message": str(error),
                "report": error.report,
            }
    return {
        "controller": controller,
        "run": result,
        "finish_error": finish_error,
        "summary": controller.summary(),
    }


def overall_failure_modes(requirement_path=None):
    """List the environment and data gaps that block real acceptance."""
    del requirement_path
    return [
        "no ESTUN controller, physical socket or command mapping was exercised",
        "no MuJoCo collision, dynamics or tracking replay was performed",
        "no real joint feedback, clock monotonicity or controller restart log",
        "no camera, TCP, fixture repeatability or force/removal calibration",
        "no independent signed before/after surface measurement",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Validate an accepted workstation command bundle and optionally "
            "replay it through the explicit offline mock controller."
        )
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--valid-hours", type=float, default=24.0)
    parser.add_argument("--execute-mock", action="store_true")
    parser.add_argument("--fault-at", type=int)
    parser.add_argument("--exception-at", type=int)
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = load_plan_from_run(
        args.run_dir,
        valid_for_s=args.valid_hours * 3600,
    )
    plan.write_manifest(output / "plan_manifest.json")
    response = {
        "schema": "daiyi-controller-validation-v1",
        "plan": plan.summary(),
        "mock_executed": False,
    }
    if args.execute_mock:
        result = run_mock_cycle(
            plan,
            output / "mock",
            batch_size=args.batch_size,
            fault_at=args.fault_at,
            exception_at=args.exception_at,
        )
        response["mock_executed"] = True
        response["mock"] = result["summary"]
    _atomic_write(output / "validation.json", _pretty_bytes(response))
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
