"""Short contact sensitivity study; no grinding/removal or force control claim."""

import argparse
import shutil
from pathlib import Path

import numpy as np

from . import project
from .calibration import load_inspection_calibration
from .execution import execute, make_commands, preflight
from .geometry import generate
from .perception import detect, observe_noise
from .planning import plan
from .run import write_json
from .sim import Simulation


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default=str(project.RUNS / "contact_probe_new"))
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    results = []
    for index, (offset, solref) in enumerate(
        [(0.3, 0.015), (0.0, 0.015), (-0.3, 0.015), (-0.3, 0.03)]
    ):
        sim = Simulation(generate(7, "mixed"))
        folder = out / f"trial_{index}"
        folder.mkdir()
        try:
            sim.model.geom_solref[:, 0] = solref
            K, T = load_inspection_calibration(
                project.ROOT / "results/calibration/calibration.json",
                camera_position=sim.data.cam_xpos[
                    sim.model.camera("inspection").id
                ],
            )
            rgb, depth = observe_noise(sim.render(), sim.render(depth=True), 7)
            dets, aux = detect(rgb, depth, K, T)
            paths = plan(dets, aux, clearance_mm=offset)
            path = next(p for p in paths if p["kind"] == "seam")
            path["xyz"] = path["xyz"][:14]
            n = np.array(path["normal"])
            path["retract"] = (np.array(path["xyz"][-1]) + n * 0.025).tolist()
            commands, planning = make_commands(sim, [path])
            check = preflight(sim, commands)
            result = dict(
                nominal_clearance_mm=offset,
                contact_solref_time_s=solref,
                planning=planning,
                preflight=check,
            )
            if check["passed"]:
                result["execution"] = execute(sim, commands, folder)
            write_json(folder / "result.json", result)
            results.append(result)
            print(
                f"trial {index}: offset={offset}, contacts={result.get('execution', {}).get('abrasive_contact_steps')}",
                flush=True,
            )
        finally:
            sim.close()
    write_json(
        out / "summary.json",
        dict(
            simulation_only=True,
            force_control_validated=False,
            material_removal_simulated=False,
            scope="fixed position servo follows non-eroding surface; nominal TCP offsets and soft-contact time constants are experimental assumptions",
            trials=results,
        ),
    )
    shutil.copytree(
        project.SOURCE,
        out / "source_snapshot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


if __name__ == "__main__":
    main()
