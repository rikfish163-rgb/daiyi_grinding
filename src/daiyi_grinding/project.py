"""Paths for the editable simulation project and its saved evidence."""

from pathlib import Path

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
RESULTS = ROOT / "results"
RUNS = ROOT / "runs"
