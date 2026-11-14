#!/usr/bin/env bash
# Project-local training interpreter and CUDA forward-compatibility libraries.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
compat_dir="$project_root/.runtime/generator-cuda/compat"
if [[ -d "$compat_dir" ]]; then
  export LD_LIBRARY_PATH="$compat_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export TOKENIZERS_PARALLELISM=false
exec "$project_root/.runtime/generator-venv/bin/python" "$@"
