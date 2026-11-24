"""DeepSeek Harness CLI and SDK profile adapter."""

from ...evidence.schema import NativeRun, identifier
from ..base import HostAdapter, evidence_input, probe_command, read_optional
from .reader import _calls, _capture, _memory, _session, decode_session_row, read_runs


class Adapter(HostAdapter):
    name = "dsh"
    command = "dsh"
    offline_formats = ("batch", "session", "cli")
    nodes = {
        "session_start": (),
        "tool_result": ("read_origin", "replace_tool_result", "retrieve_original"),
        "before_action": ("read_origin", "append_context"),
        "before_use": ("read_origin", "append_context"),
    }
    hook_boundary = "any_event"

    def probe(self, project=None, state_dir=None, *, command=None):
        return {**super().probe(project, state_dir, command=command), "node_version": probe_command("node", "--version")}

    def decode(self, row, origin):
        yield decode_session_row(row, origin)

    def read(self, path, *, format=None, options=None):
        archive, source = evidence_input(path, options)
        options = options or {}
        if format not in {None, *self.offline_formats}:
            raise ValueError(f"Unsupported DSH format {format!r}")
        if format == "batch" or (source.is_dir() and ((source / "checkpoints").is_dir() or (source / "native").is_dir())):
            if source != archive.root:
                raise ValueError("DSH batch selection must match the source root")
            yield from read_runs(archive, sources=options.get("sources"), coverage=options.get("coverage"))
            return
        if source.is_file():
            paths = [source]
            root = source.parent
        else:
            paths = sorted(source.glob("sessions/*/*/session.v4.jsonl"))
            root = source
        launch_file = root / "launch.json"
        launch = read_optional(launch_file) or {}
        identity = {
            "batch_id": archive.root.name, "job_attempt_id": launch.get("run_id") or identifier("dsh", str(root.relative_to(archive.root))),
            "task": options.get("task"), "status": launch.get("status", "unknown"),
            "resume_from": launch.get("resume_from"), "harness_version": launch.get("harness_version"),
            "runtime_ids": [root.name], "capture_capabilities": {"physical_requests": "unknown"},
        }
        run = NativeRun(identity)
        begin = len(archive.diagnostics)
        if launch:
            run.sources.append(archive.ref(launch_file, layer="native_process_receipt"))
        queries = {}
        captured = _capture(archive, run, root, queries)
        _, outputs = _session(archive, run, root, files=paths if source.is_file() else None)
        _memory(archive, run, root, captured["memory_operations"])
        _calls(archive, run, root, captured["calls"], queries, outputs)
        run.diagnostics.extend(archive.diagnostics[begin:])
        if not run.events:
            raise ValueError(f"No supported DSH records at {source}")
        yield run

    def run(self, output, command, project, prompt, resume, options):
        if command is not None:
            return super().run(output, command, project, prompt, resume, options)
        from .sdk import run
        return run(output, project, prompt, resume, options)
