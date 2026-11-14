"""Dataset-owned evaluator, mounted only in the scoring container."""
import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
path = Path(request["context"]["workspace"]) / "result.txt"
print(json.dumps({"pass": path.is_file() and path.read_bytes() == b"hello\n"}))
