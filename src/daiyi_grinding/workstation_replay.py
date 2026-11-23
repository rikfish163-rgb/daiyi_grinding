"""Lossless browser export of recorded feedback, without rerunning simulation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


class ReplayRecorder:
    """Keep exact feedback while sharing immutable cumulative stock states."""

    def __init__(self):
        self.frames = []
        self.mesh_states = [[]]
        self.meshes = {}

    def append(self, original):
        changed = False
        for update in original.get("mesh_updates", []):
            previous = self.meshes.get(update["id"])
            if previous is update:
                continue
            if previous != update:
                self.meshes[update["id"]] = update
                changed = True
        if changed:
            self.mesh_states.append(
                [self.meshes[key] for key in sorted(self.meshes)]
            )
        self.frames.append(
            {
                **{
                    key: value
                    for key, value in original.items()
                    if key != "mesh_updates"
                },
                "mesh_revision": len(self.mesh_states) - 1,
            }
        )

    def document(self, **metadata):
        return {
            **metadata,
            "schema_version": 2,
            "frames": self.frames,
            "mesh_states": self.mesh_states,
            "visualization_only": True,
            "body_poses_quantized": False,
            "frames_dropped": 0,
            "stock_snapshots": len(self.mesh_states) - 1,
            "encoding": "exact feedback frames with cumulative stock snapshot references",
        }


def compact_frames(replay):
    """Reference complete stock snapshots instead of repeating them per frame.

    Joint feedback, body poses, timestamps and all other frame fields remain
    exact. Every frame references its cumulative stock state, so a browser may
    skip display frames without losing an intervening material update.
    """
    if replay.get("schema_version") == 2 and "mesh_states" in replay:
        return dict(replay)
    recording = ReplayRecorder()
    for original in replay["frames"]:
        recording.append(original)
    return recording.document(
        **{key: value for key, value in replay.items() if key != "frames"}
    )


def export_compact_replay(directory):
    directory = Path(directory)
    original = directory / "replay.json"
    target = directory / "replay_compact.json"
    if not original.exists():
        return None
    stat = original.stat()
    with original.open() as file:
        compact = compact_frames(json.load(file))
    digest = hashlib.sha256()
    with original.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    compact["original_file"] = original.name
    compact["original_bytes"] = stat.st_size
    compact["original_sha256"] = digest.hexdigest()
    compact["exporter_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    temporary = target.with_suffix(".json.tmp")
    with temporary.open("w") as file:
        json.dump(
            compact,
            file,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        file.write("\n")
    temporary.replace(target)
    return {
        "frames": len(compact["frames"]),
        "stock_snapshots": compact["stock_snapshots"],
        "original_bytes": stat.st_size,
        "compact_bytes": target.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(export_compact_replay(args.directory), indent=2))


if __name__ == "__main__":
    main()
