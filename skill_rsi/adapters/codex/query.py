"""Explicit query dependencies, checked before a managed native prompt is sent."""
from ...evidence.archive import write
from ...components.manifest import sha256
from ...runtime import RuntimeEvent
from .gateway import RequestBoundary


def prepare(runtime, prompt, *, source, constraints, session_id, capture_id, output):
    if not isinstance(source, str):
        return prompt
    boundary = RequestBoundary()
    notes, decisions = [], []
    for index, resource in enumerate(constraints):
        if not isinstance(resource, dict) or source not in [resource.get('source_path'), *resource.get('target_paths', [])]:
            continue
        payload = {'target': capture_id, 'resource': resource}
        event = RuntimeEvent('codex', capture_id + ':query:' + str(index), 'before_use', session_id, payload,
                             input_version=sha256(prompt.encode()), run_id=capture_id)
        boundary.actions.clear()
        decisions.extend(runtime.dispatch(event, boundary))
        notes.extend({'text': action['payload']['text'], 'operation_key': key}
                     for action, key in boundary.actions if action['kind'] == 'append_context'
                     and isinstance(action['payload'].get('text'), str))
    write(output / 'query-prepared.json', {'session_id': session_id, 'source': source, 'notes': notes,
                                         'decisions': [decision.to_dict() for decision in decisions]})
    return '\n\n'.join([prompt, *(note['text'] for note in notes)])
