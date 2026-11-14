"""Build an unchanged pinned original Calendar MCP using a separate dependency lock."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.calendar_component import build
from workenv.core import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install the exact npm lock, without lifecycle scripts")
    args = parser.parse_args()
    try:
        result = build(ROOT, install=args.install)
    except Exception as exc:
        save(ROOT / "evidence/mainline/calendar_build.json", {"schema_version": "original-calendar-build-v1", "passed": False,
             "error_type": type(exc).__name__, "runtime_task_activated": False})
        raise
    save(ROOT / "evidence/mainline/calendar_build.json", result)
    print(json.dumps({"passed": True, "build_ref": result["build_ref"], "typecheck_completed": result["typecheck_completed"]}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
