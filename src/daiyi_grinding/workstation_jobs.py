"""Versioned job presets for supported CAD, fixture and sensor combinations."""

import hashlib
import json

from . import project
from .workpiece_packages import load_workpiece_package

JOB_FILE = project.ROOT / "config/workstation_jobs.json"
JOB_FIELDS = ("case", "detector", "tool", "views")


def job_profiles():
    return json.loads(JOB_FILE.read_text())


def job_profile(name):
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("工件配置须为已登记的名称")
    row = job_profiles().get(name)
    if row is None:
        raise ValueError("当前工作站未登记该工件和工装组合")
    load_workpiece_package(row["cad_id"])
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {**row, "id": name, "sha256": digest}
