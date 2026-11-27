"""Codex CLI, session and native trace adapters."""

from ..base import HostAdapter, evidence_input, probe_command, host_record, read_optional
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
        "tool_result": ("read_origin", "replace_tool_result", "retrieve_original", "append_context"),
        "before_request": ("append_context",),
        "before_use": ("read_origin", "append_context"),
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
        report["native_trace_reducer"] = available
        if state_dir is not None:
            from pathlib import Path
            from .gateway import RequestBoundary
            receipt = read_optional(host_record(project or '.', state_dir, 'codex').parent / 'gateway.json')
            if (receipt and receipt.get('adapter_version') == self.version
                    and receipt.get('project') == str(Path(project or '.').resolve())
                    and receipt.get('host_version') == report.get('host_version')
                    and receipt.get('registration_id') == (report.get('registration') or {}).get('registration_id')
                    and Path(receipt['forward_path']).is_file()):
                report.update(RequestBoundary(self.version).capabilities(), loaded=True,
                              gateway_receipt=receipt, capability_scope='verified_managed_gateway_launch',
                              missing_reasons=[r for r in report['missing_reasons'] if r != 'awaiting_native_callback'])
        return report

    def attach(self, project, state_dir, **options):
        previous = read_optional(host_record(project, state_dir, self.name)) or {}
        if options.get('api') is not None or previous.get('transport') == 'gateway':
            from .registration import install
            return install(self, project, state_dir, **options)
        return super().attach(project, state_dir, **options)

    def detach(self, project, state_dir, **options):
        previous = read_optional(host_record(project, state_dir, self.name)) or {}
        if previous.get('transport') == 'gateway':
            from .registration import uninstall
            return uninstall(self, project, state_dir)
        return super().detach(project, state_dir, **options)

    def launch(self, command=None, *, project, state_dir, prompt=None, resume=None, options=None):
        registration = read_optional(host_record(project, state_dir, self.name)) or {}
        configured = registration.get('launch_options', {}) if registration.get('transport') == 'gateway' else {}
        selected = {**configured, **(options or {})}
        return super().launch(command, project=project, state_dir=state_dir, prompt=prompt,
                              resume=resume, options=selected)

    def command_line(self, prompt, resume, options):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Codex CLI capture requires a nonempty prompt")
        command = [options.get("command") or self.command, "exec"]
        if options.get("profile"):
            command += ["--profile", options["profile"]]
        args = list(options.get("args", ()))
        if resume:
            # Sandbox selection belongs to exec, including on resumed turns.
            remaining = []
            iterator = iter(args)
            for arg in iterator:
                if arg in ("--sandbox", "-s"):
                    value = next(iterator, None)
                    if value is None:
                        raise ValueError("sandbox option requires a mode")
                    command.extend((arg, value))
                elif arg.startswith("--sandbox="):
                    command.append(arg)
                else:
                    remaining.append(arg)
            args = remaining
            command.append("resume")
        command += ["--json", "--skip-git-repo-check"]
        if options.get("model"):
            command += ["--model", options["model"]]
        command += args
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
            from .wire import read
            yield from read(archive, source)
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
