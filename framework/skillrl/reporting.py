"""Count every persisted provider attempt, including failed and invalid responses."""
import json
from .storage import load

def costs(root):
    patterns = {'executor': ['episodes/*/actor/requests/*/intent.json', 'episodes/*/actor/native-requests/*/provider/intent.json', 'episodes/*/actor/wire/*/request.json'],
                'curator': ['episodes/*/curator/requests/*/intent.json'],
                'judge': ['evaluation/*/review/*/finding/intent.json', 'evaluation/*/review/*/effect/intent.json'],
                'objective_judge': ['evaluation/*/objective/judge-requests/*/intent.json']}
    result = {}
    for role, expressions in patterns.items():
        paths = {path.parent for expression in expressions for path in root.glob(expression)}
        incoming = outgoing = missing = complete = invalid = known_unsent = 0
        for path in paths:
            usage = None
            if (path / 'result.json').exists():
                complete += 1
                usage = load(path / 'result.json').get('usage')
            elif (path / 'response.raw').exists():
                invalid += 1
                try:
                    usage = json.loads((path / 'response.raw').read_bytes()).get('usage')
                except (ValueError, UnicodeError):
                    pass
            elif (path / 'failure.json').exists() and load(path / 'failure.json').get('kind') == 'known_unsent':
                known_unsent += 1
                continue
            usage = usage or {}
            inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(inp) is int and inp >= 0 and type(out) is int and out >= 0:
                incoming += inp
                outgoing += out
            else:
                missing += 1
        result[role] = {'request_attempts': len(paths), 'known_unsent': known_unsent,
                        'complete_responses': complete, 'invalid_complete_responses': invalid,
                        'input_tokens': incoming if not missing else None,
                        'output_tokens': outgoing if not missing else None, 'known_input_lower_bound': incoming,
                        'known_output_lower_bound': outgoing, 'missing_usage': missing, 'money': None}
    return result
