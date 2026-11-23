#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ $# -gt 0 ]]; then
    echo 'Usage: bash scripts/bootstrap.sh' >&2
    exit 2
fi
if [[ -n "${DAIYI_PYTHON:-}" ]]; then
    selected_python="$DAIYI_PYTHON"
elif command -v python3.12 >/dev/null 2>&1; then
    selected_python="python3.12"
else
    selected_python="python3"
fi
if [[ ! -e .venv ]]; then
    "$selected_python" -m venv .venv
fi
if [[ ! -x .venv/bin/python ]]; then
    echo 'Invalid .venv: preserve/rename it, then rerun this script.' >&2
    exit 1
fi
if [[ -f requirements.lock.txt ]]; then
    .venv/bin/python -m pip install -r requirements.lock.txt
    .venv/bin/python -m pip install --no-deps -e .
else
    .venv/bin/python -m pip install -e '.[dev]'
fi
.venv/bin/python -c 'import sys, mujoco; print(sys.version); print("MuJoCo", mujoco.__version__)'
echo 'Next: bash scripts/verify.sh'
