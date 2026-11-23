"""Fetch the seven official FAIRINO FR5 visual meshes for local use."""

from pathlib import Path
from urllib.request import urlopen

from . import project

REV = "fcf0c7f0d60d949d8a9a4238f929a44d07f60379"
ROOT = project.ROOT / "assets" / "fairino5_v6"
BASE = f"https://raw.githubusercontent.com/FAIR-INNOVATION/frcobot_ros2/{REV}/fairino_description/meshes/fairino5_v6"
NAMES = (
    "base_link",
    "shoulder_link",
    "upperarm_link",
    "forearm_link",
    "wrist1_link",
    "wrist2_link",
    "wrist3_link",
)


def fetch() -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        path = ROOT / f"{name}.STL"
        if not path.exists():
            print(f"Downloading {path.name}...")
            with urlopen(f"{BASE}/{path.name}", timeout=45) as response:
                path.write_bytes(response.read())
    return ROOT
