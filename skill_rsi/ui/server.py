"""Loopback-only HTTP UI with authenticated same-origin actions."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import hmac
import json
import mimetypes
import secrets
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .service import Manager
from ..evidence.io import write_bytes


def _json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "SkillRSI"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default).encode())

    def _allowed(self, *, authenticated=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self._json(403, {"error": "请使用终端显示的本机地址"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{expected}":
            self._json(403, {"error": "请求来源无效"})
            return False
        if authenticated and not hmac.compare_digest(self.headers.get("X-Skill-RSI-Token", ""), self.server.token):
            self._json(401, {"error": "请使用启动时显示的完整链接重新打开界面"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path)
        if not self._allowed(authenticated=path.path.startswith("/api/")):
            return
        try:
            if path.path == "/api/state":
                query = parse_qs(path.query)
                response = self.server.manager.state(query.get("project", [None])[0],
                                                     query.get("host", ["dsh"])[0],
                                                     refresh=query.get("refresh") == ["1"])
                self._json(200, response)
            elif path.path == "/api/task":
                query = parse_qs(path.query)
                self._json(200, self.server.manager.task(query.get("id", [""])[0]))
            elif path.path == "/api/analysis":
                query = parse_qs(path.query)
                self._json(200, self.server.manager.analysis(query.get("task_id", [""])[0]))
            elif path.path == "/api/llm":
                self._json(200, self.server.manager.llm())
            elif path.path == "/api/emergence/settings":
                self._json(200, self.server.manager.emergence_settings())
            elif path.path == "/api/emergence":
                query = parse_qs(path.query)
                self._json(200, self.server.manager.emergence(query.get("task_id", [""])[0]))
            elif path.path in {"/", "/index.html", "/app.js", "/style.css"}:
                name = "index.html" if path.path == "/" else path.path[1:]
                self._send(200, files("skill_rsi.ui").joinpath("static", name).read_bytes(),
                           mimetypes.guess_type(name)[0] or "application/octet-stream")
            else:
                self._json(404, {"error": "页面不存在"})
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self):
        if not self._allowed(authenticated=True):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("请求大小超出范围")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请提交 JSON 数据")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象")
            manager = self.server.manager
            routes = {"/api/configure": manager.configure, "/api/install": manager.install,
                      "/api/start": manager.start,
                      "/api/desktop": manager.desktop, "/api/desktop/remove": manager.remove_desktop,
                      "/api/analyze": manager.analyze,
                      "/api/llm": manager.save_llm,
                      "/api/emergence": manager.emerge,
                      "/api/emergence/settings": manager.save_emergence_settings,
                      "/api/open-path": manager.open_path,
                      "/api/codex/recording": manager.codex_recording,
                      "/api/codex/sync": manager.codex_sync,
                      "/api/cancel": lambda value: manager.tasks.cancel(value["task_id"])}
            action = routes.get(urlsplit(self.path).path)
            if action is None:
                self._json(404, {"error": "操作不存在"})
                return
            self._json(200, action(data))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        try:
            if hasattr(self, "manager"):
                self.manager.close()
        finally:
            super().server_close()


def create_server(project, state_dir, *, tasks_dir=None, port=0):
    manager = Manager(project, state_dir, tasks_dir)
    server = LocalServer(("127.0.0.1", port), Handler)
    token_path = manager.state_dir / "ui-access.token"
    try:
        write_bytes(token_path, secrets.token_urlsafe(32).encode(), replace=False)
    except FileExistsError:
        pass
    if token_path.is_symlink():
        server.server_close()
        raise ValueError("UI access token must be a regular local file")
    token = token_path.read_text().strip()
    if len(token) < 32 or not token.isascii():
        server.server_close()
        raise ValueError("Invalid local UI access token")
    server.manager, server.token = manager, token
    manager.start_recording()
    return server


def serve(args):
    with create_server(args.project, args.state_dir, tasks_dir=args.tasks_dir, port=args.port) as server:
        url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
        print(f"Skill RSI  {url}", flush=True)
        print(f"任务轨迹  {server.manager.tasks.root}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return {"status": "stopped"}
