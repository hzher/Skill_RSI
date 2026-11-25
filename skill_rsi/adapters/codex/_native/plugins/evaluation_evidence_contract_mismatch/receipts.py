"""A score-blind projection of actual internal calls for the evaluator boundary."""
import json

_INTERNAL = {'memory', 'session_search', 'Skill', 'skill_view', 'skill_manage'}


def public_receipts(session):
    calls, results = {}, {}
    for message in session.get('messages', []):
        for call in message.get('tool_calls', []):
            calls[call['id']] = (call, message.get('source_actor', 'executor'))
        if message.get('role') == 'tool' and message.get('tool_call_id'):
            results[message['tool_call_id']] = message
    receipts = []
    for call_id, (call, actor) in calls.items():
        function = call.get('function', {})
        if function.get('name') not in _INTERNAL:
            continue
        returned = results.get(call_id)
        receipts.append({'call_id': call_id, 'actor': actor, 'tool': function['name'],
                         'arguments': function.get('arguments'),
                         'returned': returned is not None,
                         'result': returned.get('content') if returned else None})
    projection = {'schema': 'public-internal-receipts-v1',
                  'initial_context': session.get('executor_public_context', {}),
                  'calls': receipts,
                  'interpretation': 'Untrusted actual public observations. A call, return or stored artifact does not by itself prove semantic correctness. Framework actors are not Executor calls.'}
    encoded = json.dumps(projection, ensure_ascii=False)
    if len(encoded) > 320_000:
        raise ValueError('Public receipt view exceeds budget; retain full raw evidence for reconciliation')
    return encoded
