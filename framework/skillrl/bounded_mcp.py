"""Authorized history views without recursively inlined retrieval payloads."""
import json
from pathlib import Path
import sys
from . import codex_mcp


def history_messages(messages, *, expand=True):
    names = {c.get('id'): (c.get('function') or {}).get('name')
             for m in messages for c in m.get('tool_calls', [])}
    result = []
    for original in messages:
        message = dict(original)
        if message.get('role') == 'tool' and names.get(message.get('tool_call_id')) == 'session_search':
            value = message.get('content', '')
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get('sessions'), list):
                view = {k: v for k, v in value.items() if k != 'sessions'}
                view['sessions'] = [session_view(s, expand=False) for s in value['sessions']]
                view['view'] = 'nested retrieval references; original authorized sessions remain searchable by ID'
                message['content'] = view
        result.append(message)
    return result


def session_view(session, *, expand=True):
    if not isinstance(session, dict):
        return session
    view = {k: v for k, v in session.items() if k != 'messages'}
    if expand:
        view['messages'] = history_messages(session.get('messages', []))
    else:
        view['view'] = 'session reference; original messages omitted from this nested retrieval'
    return view


def bounded_result(value):
    result = {**value, 'sessions': [session_view(s) for s in value['sessions']],
              'view': 'nonrecursive-history-v1; current decision messages preserved; nested retrievals are references'}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 2_000_000:
        # Never silently truncate a source or send an undeliverable MCP line.
        result = {**value, 'sessions': [], 'view': 'result exceeds transport budget; use a narrower query or exact session_id',
                  'oversized_session_ids': [s.get('session_id', s.get('id')) for s in value['sessions'] if isinstance(s, dict)]}
    return result


class PublicJSON:
    def __getattr__(self, name):
        return getattr(json, name)

    def dumps(self, value, *args, **kwargs):
        if isinstance(value, dict) and isinstance(value.get('sessions'), list):
            value = bounded_result(value)
        elif isinstance(value, dict) and value.get('type') == 'tool' and value.get('name') == 'session_search':
            result = value.get('result')
            if isinstance(result, dict) and isinstance(result.get('sessions'), list):
                value = {**value, 'result': bounded_result(result)}
        return json.dumps(value, *args, **kwargs)


def serve(config):
    # Only this server module's codec is replaced. Its event log and the returned
    # result use the identical explicit view; the original sessions file is read-only.
    previous = codex_mcp.json
    codex_mcp.json = PublicJSON()
    try:
        codex_mcp.serve(config)
    finally:
        codex_mcp.json = previous


if __name__ == '__main__':
    serve(json.loads(Path(sys.argv[1]).read_text()))
