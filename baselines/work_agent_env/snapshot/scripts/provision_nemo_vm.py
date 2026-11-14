"""Transfer pinned NeMo source bytes and install the real Linux CPU package."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

from nemo_vm import ROOT, VM, ssh_arguments

PIN = "98de01c6168062fb746d45f8cd56b38b5580555b"
UV_VERSION = "0.11.21"


def scp(source, destination):
    arguments = ssh_arguments()
    # scp uses uppercase -P; the other connection options are shared with ssh.
    arguments[0] = "scp"
    arguments[arguments.index("-p")] = "-P"
    subprocess.run(arguments[:-1] + [str(source), "workenv@127.0.0.1:" + destination], check=True, timeout=240)


def main():
    upstream = ROOT / ".upstream/nemo-gym"
    head = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=no"], text=True).strip()
    if head != PIN or dirty:
        raise RuntimeError("NeMo source pin or tracked bytes differ")
    uv_asset = VM / "assets/uv-linux.tar.gz"
    if not uv_asset.exists():
        urllib.request.urlretrieve(f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz", uv_asset)
    archive = VM / "assets/nemo-source.tar.gz"
    names = subprocess.check_output(["git", "-C", str(upstream), "ls-files", "-z"]).decode().split("\0")
    files = {}
    with tarfile.open(archive, "w:gz") as target:
        for name in names:
            path = upstream / name
            if name and path.is_file() and not path.is_symlink():
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                target.add(path, arcname="nemo-source/" + name, recursive=False)
    manifest = {"commit": head, "files": files, "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "uv_version": UV_VERSION, "uv_archive_sha256": hashlib.sha256(uv_asset.read_bytes()).hexdigest()}
    manifest_path = VM / "assets/source-transfer.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for path in (archive, uv_asset, manifest_path):
        scp(path, "/home/workenv/" + path.name)
    scp(ROOT / "configs/nemo_linux_runtime.requirements.txt", "/home/workenv/nemo-linux-requirements.txt")
    script = r'''set -eu
cd /home/workenv
tar -xzf uv-linux.tar.gz
tar -xzf nemo-source.tar.gz
mkdir -p nemo-source/cache
./uv-x86_64-unknown-linux-gnu/uv python install 3.13.14
if [ ! -x nemo-venv/bin/python ]; then ./uv-x86_64-unknown-linux-gnu/uv venv --python 3.13.14 nemo-venv; fi
# The pinned upstream imports requests but does not declare it in core dependencies.
./uv-x86_64-unknown-linux-gnu/uv pip install --python nemo-venv/bin/python -e ./nemo-source -r nemo-linux-requirements.txt
./uv-x86_64-unknown-linux-gnu/uv pip freeze --python nemo-venv/bin/python > nemo-runtime.freeze.txt
nemo-venv/bin/python - <<'PY'
import hashlib,json,pathlib,platform,importlib.metadata
import nemo_gym
from nemo_gym.base_resources_server import SimpleResourcesServer
root=pathlib.Path('/home/workenv')
manifest=json.loads((root/'source-transfer.json').read_text())
assert all(hashlib.sha256((root/'nemo-source'/p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())
result={'nemo_gym':importlib.metadata.version('nemo-gym'),'python':platform.python_version(),'system':platform.system(),'source_commit':manifest['commit'],'source_files_verified':len(manifest['files']),'real_model_calls':0,'training_updates':0}
(root/'nemo-install.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
PY
'''
    result = subprocess.run(ssh_arguments() + ["sh -s"], input=script.encode(), timeout=1800)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
