"""Local current-CAD workstation simulation, with retained FR5 evidence."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import imageio_ffmpeg

from .workstation_session import (
    SESSION_ROOT,
    SessionSafetyError,
    SessionStateError,
    create_session,
    load_session,
    validate_session_action,
    validate_session_config,
)

# Select the headless context before the legacy model imports MuJoCo.
# isort: split

from . import project
from .cad_defects import CASES as LEGACY_CASES
from .casting_profiles import BASE_PROFILE, available_profiles
from .learned_defects import file_sha256, registered_checkpoint
from .workstation_defects import WORKSTATION_CASES
from .workstation_jobs import JOB_FIELDS, job_profile, job_profiles
from .workstation_model import GRASP_SETUPS, SOURCE_FILES, source_hashes
from .workstation_replay import export_compact_replay

ROOT = project.ROOT
CASES = set(WORKSTATION_CASES)
JOBS = {}
VIDEO_JOBS = {}
LOCK = threading.Lock()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run_directory(run_id):
    if run_id in {"demo_v1", "demo_v2", "demo_v3"}:
        return ROOT / "results" / run_id
    if not re.fullmatch(r"[a-f0-9]{32}", run_id or ""):
        raise ValueError("无效的运行编号")
    return project.RUNS / "interactive" / run_id


def payload(run_id):
    out = run_directory(run_id)
    if not out.exists() and run_id not in JOBS:
        raise FileNotFoundError("找不到该运行记录")
    status = read_json(out / "status.json", {"stage": "queued"})
    process = JOBS.get(run_id, {}).get("process")
    if (
        status.get("stage") == "complete"
        and process is not None
        and (
            process.poll() is None
            or JOBS.get(run_id, {}).get("artifact_finalizing", False)
        )
    ):
        status = {**status, "stage": "finalizing"}
    result = read_json(out / "result.json")
    request = JOBS.get(run_id, {}).get("request") or read_json(
        project.RUNS / "interactive/.requests" / (run_id + ".json"), {}
    )
    files = (
        [p.name for p in out.iterdir() if p.is_file()] if out.exists() else []
    )
    return {
        "id": run_id,
        "status": status,
        "result": result,
        "detections": read_json(out / "detections.json", []),
        "paths": read_json(out / "paths.json", []),
        "files": files,
        "artifact_base": "/artifacts/" + str(out.relative_to(ROOT)) + "/",
        "saved_demo": run_id.startswith("demo_"),
        "request": request,
        "scene": (result or request).get("scene", "fr5"),
        "live": read_json(out / "live.json"),
        "components": read_json(out / "components.json", []),
        "after_paths": read_json(out / "after_paths.json", []),
        "views": read_json(out / "views.json", []),
        "planning": read_json(out / "planning_live.json", {}),
        "perception_metrics": read_json(out / "perception_metrics.json", {}),
        "after_views": read_json(out / "after_views.json", []),
    }


def validate(body):
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    profile = job_profile(body.get("workpiece_profile"))
    if profile:
        body = {**body, **{key: profile[key] for key in JOB_FIELDS}}
    seed = body.get("seed", 7)
    case = body.get("case", "mixed")
    execute = body.get("execute", False)
    scene = body.get("scene", "workstation")
    if body.get("source_id") and "scene" not in body:
        scene = payload(body["source_id"]).get("scene", "fr5")
    if scene not in {"workstation", "fr5"}:
        raise ValueError("不支持的仿真场景模型")
    if profile and scene != "workstation":
        raise ValueError("工件预设仅支持当前 ESTUN 工作站")
    grasp_setup = body.get("grasp_setup", "original")
    if grasp_setup not in GRASP_SETUPS or (
        scene != "workstation" and grasp_setup != "original"
    ):
        raise ValueError("不支持的夹持方案")
    targeted_rescan = body.get("targeted_rescan", False)
    if type(targeted_rescan) is not bool or (
        targeted_rescan and scene != "workstation"
    ):
        raise ValueError("仅工作站可启用复检补拍")
    mode = body.get("mode", "removal")
    if mode not in {"removal", "clearance"}:
        raise ValueError("不支持的加工仿真模式")
    views = body.get("views", "multi")
    if views not in {"single", "multi"}:
        raise ValueError("不支持的采集方式")
    tool = body.get("tool", "belt")
    if tool not in {"belt", "wheel"}:
        raise ValueError("不支持的固定磨具")
    max_seed = 1000000 if scene == "workstation" else 100000
    if type(seed) is not int or not 0 <= seed <= max_seed:
        raise ValueError(f"场景种子须为 0–{max_seed} 的整数")
    if case not in CASES:
        raise ValueError("不支持的铸件场景")
    if scene == "fr5" and case not in LEGACY_CASES:
        raise ValueError("研究合成铸件工况仅支持当前 ESTUN 工作站")
    casting_profile = body.get("casting_profile", BASE_PROFILE)
    if casting_profile not in available_profiles():
        raise ValueError("不支持或尚未准备的缺陷构造参数版本")
    detector = body.get("detector", "geometric")
    if detector not in {"geometric", "learned", "hybrid"}:
        raise ValueError("不支持的缺陷识别方式")
    checkpoint = None
    if detector in {"learned", "hybrid"}:
        if scene != "workstation":
            raise ValueError("专用分割模型仅支持当前 ESTUN 工作站")
        checkpoint = registered_checkpoint()
        if checkpoint is None:
            raise ValueError("专用分割模型尚未训练并验证")
    if type(execute) is not bool:
        raise ValueError("execute 必须是布尔值")
    roi = body.get("roi")
    width, height = (1024, 768) if scene == "workstation" else (768, 576)
    if roi is not None:
        if not isinstance(roi, list) or not 3 <= len(roi) <= 100:
            raise ValueError("ROI 须包含 3–100 个顶点")
        for point in roi:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or any(type(v) not in (int, float) for v in point)
                or not 0 <= point[0] < width
                or not 0 <= point[1] < height
            ):
                raise ValueError(
                    f"ROI 顶点须在 {width}×{height} 原始图像范围内"
                )
        area = (
            abs(
                sum(
                    roi[i][0] * roi[(i + 1) % len(roi)][1]
                    - roi[(i + 1) % len(roi)][0] * roi[i][1]
                    for i in range(len(roi))
                )
            )
            / 2
        )
        if area < 1:
            raise ValueError("ROI 面积过小，请扩大选区")
    ids = body.get("ids")
    if ids is not None and (
        not isinstance(ids, list)
        or len(ids) > 100
        or any(type(v) is not int or v < 1 or v > 10000 for v in ids)
    ):
        raise ValueError("区域编号须为有效正整数列表")
    if execute or ids is not None:
        source_id = body.get("source_id")
        if not source_id:
            raise ValueError("请先重新拍照与规划，再选择区域")
        source = payload(source_id)
        source_result = source["result"]
        if source["status"].get("stage") != "complete" or not source_result:
            raise ValueError("请等待当前规划完成")
        if (
            source_result["seed"] != seed
            or source_result["case"] != case
            or source_result.get("roi") != roi
            or source.get("scene", "fr5") != scene
            or (scene == "workstation" and source_result.get("mode") != mode)
            or (
                scene == "workstation"
                and source_result.get("views", "multi") != views
            )
            or (
                scene == "workstation"
                and source_result.get("tool", {}).get("key", "wheel") != tool
            )
            or (
                case.startswith("casting_")
                and source_result.get("casting_profile", BASE_PROFILE)
                != casting_profile
            )
            or source_result.get("detector_kind", "geometric") != detector
            or source_result.get("grasp_setup", "original") != grasp_setup
            or source_result.get("targeted_rescan", False) != targeted_rescan
            or (source_result.get("workpiece_profile") or {}).get("sha256")
            != (profile or {}).get("sha256")
        ):
            raise ValueError("场景或 ROI 已修改，请先重新规划")
        if detector in {"learned", "hybrid"} and source_result.get(
            "learned_detector", {}
        ).get("checkpoint_sha256") != file_sha256(checkpoint):
            raise ValueError("识别模型权重已修改，请重新采集并预测")
        snapshot = run_directory(source_id) / "source_snapshot"
        source_files = (
            SOURCE_FILES
            if scene == "workstation"
            else (
                "run.py",
                "geometry.py",
                "perception.py",
                "planning.py",
                "sim.py",
                "execution.py",
                "calibration.py",
                "robot_model.py",
                "assets.py",
            )
        )
        for name in source_files:
            saved_source = snapshot / name
            if (
                not saved_source.is_file()
                or (project.SOURCE / name).read_bytes()
                != saved_source.read_bytes()
            ):
                raise ValueError("计算模型已更新，请先重新拍照与规划")
        if (
            scene == "workstation"
            and read_json(run_directory(source_id) / "source_hashes.json")
            != source_hashes()
        ):
            raise ValueError("整站模型已更新，请重新采集观测并预测轨迹")
        allowed = {
            d["id"]
            for d in source["detections"]
            if d["kind"] in {"seam", "burr"}
        }
        if ids is not None and not set(ids).issubset(allowed):
            raise ValueError("保留结构与待复核区域不可提交执行")
        if execute and ids is not None and not ids:
            raise ValueError("请至少选择一个可处理区域")
    return {
        "seed": seed,
        "case": case,
        "roi": roi,
        "ids": ids,
        "execute": execute,
        "source_id": body.get("source_id"),
        "scene": scene,
        "mode": mode,
        "views": views,
        "tool": tool,
        "casting_profile": casting_profile,
        "detector": detector,
        "workpiece_profile": profile["id"] if profile else None,
        "grasp_setup": grasp_setup,
        "targeted_rescan": targeted_rescan,
        "checkpoint": str(checkpoint) if checkpoint is not None else None,
    }


def start_job(request):
    with LOCK:
        if any(
            job["process"].poll() is None
            or job.get("artifact_finalizing", False)
            for job in JOBS.values()
        ):
            raise RuntimeError("当前运行尚未结束，请等待后再提交")
        run_id = uuid.uuid4().hex
        out = run_directory(run_id)
        requests = project.RUNS / "interactive/.requests"
        logs = project.RUNS / "interactive/.logs"
        requests.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        (requests / (run_id + ".json")).write_text(
            json.dumps(request, ensure_ascii=False)
        )
        command = [
            sys.executable,
            "-m",
            "daiyi_grinding.workstation_run"
            if request.get("scene") == "workstation"
            else "daiyi_grinding.run",
            "--seed",
            str(request["seed"]),
            "--case",
            request["case"],
            "--output",
            str(out),
        ]
        if request["roi"] is not None:
            roi_file = requests / (run_id + "_roi.json")
            roi_file.write_text(json.dumps({"polygon": request["roi"]}))
            command += ["--roi", str(roi_file)]
        if request["ids"] is not None:
            command += ["--ids", ",".join(map(str, request["ids"])) or "0"]
        if request.get("scene") == "workstation":
            command += ["--mode", request.get("mode", "removal")]
            command += ["--grasp-setup", request.get("grasp_setup", "original")]
            if request.get("targeted_rescan"):
                command.append("--targeted-rescan")
            command += ["--views", request.get("views", "multi")]
            command += ["--tool", request.get("tool", "belt")]
            command += [
                "--casting-profile",
                request.get("casting_profile", BASE_PROFILE),
            ]
            command += ["--detector", request.get("detector", "geometric")]
            if request.get("workpiece_profile"):
                command += ["--workpiece-profile", request["workpiece_profile"]]
            if request.get("checkpoint"):
                command += ["--checkpoint", request["checkpoint"]]
            if not request["execute"]:
                command += ["--perception-only"]
        else:
            command += (
                ["--video"] if request["execute"] else ["--perception-only"]
            )
        mujoco_lock = project.RUNS / "enterprise_offline_20261123/mujoco.lock"
        mujoco_lock.parent.mkdir(parents=True, exist_ok=True)
        command = ["flock", str(mujoco_lock), *command]
        log_path = logs / (run_id + ".log")
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "MUJOCO_GL": "egl",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                },
            )
        JOBS[run_id] = {
            "process": process,
            "request": request,
            "artifact_finalizing": request.get("scene") == "workstation",
        }

    def reap():
        code = process.wait()
        if not code and request.get("scene") == "workstation":
            try:
                export_compact_replay(out)
            except Exception as error:
                (out / "replay_export_error.json").write_text(
                    json.dumps({"error": str(error)}, ensure_ascii=False)
                )
            finally:
                JOBS[run_id]["artifact_finalizing"] = False
            latest = project.RUNS / "workstation/latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps({"id": run_id}))
        else:
            JOBS[run_id]["artifact_finalizing"] = False
        if code and read_json(out / "status.json", {}).get("stage") != "failed":
            out.mkdir(parents=True, exist_ok=True)
            (out / "status.json").write_text(
                json.dumps(
                    {
                        "stage": "failed",
                        "error": "仿真进程退出，请查看运行日志",
                        "exit_code": code,
                    },
                    ensure_ascii=False,
                )
            )

    threading.Thread(target=reap, daemon=True).start()
    return run_id


def compatible_video(run_id):
    out = run_directory(run_id)
    source = out / "demo.mp4"
    if (
        not source.is_file()
        or read_json(out / "status.json", {}).get("stage") != "complete"
    ):
        raise FileNotFoundError("当前运行尚无完整录像")
    cache = project.RUNS / "interactive/.video"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (run_id + ".webm")
    with LOCK:
        process = VIDEO_JOBS.get(run_id)
        if target.exists() and (process is None or process.poll() == 0):
            return {
                "stage": "complete",
                "url": "/artifacts/runs/interactive/.video/" + target.name,
            }
        if process is not None:
            if process.poll() is None:
                return {"stage": "encoding"}
            if process.poll() == 0:
                return {"stage": "encoding"}
            raise ValueError("兼容录像生成失败，请下载原始 MP4")
        temporary = cache / (run_id + "." + uuid.uuid4().hex + ".partial.webm")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-an",
            "-vf",
            "scale=960:-2",
            "-c:v",
            "libvpx",
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            "-threads",
            "2",
            "-b:v",
            "1500k",
            str(temporary),
        ]
        with (cache / (run_id + ".log")).open("wb") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT
            )
            VIDEO_JOBS[run_id] = process

        def finish_video():
            if process.wait() == 0:
                temporary.replace(target)

        threading.Thread(target=finish_video, daemon=True).start()
    return {"stage": "encoding"}


class SessionStore:
    """Process-local session owners; MuJoCo work never holds the registry lock."""

    def __init__(self, root=SESSION_ROOT, *, backend_factory=None):
        self.root = Path(root).resolve()
        self.backend_factory = backend_factory
        self.sessions = {}
        self.actions = {}
        self._lock = threading.RLock()

    def _id(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity
        ):
            raise ValueError("无效的会话编号")
        return identity

    def create(self, body):
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象")
        identity = self._id(body.get("session_id", uuid.uuid4().hex))
        if identity == "load":
            raise ValueError("load 为保留的会话路由名称")
        configuration = validate_session_config(
            {key: value for key, value in body.items() if key != "session_id"}
        )
        with self._lock:
            if identity not in self.sessions:
                self.sessions[identity] = create_session(
                    self.root / identity,
                    session_id=identity,
                    backend_factory=self.backend_factory,
                    **configuration,
                )
        return self.payload(identity)

    def load(self, directory):
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ValueError("session_dir 须为实验目录内的绝对路径")
        path = Path(directory).resolve()
        if not path.is_relative_to(self.root.parent):
            raise ValueError("会话须位于当前离线实验目录内")
        with self._lock:
            existing = next(
                (
                    session
                    for session in self.sessions.values()
                    if session.directory.resolve() == path
                ),
                None,
            )
            if existing is not None:
                return self.payload(existing.session_id)
            metadata = json.loads(
                (path / "session_state.json").read_text(encoding="utf-8")
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("schema_version") != 1
            ):
                raise ValueError("会话检查点格式无效")
            identity = self._id(metadata.get("session_id"))
            existing = self.sessions.get(identity)
            if existing is not None:
                if existing.directory.resolve() != path:
                    raise ValueError("相同 session_id 已绑定另一目录")
                return self.payload(identity)
            session = load_session(path, backend_factory=self.backend_factory)
            self.sessions[identity] = session
        return self.payload(identity)

    def get(self, identity):
        identity = self._id(identity)
        with self._lock:
            session = self.sessions.get(identity)
            if session is not None:
                return session
            directory = self.root / identity
            if (
                not (directory / "session_state.json").is_file()
                and self.root == SESSION_ROOT.resolve()
            ):
                directory = self.root.parent / identity
            if not (directory / "session_state.json").is_file():
                raise FileNotFoundError("找不到该会话检查点")
            session = load_session(
                directory, backend_factory=self.backend_factory
            )
            if session.session_id != identity:
                raise ValueError("目录与会话身份不一致")
            self.sessions[identity] = session
            return session

    def payload(self, identity):
        session = self.get(identity)
        state = session.status()
        with self._lock:
            action = (
                dict(self.actions[identity])
                if identity in self.actions
                else None
            )
        return {
            "session_id": identity,
            "state": state,
            "action": action,
            "session_dir": str(session.directory),
            "artifact_base": "/artifacts/"
            + str(session.directory.relative_to(ROOT))
            + "/"
            if session.directory.is_relative_to(ROOT)
            else None,
        }

    def listing(self):
        candidates = list(self.root.glob("*/session_state.json"))
        if self.root == SESSION_ROOT.resolve():
            candidates.extend(
                self.root.parent.glob("p0a_session_*/session_state.json")
            )
        saved = {}
        for path in candidates:
            state = read_json(path, {})
            identity = state.get("session_id")
            if state.get("schema_version") == 1 and identity:
                saved[identity] = state
        with self._lock:
            sessions = list(self.sessions.items())
            actions = {
                identity: dict(action)
                for identity, action in self.actions.items()
            }
        saved.update(
            {identity: session.status() for identity, session in sessions}
        )
        return {
            "sessions": [
                {
                    "session_id": identity,
                    "stage": state["stage"],
                    "workpiece_id": state.get("workpiece_id", "097"),
                    "updated_at": state.get("updated_at"),
                    "action": actions.get(identity),
                }
                for identity, state in sorted(
                    saved.items(),
                    key=lambda item: item[1].get("updated_at", ""),
                    reverse=True,
                )
            ]
        }

    def dispatch(self, identity, method, parameters, *, background=False):
        parameters = validate_session_action(method, parameters)
        session = self.get(identity)
        control = method in {"pause", "safe_retract", "abort"}
        with self._lock:
            current = self.actions.get(identity)
            if not control and current and current["status"] == "running":
                raise SessionStateError(
                    "同一会话已有推进所有者；可查询、暂停或中止"
                )
            if not control:
                self.actions[identity] = {
                    "id": uuid.uuid4().hex,
                    "name": method,
                    "status": "running",
                    "started_at": time.time(),
                    "error": None,
                }
        if control:
            getattr(session, method)(**parameters)
            return self.payload(identity)

        def run_action():
            try:
                getattr(session, method)(**parameters)
            except Exception as error:
                with self._lock:
                    self.actions[identity].update(
                        status="failed",
                        error=str(error),
                        error_type=type(error).__name__,
                        finished_at=time.time(),
                    )
                if not background:
                    raise
            else:
                with self._lock:
                    self.actions[identity].update(
                        status="complete", finished_at=time.time()
                    )

        if background:
            threading.Thread(
                target=run_action, daemon=True, name="session-" + identity
            ).start()
        else:
            run_action()
        return self.payload(identity)


SESSION_STORE = SessionStore()


def session_action(parts, body):
    """Map the closed HTTP intervention surface to SDK primitives."""
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    simple = {
        "step": "step_next",
        "advance": "run_until_pause",
        "pause": "pause",
        "safe-retract": "safe_retract",
        "resume": "resume",
        "abort": "abort",
        "rescan": "rescan_views",
        "replan": "replan_remaining",
        "grasp": "switch_grasp",
    }
    if len(parts) == 1 and parts[0] in simple:
        method = simple[parts[0]]
        return method, validate_session_action(method, body)
    if parts == ["regions", "approve"]:
        return "approve_regions", validate_session_action(
            "approve_regions", body
        )
    if parts == ["regions", "manual"]:
        return "add_manual_region", validate_session_action(
            "add_manual_region", body
        )
    if len(parts) != 3 or not re.fullmatch(r"[1-9][0-9]{0,6}", parts[1]):
        raise FileNotFoundError("会话操作接口不存在")
    kind, identity, action = parts
    routes = {
        ("regions", "mask"): ("mask_false_positive", "region_id"),
        ("segments", "params"): ("set_segment_params", "segment_id"),
        ("segments", "skip"): ("skip_segment", "segment_id"),
        ("segments", "split"): ("split_segment", "segment_id"),
        ("segments", "grasp"): ("switch_grasp", "segment_id"),
    }
    if (kind, action) not in routes:
        raise FileNotFoundError("会话操作接口不存在")
    method, key = routes[kind, action]
    if key in body and body[key] != int(identity):
        raise ValueError("路径编号与请求编号冲突")
    return method, validate_session_action(method, {**body, key: int(identity)})


class Handler(BaseHTTPRequestHandler):
    def json_response(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session_store(self):
        return getattr(self.server, "session_store", SESSION_STORE)

    def _sessions_post(self, parsed):
        identity = None
        try:
            if not self.headers.get("Content-Type", "").startswith(
                "application/json"
            ):
                raise ValueError("仅接受 application/json 请求")
            origin = self.headers.get("Origin")
            if origin and origin not in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }:
                self.json_response({"error": "仅接受本地页面提交"}, 403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("无效的请求长度")
            body = json.loads(self.rfile.read(length))
            store = self._session_store()
            parts = (
                unquote(parsed.path)
                .removeprefix("/api/sessions")
                .strip("/")
                .split("/")
            )
            if parts == [""]:
                self.json_response(store.create(body), 201)
                return
            if parts == ["load"]:
                if not isinstance(body, dict) or set(body) != {"session_dir"}:
                    raise ValueError("载入会话须提供 session_dir")
                self.json_response(store.load(body["session_dir"]))
                return
            identity = parts[0]
            method, parameters = session_action(parts[1:], body)
            background = method in {
                "step_next",
                "run_until_pause",
                "replan_remaining",
                "rescan_views",
                "add_manual_region",
                "resume",
            }
            self.json_response(
                store.dispatch(
                    identity, method, parameters, background=background
                ),
                202 if background else 200,
            )
        except FileNotFoundError as error:
            self.json_response({"error": str(error)}, 404)
        except (SessionSafetyError, SessionStateError) as error:
            value = {"error": str(error)}
            if identity:
                value["session"] = self._session_store().payload(identity)
            self.json_response(value, 409)
        except (ValueError, TypeError) as error:
            self.json_response({"error": str(error)}, 400)

    def _sessions_get(self, parsed):
        store = self._session_store()
        parts = (
            unquote(parsed.path)
            .removeprefix("/api/sessions")
            .strip("/")
            .split("/")
        )
        if parts == [""]:
            self.json_response(store.listing())
            return
        identity = parts[0]
        if len(parts) == 1 or parts[1:] == ["state"]:
            self.json_response(store.payload(identity))
            return
        session = store.get(identity)
        if parts[1:] == ["checkpoint"]:
            self.json_response(session.checkpoint())
        elif parts[1:] == ["segments"]:
            self.json_response({"segments": session.segments()})
        elif parts[1:] == ["interventions"]:
            self.json_response({"interventions": session.interventions()})
        else:
            raise FileNotFoundError("会话查询接口不存在")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions" or parsed.path.startswith(
            "/api/sessions/"
        ):
            self._sessions_post(parsed)
            return
        try:
            if self.path != "/api/run":
                self.json_response({"error": "接口不存在"}, 404)
                return
            if not self.headers.get("Content-Type", "").startswith(
                "application/json"
            ):
                raise ValueError("仅接受 application/json 请求")
            origin = self.headers.get("Origin")
            if origin and origin not in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }:
                self.json_response({"error": "仅接受本地页面提交"}, 403)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("无效的请求长度")
            request = validate(json.loads(self.rfile.read(length)))
            self.json_response(
                {"id": start_job(request), "status": {"stage": "queued"}}, 202
            )
        except RuntimeError as error:
            self.json_response({"error": str(error)}, 409)
        except (ValueError, TypeError, FileNotFoundError) as error:
            self.json_response({"error": str(error)}, 400)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/sessions" or parsed.path.startswith(
                "/api/sessions/"
            ):
                self._sessions_get(parsed)
                return
            if parsed.path == "/api/status":
                self.json_response(
                    payload(parse_qs(parsed.query).get("id", [""])[0])
                )
                return
            if parsed.path == "/api/video":
                self.json_response(
                    compatible_video(parse_qs(parsed.query).get("id", [""])[0])
                )
                return
            if parsed.path == "/api/workpiece-profiles":
                self.json_response(job_profiles())
                return
            if parsed.path == "/api/initial":
                if (
                    parse_qs(parsed.query).get("scene", ["workstation"])[0]
                    != "fr5"
                ):
                    active = next(
                        (
                            run_id
                            for run_id, job in JOBS.items()
                            if job["request"].get("scene") == "workstation"
                            and job["process"].poll() is None
                        ),
                        None,
                    )
                    latest_id = read_json(
                        project.RUNS / "workstation/latest.json", {}
                    ).get("id")
                    for candidate in (active, latest_id):
                        if not candidate:
                            continue
                        try:
                            data = payload(candidate)
                            if candidate == active or (
                                data["status"].get("stage") == "complete"
                                and read_json(
                                    run_directory(candidate)
                                    / "source_hashes.json"
                                )
                                == source_hashes()
                            ):
                                self.json_response(data)
                                return
                        except (ValueError, FileNotFoundError):
                            continue
                    request = validate(
                        {
                            "scene": "workstation",
                            "seed": 7007,
                            "case": "mixed",
                            "execute": False,
                        }
                    )
                    try:
                        run_id = start_job(request)
                        self.json_response(payload(run_id))
                    except RuntimeError as error:
                        self.json_response(
                            {
                                "id": None,
                                "scene": "workstation",
                                "status": {"stage": "empty"},
                                "error": str(error),
                                "detections": [],
                                "paths": [],
                                "files": [],
                            }
                        )
                    return
                for candidate in ("demo_v3", "demo_v2", "demo_v1"):
                    try:
                        data = payload(candidate)
                    except FileNotFoundError:
                        continue
                    if (
                        data["result"]
                        and data["status"].get("stage") == "complete"
                    ):
                        self.json_response(data)
                        return
                self.json_response(
                    {
                        "id": None,
                        "status": {"stage": "empty"},
                        "detections": [],
                        "paths": [],
                        "files": [],
                    }
                )
                return
            path = unquote(parsed.path)
            if path.startswith("/artifacts/"):
                target = (ROOT / path.removeprefix("/artifacts/")).resolve()
                if not any(
                    target.is_relative_to(base.resolve())
                    for base in (project.RESULTS, project.RUNS)
                ) or target.name.startswith("."):
                    self.send_error(404)
                    return
            elif path.startswith("/deliverables/"):
                base = (ROOT / "deliverables").resolve()
                target = (ROOT / path.lstrip("/")).resolve()
                if not target.is_relative_to(base) or target.name.startswith(
                    "."
                ):
                    self.send_error(404)
                    return
            elif path.startswith("/docs/"):
                base = (ROOT / "docs").resolve()
                target = (ROOT / path.lstrip("/")).resolve()
                if not target.is_relative_to(base):
                    self.send_error(404)
                    return
                if target.suffix == ".md" and target.is_file():
                    title = target.name
                    data = (
                        "<!doctype html><meta charset='utf-8'><title>"
                        + html.escape(title)
                        + "</title><style>body{max-width:1000px;margin:48px auto;padding:0 28px;font:16px/1.8 system-ui;color:#18283c;background:#f5f7fa}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#096d60}</style><a href='/'>← 返回演示</a><pre>"
                        + html.escape(target.read_text())
                        + "</pre>"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            else:
                base = (ROOT / "web").resolve()
                target = (base / (path.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(base):
                    self.send_error(404)
                    return
            if not target.is_file():
                self.send_error(404)
                return
            size = target.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                if match:
                    start = int(match.group(1))
                    end = (
                        min(int(match.group(2)), size - 1)
                        if match.group(2)
                        else size - 1
                    )
                if not match or start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with target.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = f.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (ValueError, FileNotFoundError) as error:
            self.json_response({"error": str(error)}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8848)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"Current ESTUN workstation: http://127.0.0.1:{args.port}", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
