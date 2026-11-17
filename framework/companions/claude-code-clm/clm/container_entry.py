"""Expose only the session Unix socket to the CLI's loopback network."""

import os
import selectors
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream, selectors.DefaultSelector() as selector:
            upstream.connect("/gateway/messages.sock")
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
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Relay) as server:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        # The host process is the only process that gets the actual credential.
        os.environ["ANTHROPIC_API_KEY"] = "clm-session-local"
        return subprocess.call(["/opt/claude", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
