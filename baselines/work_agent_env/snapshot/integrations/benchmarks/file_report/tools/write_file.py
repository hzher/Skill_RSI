"""A preinstalled command-tool adapter: JSON stdin -> workspace file -> JSON stdout."""
import json
from pathlib import Path
import sys

arguments = json.load(sys.stdin)
workspace = Path("/workspace").resolve()
path = (workspace / arguments["path"]).resolve()
if not path.is_relative_to(workspace):
    raise ValueError("Output escapes workspace")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(arguments["content"], encoding="utf-8")
print(json.dumps({"written": str(path)}))
