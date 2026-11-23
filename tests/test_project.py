"""Integration checks for editable imports, resources and planning state."""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.request import Request, urlopen

from daiyi_grinding import calibration, project, run, serve, vla_experiment


class ProjectIntegrationTests(unittest.TestCase):
    def test_imports_and_resources_from_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from daiyi_grinding import assets, calibration, project, sim, vla_experiment; import sys; assert 'torch' not in sys.modules; assert (project.RESULTS / 'calibration/calibration.json').is_file(); assert len(list(assets.ROOT.glob('*.STL'))) == 7",
                ],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
            )

    def test_installed_cli_entry_points_from_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "daiyi-run",
                "daiyi-benchmark",
                "daiyi-calibrate",
                "daiyi-vla",
                "daiyi-serve",
                "daiyi-workstation",
                "daiyi-workstation-benchmark",
                "daiyi-defect-study",
                "daiyi-cad-defects",
                "daiyi-casting-synthesis",
                "daiyi-cad-resolution",
                "daiyi-cad-refinement",
            ):
                with self.subTest(command=name):
                    result = subprocess.run(
                        [str(Path(sys.executable).parent / name), "--help"],
                        cwd=directory,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.assertIn("usage:", result.stdout)

    def test_original_runner_exposes_selected_observation_method(self):
        arguments = run.parser().parse_args(
            ["--method", "robust_local", "--case", "thin", "--perception-only"]
        )
        self.assertEqual(arguments.method, "robust_local")
        self.assertEqual(arguments.case, "thin")
        self.assertTrue(arguments.perception_only)

    def test_existing_outputs_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            evidence = out / "result.json"
            evidence.write_text('{"previous": true}')
            for operation in (run.run, vla_experiment.prepare):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises((ValueError, FileExistsError)):
                        operation(SimpleNamespace(output=out))
            with self.assertRaises(ValueError):
                calibration.run(out)
            self.assertEqual(evidence.read_text(), '{"previous": true}')
            self.assertEqual([p.name for p in out.iterdir()], ["result.json"])

    def test_http_baseline_resources_and_video_seek(self):
        server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        address = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(
                address + "/api/initial?scene=fr5", timeout=10
            ) as response:
                initial = json.load(response)
            self.assertEqual(initial["id"], "demo_v3")
            self.assertEqual(len(initial["paths"]), 14)
            for path in (
                "/",
                "/app.js",
                "/style.css",
                "/docs/fr5_real_robot_plan.md",
                "/artifacts/results/vla/shadow_v1/audit.json",
            ):
                with self.subTest(path=path):
                    with urlopen(address + path, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(response.read())
            request = Request(
                address + "/artifacts/results/demo_v3/demo.mp4",
                headers={"Range": "bytes=0-1023"},
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(len(response.read()), 1024)
        finally:
            server.shutdown()
            worker.join(timeout=10)
            server.server_close()

    def test_robot_parameters_invalidate_prior_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            shutil.copytree(
                project.SOURCE,
                current,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            run_id = "1" * 32
            out = root / "runs/interactive" / run_id
            shutil.copytree(current, out / "source_snapshot")
            (out / "status.json").write_text('{"stage": "complete"}')
            (out / "result.json").write_text(
                '{"seed": 7, "case": "mixed", "roi": null}'
            )
            (out / "detections.json").write_text('[{"id": 1, "kind": "seam"}]')
            request = {
                "seed": 7,
                "case": "mixed",
                "roi": None,
                "ids": [1],
                "execute": True,
                "source_id": run_id,
            }
            with (
                mock.patch.object(project, "SOURCE", current),
                mock.patch.object(project, "RUNS", root / "runs"),
                mock.patch.object(serve, "ROOT", root),
            ):
                self.assertTrue(serve.validate(request)["execute"])
                model = current / "robot_model.py"
                model.write_text(model.read_text().replace("1.6185", "1.8"))
                with self.assertRaisesRegex(ValueError, "计算模型已更新"):
                    serve.validate(request)


if __name__ == "__main__":
    unittest.main()
