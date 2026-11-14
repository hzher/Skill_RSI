"""Stage unchanged original email sources with an explicitly separate dependency install."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.email_build import build
from workenv.core import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    result = build(ROOT, install=args.install)
    save(ROOT / "evidence/mainline/email_build.json", result)
    print(json.dumps({"passed": True, "build_ref": result["build_ref"], "versions": result["versions"],
                      "runtime_task_activated": False, "external_messages_sent": 0}))


if __name__ == "__main__": main()
