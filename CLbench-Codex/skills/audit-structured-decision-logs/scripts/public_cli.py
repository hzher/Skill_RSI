"""Read one public audit packet from stdin and write the nine-field answer to stdout."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

f = Path(__file__).with_name("audit.py")
if hashlib.sha256(f.read_bytes()).hexdigest() != "9be044403d79159c7b79f1177a3ff48aab961dcc4701a1af9df93cdd80941171":
    raise ValueError("Audit module does not match the frozen version")
api = runpy.run_path(str(f))
try:
    print(json.dumps(api["audit_packet"](json.load(sys.stdin)), allow_nan=False))
except api["EvidenceGap"] as error:
    sys.stderr.write("evidence_gap: " + str(error) + "\n")
    sys.exit(2)
