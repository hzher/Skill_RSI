"""Verify container isolation and host Unix socket connectivity without an API call."""

import json
import os
import socket
import subprocess
from pathlib import Path

assert os.getuid() == 1005
assert os.environ["CODEX_HOME"] == "/home/clm/.codex"
assert not any(key.startswith("OPENAI_") for key in os.environ)
assert not Path("/mount-data").exists()
assert not Path("/var/docker/proxy/docker-proxy.sock").exists()
assert {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}
assert os.statvfs("/").f_flag & os.ST_RDONLY
assert os.statvfs("/opt/codex/bin/codex").f_flag & os.ST_RDONLY
Path("/workspace/isolation-probe").write_text("owned by this session\n")
with socket.socket(socket.AF_UNIX) as sock:
    sock.connect("/gateway/responses.sock")
    sock.sendall(b"POST /unexpected HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
    assert b"404" in sock.recv(1024).splitlines()[0]
print(json.dumps({
    "uid": os.getuid(),
    "interfaces": ["lo"],
    "root_and_binary_read_only": True,
    "host_cpfs_and_docker_socket_absent": True,
    "real_credential_absent": True,
    "scoped_gateway_responded": True,
    "codex": subprocess.check_output(["/opt/codex/bin/codex", "--version"], text=True).strip(),
}))
