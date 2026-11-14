#!/usr/bin/env bash
# Run through codex/run_and_log.sh. Never replace the native training runtime.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
uv_bin="$project_root/.runtime/uv-bootstrap/bin/uv"
content_python="$project_root/.runtime/skillos-content-venv/bin/python"
if [[ ! -x "$content_python" ]]; then
  "$uv_bin" venv --python .runtime/generator-venv/bin/python .runtime/skillos-content-venv
fi
"$content_python" - <<'PY'
from pathlib import Path
import sysconfig
site=Path(sysconfig.get_paths()['purelib'])
(site/'generator-runtime.pth').write_text(str(Path.cwd()/'.runtime/generator-venv/lib/python3.11/site-packages')+'\n')
PY
"$uv_bin" pip install --python "$content_python" --no-deps -r configs/skillos-content.requirements.in
"$content_python" - <<'PY'
import torch,transformers
from transformers import AutoModelForImageTextToText
if not torch.__version__.startswith('2.8.0'):
    raise RuntimeError('Unexpected shared torch runtime')
print('ContentJudge imports verified:',torch.__version__,transformers.__version__)
PY
