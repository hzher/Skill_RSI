"""Prepare the owned portable Linux runtime; do not install a host system feature."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

from nemo_vm import ROOT, VM


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch and verify upstream VM assets if missing")
    args = parser.parse_args()
    if shutil.disk_usage(ROOT).free < 12 * 1024**3:
        raise RuntimeError("Reserve at least 12 GiB free disk space for the bounded VM overlay and packages")
    if args.download:
        subprocess.run([sys.executable, str(ROOT / "scripts/download_nemo_vm_assets.py")], check=True)
    # Administrative extraction writes MSI contents to the owned target; it does not install 7-Zip.
    extractor = VM / "extractor/Files/7-Zip/7z.exe"
    if not extractor.is_file():
        msi = VM / "assets/7zip.msi"
        if not msi.is_file(): raise RuntimeError("Download VM assets first")
        completed = subprocess.run(["msiexec.exe", "/a", str(msi), "/qn", f"TARGETDIR={VM / 'extractor'}",
                                    "/l*v", str(VM / "assets/msi-extract.log")], timeout=90)
        if completed.returncode != 0:
            raise RuntimeError("Owned MSI extraction failed; see msi-extract.log")
    qemu = VM / "qemu/qemu-system-x86_64.exe"
    if not qemu.is_file():
        subprocess.run([str(extractor), "x", str(VM / "assets/qemu.exe"), "-o" + str(VM / "qemu"),
                        "-y", "-bsp0", "-bso0"], check=True, timeout=120)
    subprocess.run([str(qemu), "--version"], check=True, timeout=20)
    subprocess.run([sys.executable, str(ROOT / "scripts/nemo_vm.py"), "init"], check=True)
    print("Prepared; use nemo_vm.py start, then provision_nemo_vm.py and check_nemo_install.py")
    return 0


if __name__ == "__main__": raise SystemExit(main())
