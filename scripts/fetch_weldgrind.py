"""Fetch the precise source version used by the workstation's CPU filter."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "0e110bf14952cf89ef127342d6a10932d151fa62"
DESTINATION = ROOT / "external/WeldGrind-V1"


def main():
    DESTINATION.parent.mkdir(exist_ok=True)
    if not DESTINATION.exists():
        subprocess.run(
            [
                "git", "clone", "--depth", "1",
                "https://github.com/CharlesH777/WeldGrind-V1.git",
                str(DESTINATION),
            ],
            check=True,
        )
    actual = subprocess.check_output(
        ["git", "-C", str(DESTINATION), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != COMMIT:
        subprocess.run(
            ["git", "-C", str(DESTINATION), "fetch", "origin", COMMIT],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(DESTINATION), "checkout", "--detach", COMMIT],
            check=True,
        )
    print(f"WeldGrind source ready: {DESTINATION} @ {COMMIT}")


if __name__ == "__main__":
    main()
