#!/usr/bin/env bash
# Source this file from a shell: source scripts/activate_linux.sh
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Use: source scripts/activate_linux.sh" >&2
    exit 2
fi
workagent_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$workagent_root/.venv/bin/activate"
export PATH="$workagent_root/.runtime/node-v22.14.0-linux-x64/bin:$workagent_root/.runtime/pgsql/bin:$workagent_root/.runtime/uv-bootstrap/bin:$PATH"
export LD_LIBRARY_PATH="$workagent_root/.runtime/pgsql/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
unset workagent_root
