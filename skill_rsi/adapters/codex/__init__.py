"""Codex CLI, session and native trace adapters."""

from ..base import HostAdapter, evidence_input, probe_command
from ..session import decode_native, jsonl_sources, read_session
from .trace import read_bundle


class Adapter(HostAdapter):
    version = "2"
    name = "codex"
    command = "codex"
    offline_formats = ("trace", "rollout", "session", "cli", "responses")
    nodes = {
        "session_start": (),
        "before_action": ("read_origin", "append_context", "suggest"),
        "tool_result": (),
        "mcp": ("retrieve_original",),
        "mcp_tool_result": ("read_origin", "replace_tool_result", "retrieve_original"),
        "mcp_before_action": ("read_origin", "append_context"),
        "capture_start": ("append_context", "load_plugin", "suggest"),
    }
    hook_boundary = "any_event"

    def probe(self, project=None, state_dir=None, *, command=None):
        report = super().probe(project, state_dir, command=command)
        executable = report["command"]
        available = bool(executable and probe_command(executable, "debug", "trace-reduce", "--help"))
        return {**report, "native_trace_reducer": available}

    def command_line(self, prompt, resume, options):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Codex CLI capture requires a nonempty prompt")
        command = [options.get("command") or self.command, "exec"]
        if options.get("profile"):
            command += ["--profile", options["profile"]]
        if resume:
            command.append("resume")
        command += ["--json", "--skip-git-repo-check"]
        if options.get("model"):
            command += ["--model", options["model"]]
        command += list(options.get("args", ()))
        return [*command, "--", *([resume] if resume else []), prompt]

    def environment(self, output, options):
        env = super().environment(output, options)
        for name, value in options.get("_capture_environment", {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        if options.get("trace"):
            location = output / "codex-trace"
            location.mkdir(mode=0o700)
            env["CODEX_ROLLOUT_TRACE_ROOT"] = str(location)
        return env

    def run(self, output, command, project, prompt, resume, options):
        from ...evidence.archive import write

        if options.get("api") is not None or options.get("native_mcp") or options.get("components"):
            from .capture import run
            result = run(self, output, command, project, prompt, resume, options)
        else:
            result = super().run(output, command, project, prompt, resume, options)
        if options.get("trace"):
            bundles = sorted(path.parent for path in (output / "codex-trace").rglob("trace.jsonl")
                             if (path.parent / "manifest.json").is_file())
            result["capture_format"] = "trace" if bundles else "cli"
            result["native_trace_status"] = "observed" if bundles else "missing"
            write(output / "launch.json", result)
        return result

    def decode(self, row, origin):
        return decode_native(row, origin, self.name)

    def read(self, path, *, format=None, options=None):
        options = options or {}
        archive, source = evidence_input(path, options)
        if format not in {None, *self.offline_formats}:
            raise ValueError(f"Unsupported Codex format {format!r}")
        if format == "responses":
            from ..gateway import read_wire
            yield from read_wire(archive, source, "codex")
            return
        bundles = []
        if source.is_file() and source.name == "manifest.json":
            bundles = [source.parent]
        elif source.is_dir() and format in {None, "trace"}:
            bundles = sorted({p.parent for p in source.rglob("trace.jsonl") if (p.parent / "manifest.json").is_file()
                              and "derived" not in p.relative_to(source).parts})
        if bundles:
            for bundle in bundles:
                yield from read_bundle(archive, bundle, command=options.get("reducer_command", self.command), task=options.get("task"))
        elif format == "trace":
            raise ValueError(f"No Codex trace bundle at {source}")
        else:
            patterns = ("stdout.jsonl",) if format == "cli" else ("rollout-*.jsonl", "stdout.jsonl")
            paths = jsonl_sources(archive, source, options, patterns)
            for records in paths:
                from .evidence import enrich
                yield from enrich(archive, records.parent, list(read_session(archive, records, self.name)))

    def summarize_cli(self, stdout):
        report = {}
        for row in self.cli_rows(stdout):
            if row.get("type") == "thread.started" and isinstance(row.get("thread_id"), str):
                report["session_id"] = row["thread_id"]
            if row.get("type") == "turn.completed":
                report.update(last_turn_status="completed", usage=row.get("usage"))
            elif row.get("type") == "turn.failed":
                report["last_turn_status"] = "failed"
            elif row.get("type") == "item.completed" and isinstance(row.get("item"), dict):
                item = row["item"]
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    report["final_response"] = item["text"]
        return report
