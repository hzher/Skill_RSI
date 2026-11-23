"""Claude Code CLI, native transcript, hook and Messages adapters."""

from ..base import HostAdapter, evidence_input
from ..session import decode_native, jsonl_sources, read_session


class Adapter(HostAdapter):
    name = "claude_code"
    command = "claude"
    offline_formats = ("cli", "session", "messages")
    nodes = {
        "session_start": ("append_context", "suggest"),
        "compaction": (),
        "before_action": ("read_origin", "append_context", "suggest"),
        "tool_result": ("read_origin", "replace_tool_result", "retrieve_original", "suggest"),
        "mcp": ("retrieve_original",),
        "mcp_tool_result": ("read_origin", "replace_tool_result", "retrieve_original"),
        "mcp_before_action": ("read_origin", "append_context"),
    }
    hook_boundary = "any_event"

    def command_line(self, prompt, resume, options):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Claude Code CLI capture requires a nonempty prompt")
        command = [options.get("command") or self.command, "--print", "--output-format", "stream-json", "--verbose"]
        if options.get("model"):
            command += ["--model", options["model"]]
        if resume:
            command += ["--resume", resume]
        return [*command, *options.get("args", ()), "--", prompt]

    def decode(self, row, origin):
        return decode_native(row, origin, self.name)

    def read(self, path, *, format=None, options=None):
        archive, source = evidence_input(path, options)
        if format not in {None, *self.offline_formats}:
            raise ValueError(f"Unsupported Claude Code format {format!r}")
        if format == "messages":
            from ..gateway import read_wire
            yield from read_wire(archive, source, self.name)
            return
        patterns = ("stdout.jsonl",) if format == "cli" else ("*.jsonl",)
        for records in jsonl_sources(archive, source, options, patterns):
            yield from read_session(archive, records, self.name)

    def summarize_cli(self, stdout):
        report = {}
        for row in self.cli_rows(stdout):
            if isinstance(row.get("session_id"), str):
                report["session_id"] = row["session_id"]
            if row.get("type") == "result":
                report.update(last_turn_status="failed" if row.get("is_error") else "completed",
                              usage=row.get("usage"), final_response=row.get("result"))
        return report
