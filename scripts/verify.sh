#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
export MUJOCO_GL="${MUJOCO_GL:-egl}"
.venv/bin/python -m unittest discover -s tests -v
mkdir -p runs
verification_dir="$(mktemp -d runs/verification-XXXXXXXX)"
.venv/bin/daiyi-run --seed 7 --case mixed --perception-only \
    --output "$verification_dir/mixed" > "$verification_dir/mixed.log"
.venv/bin/daiyi-run --seed 7 --case normal --perception-only \
    --output "$verification_dir/normal" > "$verification_dir/normal.log"
.venv/bin/python - "$verification_dir" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
for case, expected in [('mixed', 14), ('normal', 0)]:
    status = json.loads((p/case/'status.json').read_text())
    result = json.loads((p/case/'result.json').read_text())
    assert status['stage'] == 'complete', status
    assert result['path_count'] == expected, result
    assert result['perception']['normal_structure_selected_pixels'] == 0, result
    print(case, 'path_count =', result['path_count'])
print('Perception and entry-point verification passed:', p)
PY
