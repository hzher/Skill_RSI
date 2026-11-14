"""Build a pinned, project-local copy of one upstream MCP component."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from record_command import ROOT, decode, now

source = ROOT / ".upstream" / "toolathlon-gym" / "local_servers" / "woocommerce-mcp"
target = ROOT / ".runtime" / "woocommerce-build"
if not target.resolve().is_relative_to(ROOT.resolve()):
    raise RuntimeError("Build target escapes workspace")
target.mkdir(exist_ok=True, parents=True)
owner = target / "build_owner.json"
if owner.exists() and json.loads(owner.read_text())["workspace"] != str(ROOT):
    raise RuntimeError("Build directory ownership mismatch")
if not owner.exists() and any(target.iterdir()):
    raise RuntimeError("Refusing to overwrite an unowned nonempty build directory")
owner.write_text(json.dumps({"workspace":str(ROOT),"created_at":now()}),encoding="utf-8")
shutil.copytree(source / "src", target / "src", dirs_exist_ok=True)
shutil.copyfile(source / "tsconfig.json", target / "tsconfig.json")
shutil.copyfile(ROOT / "configs" / "woocommerce.package.json", target / "package.json")
shutil.copyfile(ROOT / "configs" / "woocommerce.package-lock.json", target / "package-lock.json")
license_file = source / "LICENSE"
if license_file.exists():
    shutil.copyfile(license_file, target / "LICENSE")
for args in (("ci", "--ignore-scripts", "--no-audit", "--no-fund"), ("run", "build")):
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm unavailable")
    p = subprocess.run([npm, *args], cwd=target, capture_output=True, timeout=180)
    print(decode(p.stdout),end="")
    print(decode(p.stderr),end="",file=sys.stderr)
    if p.returncode:
        raise RuntimeError(f"npm {args[0]} returned {p.returncode}")
record = {"built_at":now(),"source":".upstream/toolathlon-gym/local_servers/woocommerce-mcp",
          "build":".runtime/woocommerce-build", "source_file_sha256":{
              str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((source/'src').rglob('*')) if p.is_file()},
          "dependency_lock_sha256":hashlib.sha256((target/'package-lock.json').read_bytes()).hexdigest()}
(ROOT/'evidence/woocommerce_build.json').write_text(json.dumps(record,indent=2)+'\n',encoding="utf-8")
print("Native WooCommerce build ready:",target)
