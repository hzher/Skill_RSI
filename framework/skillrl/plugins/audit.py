"""Independent consumption audit across enabled plugins."""
import json
from pathlib import Path

def audit_plugins(execution, actor_path, specifications):
    from ..agents import codex_exposures
    path = Path(actor_path) / 'runtime-plugin-events.jsonl'
    receipts = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    saved = {receipt['raw_sha256']: receipt['skill'] for receipt in receipts if receipt.get('persisted')}
    reads = [event['result'] for event in execution['events']
             if event.get('name') == 'Skill' and not event.get('error') and
             event.get('result', {}).get('name', '').startswith(('checkpoint-', 'scope-program-'))]
    restored = execution.get('runtime_restored_checkpoints', [])
    archival = [v for receipt in receipts for v in receipt.get('archival_versions', [])]
    versions = {version['raw_sha256']: version for version in [*saved.values(), *reads, *restored, *archival]}
    action_tools = sorted({name for spec in specifications for name in spec.get('action_tools', [])})
    exposures = codex_exposures(execution, list(versions.values()), actor_path, action_tools)
    read_hashes = {version['raw_sha256'] for version in reads}
    restored_hashes = {version['raw_sha256'] for version in restored}
    prompt_proofs = {}
    for request_path in sorted((Path(actor_path) / 'wire').glob('*/request.json')):
        result_path = request_path.parent / 'result.json'
        if not result_path.exists() or not json.loads(result_path.read_text()).get('response_completed'):
            continue
        request = json.loads(request_path.read_text())

        def strings(value):
            if isinstance(value, str):
                yield value
                try:
                    decoded = json.loads(value)
                    if not isinstance(decoded, str):
                        yield from strings(decoded)
                except ValueError:
                    pass
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)

        inputs = list(strings(request.get('input', [])))
        for version in [*restored, *archival]:
            if any(version['content'] in value for value in inputs):
                prompt_proofs.setdefault(version['raw_sha256'], []).append(str(request_path.relative_to(actor_path)))
    return {
        'source_actor': 'framework_plugin', 'receipts': receipts,
        'policy_activity': [{
            'plugin': spec['kind'],
            'event_counts': {status: sum(r.get('plugin') == spec['kind'] and r.get('status') == status for r in receipts)
                             for status in sorted({r.get('status', 'unknown') for r in receipts if r.get('plugin') == spec['kind']})},
            'executor_request_ids': sorted({str(r['executor_call_id']) for r in receipts
                                           if r.get('plugin') == spec['kind'] and 'executor_call_id' in r}),
            'semantic_adoption': 'unverified',
            'receipt_generation_is_not_completed_model_exposure': True,
            'causal_reward': None,
        } for spec in specifications],
        'checkpoints': [{
            'name': proof['name'], 'raw_sha256': proof['raw_sha256'],
            'saved_in_this_episode': proof['raw_sha256'] in saved,
            'read_by_executor': proof['raw_sha256'] in read_hashes,
            'restored_to_initial_prompt': proof['raw_sha256'] in restored_hashes,
            'restoration_wire_proof': prompt_proofs.get(proof['raw_sha256'], []) if proof['raw_sha256'] in restored_hashes else [],
            'exact_body_wire_proof': prompt_proofs.get(proof['raw_sha256'], []),
            'archival_fallback_attached': any(v['raw_sha256'] == proof['raw_sha256'] for v in archival),
            'body_exposed_to_completed_model_request': proof['body_proven'] or proof['raw_sha256'] in prompt_proofs,
            'followed_by_bound_business_action': any(item['subsequent_actions'] for item in proof['proof']),
            'proof': proof['proof'], 'semantic_adoption': 'unverified', 'causal_reward': None,
        } for proof in exposures],
    }
