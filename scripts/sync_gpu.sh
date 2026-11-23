#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ -f .gpu.env ]]; then
    source .gpu.env
fi
: "${DAIYI_GPU_SSH:?Set DAIYI_GPU_SSH or copy gpu.env.example to .gpu.env}"
: "${DAIYI_GPU_PROJECT:?Set DAIYI_GPU_PROJECT in .gpu.env}"
: "${DAIYI_GPU_RUNS:?Set DAIYI_GPU_RUNS in .gpu.env}"
command -v rsync >/dev/null
operation="${1:-}"
sync_options=(-av --protect-args --mkpath)
if [[ "${DAIYI_SYNC_DRY_RUN:-0}" == 1 ]]; then
    sync_options+=(--dry-run)
fi
if [[ "$operation" == push-code ]]; then
    rsync "${sync_options[@]}" --include='*.py' --exclude='*' \
        src/daiyi_grinding/ "${DAIYI_GPU_SSH}:${DAIYI_GPU_PROJECT}/src/daiyi_grinding/"
    rsync "${sync_options[@]}" pyproject.toml requirements.lock.txt README.md HANDOFF.md \
        "${DAIYI_GPU_SSH}:${DAIYI_GPU_PROJECT}/"
    exit
fi
run_id="${2:-}"
if [[ "$operation" != push-input && "$operation" != pull-output ]] || [[ ! "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$ ]]; then
    echo 'Usage: bash scripts/sync_gpu.sh push-code | {push-input|pull-output} RUN_ID' >&2
    exit 2
fi
local_run="runs/vla/${run_id}"
remote_run="${DAIYI_GPU_SSH}:${DAIYI_GPU_RUNS}/${run_id}/"
if [[ "$operation" == push-input ]]; then
    for name in input.npz input_manifest.json baseline.json reference_points.npz; do
        [[ -f "$local_run/$name" ]] || { echo "Missing prepared input: $local_run/$name" >&2; exit 1; }
    done
    rsync "${sync_options[@]}" \
        --include='input.npz' --include='input_manifest.json' --include='baseline.json' \
        --include='reference_points.npz' --include='*.png' --exclude='*' \
        "$local_run/" "$remote_run"
else
    mkdir -p "$local_run"
    rsync "${sync_options[@]}" --exclude='input.npz' --exclude='input_manifest.json' \
        --exclude='baseline.json' --exclude='reference_points.npz' \
        --include='*.json' --include='*.npz' \
        --include='*.png' --include='*.log' --include='*.txt' --exclude='*' \
        "$remote_run" "$local_run/"
fi
