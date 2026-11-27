"""Project-bound interventions at the Responses HTTP boundary.

Only complete, stateless inputs are editable. Native tools keep executing in
Codex; this gateway never executes a tool call or invents its success status.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys
import threading

from ...components.manifest import canonical, sha256
from ...evidence.archive import write
from ...evidence.io import file_lock, read_json, write_json
from ...runtime import Runtime, RuntimeEvent
from ..bridge import _constraint
from ..gateway import WireRecorder


def code_digest():
    root = Path(__file__).resolve().parents[2]
    return sha256(canonical({path.relative_to(root).as_posix(): sha256(path.read_bytes())
                             for path in sorted(root.rglob('*.py'))}))


class RequestBoundary:
    def __init__(self, version="2"):
        self.version = version
        self.actions = []

    def capabilities(self, session=None):
        from ..wasm import available
        nodes = {
            "tool_result": ["read_origin", "replace_tool_result", "retrieve_original", "append_context"],
            "before_action": ["read_origin", "append_context"],
            "before_use": ["read_origin", "append_context"],
            "before_request": ["append_context"],
        }
        caps = {value for values in nodes.values() for value in values}
        if available():
            caps.add("execute_wasm_component")
        return {"host": "codex", "adapter_version": self.version, "nodes": nodes,
                "boundaries": list(nodes), "capabilities": sorted(caps), "reload_boundary": "any_event"}

    def propose_native(self, context, engine):
        if engine == "wasm":
            from ..wasm import decide
            return decide(context)
        raise ValueError("Gateway accepts Skills, verified built-in policies, and isolated Wasm components")

    def deliver(self, action, key):
        if action["kind"] not in {"replace_tool_result", "append_context", "display_suggestion"}:
            return {"status": "skipped", "stage": "submitted", "missing_reason": "unsupported_gateway_action"}
        self.actions.append((action, key))
        # Preparing an output is not yet evidence that the socket accepted it.
        return {"status": "submitted", "stage": "submitted", "operation_key": key,
                "delivery_scope": "gateway_preparation", "model_input_visibility": "unknown"}


def tool_pairs(items):
    """Unique ordered native call/result groups only; ambiguous groups stay untouched."""
    calls, results = {}, {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("call_id"), str):
            continue
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            calls.setdefault(item["call_id"], []).append((index, item))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            results.setdefault(item["call_id"], []).append((index, item))
    for key, values in results.items():
        sources = calls.get(key, [])
        if len(sources) != 1 or len(values) != 1 or sources[0][0] >= values[0][0]:
            continue
        call, result = sources[0][1], values[0][1]
        expected = "custom_tool_call_output" if call["type"] == "custom_tool_call" else "function_call_output"
        if result["type"] == expected and isinstance(result.get("output"), str):
            yield key, call, result


def success_of(result):
    """A string is not a success receipt. Codex shell wrappers provide an exit code."""
    if result.get("is_error") is True or result.get("isError") is True:
        return False
    if result.get("is_error") is False or result.get("isError") is False:
        return True
    text = result["output"]
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("is_error") is True:
            return False
        metadata = value.get("metadata", {})
        code = metadata.get("exit_code") if isinstance(metadata, dict) else None
        if type(code) is int:
            return code == 0
    import re
    match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", text)
    return int(match[1]) == 0 if match else None


def source_read(call, arguments, text, project):
    """Recognize only an explicit whole-file read, then compare exact source bytes."""
    source = arguments.get('path') or arguments.get('file_path')
    if source:
        return source, text
    if call.get('name') not in {'exec_command', 'shell_command', 'shell'}:
        return None, None
    command = arguments.get('cmd') or arguments.get('command')
    if isinstance(command, list) and len(command) == 3 and command[:2] in (['bash', '-lc'], ['sh', '-lc']):
        command = command[2]
    if not isinstance(command, str):
        return None, None
    try:
        words = shlex.split(command)
    except ValueError:
        return None, None
    if len(words) == 3 and words[:2] == ['cat', '--']:
        source = words[2]
    elif len(words) == 2 and words[0] == 'cat' and not words[1].startswith('-'):
        source = words[1]
    else:
        return None, None
    cwd = arguments.get('workdir') or arguments.get('work_dir') or str(project)
    try:
        source = str((Path(cwd) / source).resolve().relative_to(project))
    except ValueError:
        return None, None
    marker = 'Final output:\n'
    if marker in text:
        return source, text.split(marker, 1)[1]
    try:
        value = json.loads(text)
    except ValueError:
        return None, None
    return source, value.get('output') if isinstance(value, dict) and isinstance(value.get('output'), str) else None


class CodexGateway(WireRecorder):
    def __init__(self, upstream, output, *, project, state_dir, session_id, run_id=None,
                 constraints=(), credential=None, port=0, timeout=120, baseline_tools=False, native_stdout=None,
                 query_source=None, public_actions=False, proxy_mode='environment'):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Gateway needs an explicit connection/session identity")
        self.runtime = Runtime(project, state_dir=state_dir)
        self.session_id, self.run_id = session_id, run_id or session_id
        self.constraints = list(constraints)
        self.baseline_tools = baseline_tools
        self.native_stdout = Path(native_stdout) if native_stdout else None
        self.query_source = query_source
        if type(public_actions) is not bool or proxy_mode not in {'environment', 'direct'}:
            raise ValueError('invalid public_actions or proxy_mode')
        self.public_actions, self.proxy_mode = public_actions, proxy_mode
        self.fail_closed_preparation = public_actions
        self.lock = threading.RLock()
        super().__init__("codex", upstream, output, credential=credential, port=port, timeout=timeout)

    def prepare_request(self, raw, key, directory):
        with self.lock:
            prepared = self._prepare(raw, key, directory)
            if self.public_actions:
                request = json.loads(prepared)
                original = request.get('tools', [])
                if not isinstance(original, list):
                    raise ValueError('native tools must be a list')
                request['tools'] = []
                request['tool_choice'] = 'none'
                request.pop('parallel_tool_calls', None)
                write(directory / 'public-actions.json', {
                    'policy': 'public-benchmark-json-actions-v1',
                    'removed_native_tools': len(original), 'native_host_tools': False,
                })
                prepared = json.dumps(request, ensure_ascii=False).encode()
            return prepared

    def open_upstream(self, parsed, timeout):
        from .transport import connection
        return connection(parsed, timeout, self.proxy_mode)

    def _prepare(self, raw, key, directory):
        if self.native_stdout and self.native_stdout.is_file():
            for line in self.native_stdout.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('type') == 'thread.started' and isinstance(row.get('thread_id'), str):
                    self.session_id = row['thread_id']
                    break
        body = json.loads(raw)
        items = body.get("input")
        client = read_json(directory / 'client.json') if (directory / 'client.json').is_file() else {}
        request_thread = client.get('thread-id') or client.get('x-codex-thread-id')
        reason = None
        if body.get("previous_response_id") or body.get("conversation"):
            reason = "unreconstructed_server_history"
        elif body.get('purpose') is not None or isinstance(body.get('metadata'), dict) and body['metadata'].get('purpose') is not None:
            reason = 'auxiliary_request'
        elif not isinstance(items, list):
            reason = "input_is_not_complete_item_list"
        elif self.native_stdout and request_thread != self.session_id:
            reason = 'different_native_thread' if request_thread else 'native_request_thread_unresolved'
        write(directory / "gateway.json", {"project_id": self.runtime.project_id, "session_id": self.session_id,
              "run_id": self.run_id, "request_id": key, "editable": reason is None, "missing_reason": reason,
              "native_request_thread_id": request_thread,
              "request_role": 'primary' if self.native_stdout and request_thread == self.session_id else 'unresolved',
              "ingress_sha256": sha256(raw), "server_final_input": "unknown",
              "schema": "skill-rsi.codex-gateway.v1", "adapter_code_sha256": code_digest()})
        if reason:
            return raw
        boundary = RequestBoundary()
        decisions, applied, notes, suggestions = [], [], [], []
        seen_path = (self.runtime.state_dir / 'codex-result-boundaries' / self.runtime.project_id /
                     (sha256(self.session_id.encode()) + '.json'))
        with file_lock(seen_path.with_suffix('.lock')):
            seen = read_json(seen_path) if seen_path.is_file() else {}
        observed_results = {}
        query_file = self.native_stdout.parent / 'query-prepared.json' if self.native_stdout else None
        if query_file and query_file.is_file():
            prepared = read_json(query_file)
            def contains(value, text):
                if isinstance(value, str):
                    return text in value
                if isinstance(value, list):
                    return any(contains(item, text) for item in value)
                if isinstance(value, dict):
                    return any(contains(item, text) for item in value.values())
                return False
            for note in prepared.get('notes', []):
                if contains(items, note['text']):
                    applied.append(note['operation_key'])
        pending = self.native_stdout.parent / 'native-pending.json' if self.native_stdout else None
        if pending and pending.is_file():
            with file_lock(pending.with_suffix('.lock')):
                for operation, note in read_json(pending).items():
                    if note['session_id'] == self.session_id:
                        notes.append(note['text'])
                        applied.append(operation)
        manual = None
        changed = False
        manual_applied = []
        if self.baseline_tools:
            from .baseline import ToolViews
            views = ToolViews(self.runtime.project, self.runtime.state_dir, self.session_id)
            pairs = list(tool_pairs(items))
            manual = views.observe((call_id, result['output']) for call_id, call, result in pairs
                if success_of(result) is True and 'skill_rsi.adapters.codex.baseline' not in call.get('arguments', ''))
            for call_id, call, result in pairs:
                projection = manual['projected'].get(call_id)
                if (projection and success_of(result) is True
                        and projection['original_sha256'] == sha256(result['output'].encode())):
                    result['output'] = projection['text']
                    manual_applied.append({'call_id': call_id, 'version': manual['version'],
                                           'original_sha256': projection['original_sha256'],
                                           'projected_sha256': sha256(projection['text'].encode())})
                    changed = True
            command = shlex.join([sys.executable, '-m', 'skill_rsi.adapters.codex.baseline',
                '--project', str(self.runtime.project), '--state-dir', str(self.runtime.state_dir),
                '--session-id', self.session_id])
            notes.append('Optional native tool-history controls, via the shell tool: ' + command +
                ' --operation-id UNIQUE_ID KIND --arguments JSON. KIND is list ({}), read '
                '({"call_id":"...","start_line":1,"end_line":40}), focus '
                '({"call_id":"...","start_line":1,"end_line":4,"version":N}), or restore '
                '({"call_id":"...","version":N}). Use a fresh operation ID for each new command; '
                'quote the JSON. Read originals by line. Focus/restore affect later requests only. '
                'Refresh the catalog after a version conflict.')
            changed = True

        def dispatch(node, identity, payload, version):
            boundary.actions.clear()
            ref = {"request_id": key, "native_call_id": payload.get("call_id"), "ingress_sha256": sha256(raw)}
            self.runtime.confirm("codex", node, ref, boundary.capabilities())
            bindings = self.runtime.bindings("codex")
            if not any(binding.enabled and set(self.runtime.registry.resolve(
                binding.component_id, binding.version, binding.form).manifest['trigger']['boundaries']) & {node, '*'}
                       for binding in bindings):
                return []
            event = RuntimeEvent("codex", key + ":" + identity, node, self.session_id, payload,
                                 input_version=version, run_id=self.run_id,
                                 trigger_ref=ref)
            decisions.extend(self.runtime.dispatch(event, boundary))
            suggestions.extend(action['payload'] for action, _ in boundary.actions
                               if action['kind'] == 'display_suggestion')
            return list(boundary.actions)

        dispatch_actions = dispatch("before_request", "request", {"target": key}, sha256(raw))
        for action, operation in dispatch_actions:
            if action["kind"] == "append_context" and isinstance(action["payload"].get("text"), str):
                notes.append(action["payload"]["text"])
                applied.append(operation)
        for call_id, call, result in tool_pairs(items):
            original_hash = sha256(result['output'].encode())
            if call_id in seen:
                continue
            observed_results[call_id] = original_hash
            if manual and call_id in manual['projected']:
                continue
            text = result["output"]
            # Explicit failed results are always preserved. Unknown results are
            # observable but not eligible for destructive text projection.
            success = success_of(result)
            payload = {"target": call_id, "call_id": call_id, "tool": call.get("name"),
                       "text": text, "success": success, "allow_projection": success is True,
                       "source_ref": {"path": str(directory / "request.ingress.raw"), "sha256": sha256(raw)}}
            try:
                arguments = json.loads(call.get("arguments", "{}"))
            except (ValueError, TypeError):
                arguments = {}
            for resource in self.constraints:
                if not isinstance(resource, dict) or not isinstance(arguments, dict):
                    continue
                source, content = source_read(call, arguments, text, self.runtime.project)
                if source == resource.get("source_path") and success is not False:
                    fact = _constraint(self.runtime.project, resource)
                    if fact.get("verified") and content is not None and sha256(content.encode()) == fact["source_digest"]:
                        payload["constraint_observation"] = fact
            for action, operation in dispatch("tool_result", call_id, payload, sha256(text.encode())):
                if action["kind"] == "replace_tool_result" and action["target"] == call_id and success is True:
                    replacement = action["payload"].get("text")
                    if isinstance(replacement, str) and len(replacement.encode()) < len(text.encode()):
                        result["output"] = replacement
                        applied.append(operation)
                elif action["kind"] == "append_context" and isinstance(action["payload"].get("text"), str):
                    notes.append(action["payload"]["text"])
                    applied.append(operation)
        if any("expand result-" in str(item.get("output", "")) for item in items if isinstance(item, dict)):
            command = shlex.join([sys.executable, "-m", "skill_rsi", "--state-dir", str(self.runtime.state_dir),
                                  "project", "expand", "--project", str(self.runtime.project)])
            notes.append("Read a preserved tool original with the native shell tool: " + command + " HANDLE")
        if notes:
            items.append({"role": "developer", "content": "\n\n".join(notes)})
        write(directory / "operations.json", {"decisions": [d.to_dict() for d in decisions],
                                             "prepared_operations": applied,
                                             "manual_views": manual_applied,
                                             "observed_results": observed_results,
                                             "suggestions": suggestions})
        return canonical(body) if applied or changed else raw

    def request_forwarded(self, key, directory):
        path = directory / "operations.json"
        if not path.is_file():
            return
        with self.lock:
            value = json.loads(path.read_text())
            seen_path = (self.runtime.state_dir / 'codex-result-boundaries' / self.runtime.project_id /
                         (sha256(self.session_id.encode()) + '.json'))
            with file_lock(seen_path.with_suffix('.lock')):
                seen = read_json(seen_path) if seen_path.is_file() else {}
                for call_id, checksum in value.get('observed_results', {}).items():
                    seen.setdefault(call_id, checksum)
                write_json(seen_path, seen)
            for operation in value["prepared_operations"]:
                self.runtime.record_receipt(operation, {"status": "applied", "stage": "delivered",
                    "receipt_ref": str(directory / "forward.json"), "request_id": key,
                    "delivery_scope": "gateway_socket_write", "model_input_visibility": "unknown"})
            value["decisions"] = [d.to_dict() for d in self.runtime.decisions("codex", session_id=self.session_id)
                                  if d.event_id.startswith(key + ":") or d.operation_key in value['prepared_operations']]
            write(path, value)
            if self.native_stdout:
                query_file = self.native_stdout.parent / 'query-prepared.json'
                if query_file.is_file():
                    prepared = read_json(query_file)
                    prepared['notes'] = [note for note in prepared.get('notes', [])
                                         if note['operation_key'] not in value['prepared_operations']]
                    write_json(query_file, prepared)
            if self.native_stdout:
                pending = self.native_stdout.parent / 'native-pending.json'
                if pending.is_file():
                    with file_lock(pending.with_suffix('.lock')):
                        notes = read_json(pending)
                        for operation in value['prepared_operations']:
                            notes.pop(operation, None)
                        write_json(pending, notes)


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--credential-env")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    credential = os.environ[args.credential_env] if args.credential_env else None
    recorder = CodexGateway(args.upstream, args.output, project=args.project, state_dir=args.state_dir,
                            session_id=args.session_id, credential=credential, port=args.port)
    print(json.dumps({"base_url": recorder.base_url + "/v1", "output": str(recorder.output)}), flush=True)
    try:
        recorder.serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
