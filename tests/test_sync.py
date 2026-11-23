"""Verify source and run exchange use separate remote destinations."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding import project


class SyncScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(
            project.ROOT / "scripts/sync_gpu.sh", scripts / "sync_gpu.sh"
        )
        (self.root / ".gpu.env").write_text(
            "DAIYI_GPU_SSH=4090\nDAIYI_GPU_PROJECT=/home/hts/demo1/packages/daiyi_grinding\nDAIYI_GPU_RUNS=/home/data4/hts/demo1/fr5-learning/grinding/runs\n"
        )
        binary = self.root / "bin"
        binary.mkdir()
        rsync = binary / "rsync"
        rsync.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\nwith open(os.environ['SYNC_CALLS'], 'a') as stream:\n    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        rsync.chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.environment = {
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "SYNC_CALLS": str(self.calls),
        }

    def invoke(self, *arguments):
        subprocess.run(
            ["bash", str(self.root / "scripts/sync_gpu.sh"), *arguments],
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return [
            json.loads(line) for line in self.calls.read_text().splitlines()
        ]

    def test_push_code_preserves_workspace_and_model_paths(self):
        calls = self.invoke("push-code")
        self.assertEqual(len(calls), 2)
        self.assertIn("src/daiyi_grinding/", calls[0])
        self.assertEqual(
            calls[0][-1],
            "4090:/home/hts/demo1/packages/daiyi_grinding/src/daiyi_grinding/",
        )
        for arguments in calls:
            self.assertNotIn("--delete", arguments)
            self.assertFalse(
                any(
                    "hy_vla_repro" in value or ".venv" in value
                    for value in arguments
                )
            )

    def test_run_exchange_preserves_prepared_inputs(self):
        run_id = "host_001"
        local = self.root / "runs/vla" / run_id
        local.mkdir(parents=True)
        for name in (
            "input.npz",
            "input_manifest.json",
            "baseline.json",
            "reference_points.npz",
        ):
            (local / name).write_text("prepared input")
        upload = self.invoke("push-input", run_id)[0]
        self.assertEqual(
            upload[-1],
            "4090:/home/data4/hts/demo1/fr5-learning/grinding/runs/host_001/",
        )
        download = self.invoke("pull-output", run_id)[1]
        for name in (
            "input.npz",
            "input_manifest.json",
            "baseline.json",
            "reference_points.npz",
        ):
            self.assertIn("--exclude=" + name, download)
            self.assertEqual((local / name).read_text(), "prepared input")
        self.assertNotIn("--delete", download)


if __name__ == "__main__":
    unittest.main()
