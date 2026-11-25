"""Conservative exposure audit: MCP transport IDs never substitute for native IDs."""
import json
from pathlib import Path

from ....components.manifest import sha256


def _contains(value, text):
    if isinstance(value, str):
        if text in value:
            return True
        try:
            decoded = json.loads(value)
        except ValueError:
            return False
        return decoded != value and _contains(decoded, text)
    if isinstance(value, dict):
        return any(_contains(item, text) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, text) for item in value)
    return False


def codex_exposures(execution, snapshot, actor_path, action_tools):
    requests = []
    for path in sorted((Path(actor_path) / 'wire').glob('*/request.raw')):
        receipt_path = path.parent / 'receipt.json'
        if not receipt_path.is_file():
            continue
        raw = path.read_bytes()
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('response_complete') is True and receipt.get('request_sha256') == sha256(raw):
            requests.append((path, json.loads(raw)))
    rows = []
    for version in snapshot:
        proof = []
        for index, event in enumerate(execution['events']):
            native = event.get('native_call_id')
            returned = event.get('result', {})
            if not native or event.get('name') != 'Skill' or event.get('error') or not isinstance(returned, dict):
                continue
            if (returned.get('raw_sha256'), returned.get('content')) != (version['raw_sha256'], version['content']):
                continue
            for path, request in requests:
                items = request.get('input')
                if not isinstance(items, list) or request.get('previous_response_id'):
                    continue
                matches = [item for item in items if isinstance(item, dict) and item.get('call_id') == native
                           and item.get('type') in ('function_call_output', 'custom_tool_call_output', 'mcp_tool_call_output')]
                if len(matches) == 1 and _contains(matches[0].get('output'), version['content']):
                    proof.append({'event_index': index, 'next_request': str(path.relative_to(actor_path)),
                                  'wire_call_id': native, 'subsequent_actions': []})
        rows.append({'name': version['name'], 'skill_id': version.get('skill_id'),
                     'raw_sha256': version['raw_sha256'], 'body_proven': bool(proof), 'proof': proof,
                     'missing_reason': None if proof else 'unverified_native_call_to_request_mapping'})
    return rows
