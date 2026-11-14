"""Check actual Linux imports and transferred upstream bytes; record host evidence."""
import json
from pathlib import Path
import subprocess
import sys

from nemo_vm import ROOT, ssh_arguments


def main():
    code = '''import faulthandler,hashlib,importlib.metadata,json,pathlib,platform
faulthandler.enable()
faulthandler.dump_traceback_later(45,repeat=True)
import nemo_gym
from nemo_gym.base_resources_server import SimpleResourcesServer
from nemo_gym.base_responses_api_agent import SimpleResponsesAPIAgent
from nemo_gym.base_environment_server import BaseEnvironmentServer
root=pathlib.Path('/home/workenv')
manifest=json.loads((root/'source-transfer.json').read_text())
assert all(hashlib.sha256((root/'nemo-source'/p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())
result={'nemo_gym':importlib.metadata.version('nemo-gym'),'python':platform.python_version(),'system':platform.system(),'source_commit':manifest['commit'],'source_files_verified':len(manifest['files']),'real_model_calls':0,'training_updates':0,'packages':sorted([{'name':d.metadata['Name'],'version':d.version} for d in importlib.metadata.distributions()],key=lambda d:d['name'].lower())}
(root/'nemo-install.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
faulthandler.cancel_dump_traceback_later()
'''
    if sys.platform == "linux":
        source = json.loads((ROOT / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["nemo_gym"]
        checkout = ROOT / source["checkout"]
        actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=10).strip()
        if actual != source["commit"] or subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], timeout=10).strip():
            raise RuntimeError("Native NeMo checkout is not clean at the pinned commit")
        names = subprocess.check_output(["git", "-C", str(checkout), "ls-files", "-z", "nemo_gym", "pyproject.toml"], timeout=10).decode().split("\0")
        files = [name for name in names if name]
        code = code.replace("root=pathlib.Path('/home/workenv')\nmanifest=json.loads((root/'source-transfer.json').read_text())\nassert all(hashlib.sha256((root/'nemo-source'/p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())",
            "root=pathlib.Path(" + repr(str(ROOT / ".runtime")) + ")\n" +
            "assert pathlib.Path(nemo_gym.__file__).resolve().is_relative_to(pathlib.Path(" + repr(str(checkout)) + "))\n" +
            "manifest=" + repr({"commit": actual, "files": files}))
        command = [str(ROOT / ".runtime/nemo-venv/bin/python"), "-X", "faulthandler", "-"]
    else:
        command = ssh_arguments() + ["/home/workenv/nemo-venv/bin/python -X faulthandler -"]
    completed = subprocess.run(command,
                               input=code.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if completed.returncode:
        print(completed.stderr.decode("utf-8", "replace"))
        return completed.returncode
    result = json.loads(completed.stdout)
    result["execution_host"] = "native_linux" if sys.platform == "linux" else "windows_managed_linux_vm"
    target = ROOT / "evidence/mainline/nemo_linux_install.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "packages"}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
