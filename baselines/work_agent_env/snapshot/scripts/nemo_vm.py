"""Own a headless, loopback-only Linux VM for the unmodified NeMo Gym runtime.

Run with .runtime/mainline-tools/Scripts/python.exe (pycdlib is required for init).
No host feature installation, elevation, shared host filesystem, or model calls.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / ".runtime/nemo-vm"
SSH_PORT = 22222
HTTP_PORT = 18700
QMP_PORT = 18702


def ssh_arguments():
    return ["ssh", "-i", str(VM / "id_ed25519"), "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-o", f"UserKnownHostsFile={VM / 'known_hosts'}",
            "workenv@127.0.0.1"]


def init():
    import pycdlib
    VM.mkdir(parents=True, exist_ok=True)
    key = VM / "id_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    # Windows ssh-keygen comments may use the local code page; the key fields are ASCII.
    public = b" ".join(key.with_suffix(".pub").read_bytes().split()[:2]).decode("ascii")
    overlay = VM / "workenv.qcow2"
    if not overlay.exists():
        subprocess.run([str(VM / "qemu/qemu-img.exe"), "create", "-f", "qcow2",
                        "-F", "qcow2", "-b", str(VM / "assets/debian-13-genericcloud-amd64.qcow2"),
                        str(overlay), "12G"], check=True)
    iso_path = VM / "seed.iso"
    if not iso_path.exists():
        user = ("#cloud-config\nusers:\n  - name: workenv\n    groups: [sudo]\n"
                "    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    lock_passwd: true\n"
                "    ssh_authorized_keys:\n      - " + public + "\n"
                "ssh_pwauth: false\ndisable_root: true\n"
                "growpart:\n  mode: auto\n  devices: ['/']\nresize_rootfs: true\n")
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, vol_ident="cidata")
        for iso_name, joliet, data in [
            ("USER_DAT.;1", "user-data", user),
            ("META_DAT.;1", "meta-data", "instance-id: workagent-nemo-v2\nlocal-hostname: workagent-nemo\n"),
        ]:
            raw = data.encode("utf-8")
            iso.add_fp(io.BytesIO(raw), len(raw), iso_path="/" + iso_name, joliet_path="/" + joliet)
        iso.write(str(iso_path))
        iso.close()
    print("VM disk and cloud-init prepared; private key remains in ignored .runtime.")


def start():
    record = VM / "process.json"
    # Never create a second process against an existing disk or steal an occupied port.
    for port in (SSH_PORT, HTTP_PORT, QMP_PORT):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} already occupied; inspect the existing runtime") from exc
    command = [str(VM / "qemu/qemu-system-x86_64.exe"), "-name", "workagent-nemo-v2",
               "-machine", "q35", "-accel", "tcg,thread=multi", "-cpu", "max", "-m", "1024", "-smp", "1",
               "-display", "none", "-monitor", "none", "-serial", f"file:{VM / 'serial.log'}",
               "-qmp", f"tcp:127.0.0.1:{QMP_PORT},server=on,wait=off",
               "-drive", f"file={VM / 'workenv.qcow2'},if=virtio,format=qcow2",
               "-drive", f"file={VM / 'seed.iso'},media=cdrom,readonly=on",
               "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:{HTTP_PORT}-:{HTTP_PORT}",
               "-device", "virtio-net-pci,netdev=n0", "-device", "virtio-rng-pci"]
    with (VM / "qemu.stdout.log").open("ab") as out, (VM / "qemu.stderr.log").open("ab") as err:
        proc = subprocess.Popen(command, cwd=VM, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError("QEMU exited: " + (VM / "qemu.stderr.log").read_text(errors="replace")[-2000:])
    record.write_text(json.dumps({"pid": proc.pid, "command": command, "started_unix": time.time()}, indent=2), encoding="utf-8")
    print(json.dumps({"pid": proc.pid, "state": "booting", "ssh": SSH_PORT, "http": HTTP_PORT}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "start", "status", "ssh", "stop"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "init":
        init()
    elif args.action == "start":
        start()
    elif args.action == "status":
        return subprocess.run(ssh_arguments() + ["uname -sr; python3 --version; cloud-init status"], timeout=20).returncode
    elif args.action == "ssh":
        return subprocess.run(ssh_arguments() + args.command).returncode
    elif args.action == "stop":
        # Guest shutdown preserves its filesystem; no process-name based host termination.
        return subprocess.run(ssh_arguments() + ["sudo shutdown -h now"], timeout=20).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
