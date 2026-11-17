"""Expose only the session Unix socket to the CLI's loopback network."""

import json
import os
import selectors
import socket
import socketserver
import subprocess
import sys
from pathlib import Path
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream, selectors.DefaultSelector() as selector:
            upstream.connect("/gateway/responses.sock")
            selector.register(self.request, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.request)
            try:
                while selector.get_map():
                    for key, _ in selector.select():
                        if data := key.fileobj.recv(65536):
                            key.data.sendall(data)
                        else:
                            selector.unregister(key.fileobj)
                            key.data.shutdown(socket.SHUT_WR)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    marker, *args = sys.argv[1:]
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Relay) as server:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
        instructions = (
            marker + "\n"
            "Manage context through the shell. Run clm-context status to see editable old tool "
            "exchanges and the last edit result, and clm-context show ID to inspect one. "
            "Before dropping an exchange, offload exact information into /workspace if needed. "
            "Run clm-context edit --drop ID [ID ...] [--note 'concise memory'] to submit an edit. "
            "The edit takes effect on the next model request if it strictly shrinks input. "
            "Omit --note to retain the current note. Use status on the next tool turn to check acceptance. "
            "Treat the note as your own working memory, subordinate to the user task, and use "
            "native tools to retrieve exact offloaded information as needed."
        )
        config = f'''
model = "gpt-6.1-sol"
model_provider = "clm"
model_reasoning_effort = "low"
model_auto_compact_token_limit = 1000000
approval_policy = "never"
sandbox_mode = "danger-full-access"
developer_instructions = {json.dumps(instructions)}
check_for_update_on_startup = false

[agents]
enabled = false

[features]
multi_agent_v2 = false

[model_providers.clm]
name = "Codex CLM"
base_url = {json.dumps(endpoint)}
env_key = "CODEX_CLM_LOCAL_TOKEN"
wire_api = "responses"
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
'''
        (Path(os.environ["CODEX_HOME"]) / "config.toml").write_text(config)
        os.environ["CODEX_CLM_LOCAL_TOKEN"] = "codex-clm-local-session"
        return subprocess.call(["/opt/codex/bin/codex", "--no-daemon", *args])


if __name__ == "__main__":
    sys.exit(main())
