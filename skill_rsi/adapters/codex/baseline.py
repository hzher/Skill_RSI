"""Versioned native-shell control of gateway tool views (no MCP dependency)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ...evidence.io import digest, encode, file_lock, read_json, write_json
from ...runtime import project_identity

MAX_SOURCES = 24
MAX_BYTES = 512 * 1024


class ToolViews:
    def __init__(self, project, state_dir, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('explicit native session required')
        self.project_id = project_identity(project)
        self.session_id = session_id
        self.path = Path(state_dir).resolve() / 'codex-views' / self.project_id / (digest(session_id.encode()) + '.json')

    def _load(self):
        if not self.path.is_file():
            return {'schema': 'skill-rsi.codex-tool-views.v1', 'project_id': self.project_id,
                    'session_id': self.session_id, 'version': 0, 'entries': {}, 'projected': {}, 'operations': {},
                    'order': [], 'operation_order': []}
        value = read_json(self.path)
        if (value.get('schema') != 'skill-rsi.codex-tool-views.v1' or value.get('project_id') != self.project_id
                or value.get('session_id') != self.session_id):
            raise ValueError('tool view identity mismatch')
        return value

    def observe(self, results):
        with file_lock(self.path.with_suffix('.lock')):
            state = self._load()
            for call_id, text in results:
                if not isinstance(call_id, str) or not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
                    continue
                if state['entries'].get(call_id) == text:
                    continue
                if call_id not in state['entries']:
                    state['order'].append(call_id)
                state['entries'][call_id] = text
                state['projected'].pop(call_id, None)
                if len(state['entries']) > MAX_SOURCES:
                    oldest = state['order'].pop(0)
                    state['entries'].pop(oldest)
                    state['projected'].pop(oldest, None)
                state['version'] += 1
            write_json(self.path, state)
            return state

    def invoke(self, kind, arguments, *, operation_id):
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError('a control operation needs a stable identity')
        with file_lock(self.path.with_suffix('.lock')):
            state = self._load()
            fingerprint = digest(encode([kind, arguments]))
            previous = state['operations'].get(operation_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('operation identity reused with different inputs')
                return previous['receipt']
            result = self._apply(state, kind, arguments)
            receipt = {**result, 'operation_id': operation_id, 'project_id': self.project_id,
                       'session_id': self.session_id, 'scope': 'gateway_tool_view_state',
                       'model_input_visibility': 'unknown'}
            state['operations'][operation_id] = {'fingerprint': fingerprint, 'receipt': receipt}
            state['operation_order'].append(operation_id)
            # Keep the replay ledger bounded separately from original-result retention.
            if len(state['operations']) > 512:
                state['operations'].pop(state['operation_order'].pop(0))
            write_json(self.path, state)
            return receipt

    @staticmethod
    def _apply(state, kind, args):
        entries, projected = state['entries'], state['projected']
        if kind == 'list' and not args:
            return {'version': state['version'], 'results': [
                {'call_id': key, 'lines': len(text.split('\n')), 'focused': key in projected,
                 'preview': [line[:160] for line in text.split('\n') if line.strip()][-2:]}
                for key in state['order'] for text in [entries[key]]]}
        allowed = {'read': {'call_id', 'start_line', 'end_line'},
                   'focus': {'call_id', 'start_line', 'end_line', 'version'},
                   'restore': {'call_id', 'version'}}
        if kind not in allowed or set(args) != allowed[kind]:
            raise ValueError('unknown tool view operation or arguments')
        call_id = args['call_id']
        if not isinstance(call_id, str) or call_id not in entries:
            raise ValueError('identifier is outside the available native tool history')
        if kind in {'focus', 'restore'} and (type(args['version']) is not int or args['version'] != state['version']):
            raise ValueError('tool view version changed; refresh the catalog')
        if kind == 'restore':
            if call_id in projected:
                projected.pop(call_id)
                state['version'] += 1
            return {'version': state['version'], 'call_id': call_id, 'focused': False}
        lines = entries[call_id].split('\n')
        start, end = args['start_line'], args['end_line']
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError('line range is outside the source')
        if kind == 'read':
            through = min(start + 39, end)
            text = '\n'.join(lines[start - 1:through])
            if len(text.encode()) > 12288:
                raise ValueError('choose a smaller range of original lines')
            return {'text': text, 'start_line': start, 'end_line': through,
                    'next_line': through + 1 if through < end else None}
        text = ('[…]\n' if start > 1 else '') + '\n'.join(lines[start - 1:end]) + ('\n[…]' if end < len(lines) else '')
        if len(text.encode()) >= len(entries[call_id].encode()):
            raise ValueError('choose a range that shortens the native output')
        projected[call_id] = {'text': text, 'original_sha256': digest(entries[call_id].encode()),
                              'start_line': start, 'end_line': end}
        state['version'] += 1
        return {'version': state['version'], 'call_id': call_id, 'focused': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('kind', choices=['list', 'read', 'focus', 'restore'])
    parser.add_argument('--arguments', default='{}', help='JSON object; always shell-quote it')
    args = parser.parse_args(argv)
    try:
        result = ToolViews(args.project, args.state_dir, args.session_id).invoke(
            args.kind, json.loads(args.arguments), operation_id=args.operation_id)
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({'error': str(error), 'status': 'not_applied'}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
