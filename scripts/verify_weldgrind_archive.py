#!/usr/bin/env python3
"""Verify the pinned public archive, safely extract, then run its reviewed audit."""

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_SHA256 = (
    "30ff4e48f1ebde8a3b74845403ea06df5822def7a2ee01607811b71d888e228a"
)
ARCHIVE_ROOT = "weldgrind_dataset_20261110_public_v1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    folder = args.directory.resolve()
    archive = folder / f"{ARCHIVE_ROOT}.zip"
    digest = hashlib.sha256()
    print("Checking full archive SHA256", flush=True)
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != EXPECTED_SHA256:
        raise ValueError(f"Archive hash mismatch: {actual}")
    print(
        "Archive hash passed; extracting with path and symlink checks",
        flush=True,
    )
    destination = folder / ARCHIVE_ROOT
    staging = folder / f".{ARCHIVE_ROOT}.extracting"
    if not destination.exists():
        staging.mkdir(exist_ok=False)
        with zipfile.ZipFile(archive) as package:
            seen = set()
            for index, item in enumerate(package.infolist(), 1):
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != ARCHIVE_ROOT
                    or item.filename in seen
                    or stat.S_ISLNK(item.external_attr >> 16)
                ):
                    raise ValueError(f"Invalid archive member: {item.filename}")
                seen.add(item.filename)
                target = staging.joinpath(*path.parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # ZipExtFile verifies the member CRC during full extraction.
                    with (
                        package.open(item) as source,
                        target.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output, 1024 * 1024)
                if index % 5000 == 0:
                    print(f"Extracted {index} members", flush=True)
        (staging / ARCHIVE_ROOT).rename(destination)
        staging.rmdir()
    print(
        "Running original standard-library file/provenance/split verification",
        flush=True,
    )
    verification = subprocess.run(
        [
            sys.executable,
            str(destination / "tools/verify_dataset.py"),
            "--root",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(verification.stdout)
    receipt.update(
        archive_sha256=actual,
        archive_bytes=archive.stat().st_size,
        safe_extraction=True,
        zip_member_crc_verified=True,
        checked_date="2026-11-21",
    )
    (folder / "local_verification.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
