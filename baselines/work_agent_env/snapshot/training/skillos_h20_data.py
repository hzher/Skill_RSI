"""Reviewed task groups, exact sampled fragments and native application-state binding."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pickle
import random

from evaluation.firewall import EvaluationFirewall
from integrations.benchmark_tasks import digest, validate_task_set
from training.seed_contract import prompt_digest
from training.skillos_h20_config import paths
from training.skillos_h20_state import atomic, checked, file_ref, read, State


def check_groups(admission, plan, config):
    if (set(admission) != {'schema_version', 'review_id', 'task_set', 'groups', 'provenance',
                           'evaluation_manifest', 'training_registry'}
            or admission['schema_version'] != 'workagent-skillos-group-admission-v1'
            or not isinstance(admission['review_id'], str) or not admission['review_id'].strip()):
        raise ValueError('Explicit reviewed SkillOS group admission required')
    manifest, registry = checked(admission['evaluation_manifest']), checked(admission['training_registry'])
    if (admission['evaluation_manifest'] != file_ref(config['verified_manifest'])
            or admission['training_registry'] != file_ref(config['training_registry'])):
        raise ValueError('Task review uses another evaluation/training inventory')
    guard = EvaluationFirewall(manifest, registry)
    task_set = validate_task_set(checked(admission['task_set']))
    if task_set['tasks_sha256'] != plan['tasks_sha256'] or admission['groups'] != plan['groups']:
        raise ValueError('Reviewed groups differ from the actual collector plan')
    if plan['engineering'] or plan['expected_groups'] != 32:
        raise ValueError('A one-group engineering diagnostic cannot become a formal update')
    tasks = {task['uid']: task for task in task_set['tasks']}
    selected = {uid for group in plan['groups'] for uid in group['source_record']['ordered_task_ids']}
    if set(admission['provenance']) != selected:
        raise ValueError('Group admission does not cover exactly the selected tasks')
    for group in plan['groups']:
        source = group['source_record']
        if len(source['ordered_task_ids']) != len(set(source['ordered_task_ids'])) or len(source['ordered_task_ids']) != 10:
            raise ValueError('Formal task sequence must have ten distinct tasks')
        for uid, sha in zip(source['ordered_task_ids'], source['task_input_sha256'], strict=True):
            task, provenance = tasks[uid], admission['provenance'][uid]
            if (not isinstance(provenance.get('task_family'), str) or not provenance['task_family'].strip()
                    or any(not isinstance(provenance.get(key), list) or any(not isinstance(x, str) or not x.strip()
                        for x in provenance[key]) for key in ('ancestor_tasks', 'ancestor_families'))):
                raise ValueError('Task family/ancestry review is incomplete')
            guard.check_training_record(provenance)
            if (digest(task) != sha or provenance['seed_sha256'] != sha
                    or provenance['task_id'] != task['task_id']
                    or provenance['source_commit'] != task['source']['revision'].split('+', 1)[0]
                    or provenance['prompt_sha256'] != prompt_digest(task['prompt'], {
                        'input': [{'role': 'user', 'content': task['prompt']}]})):
                raise ValueError('Reviewed public task identity differs from collected input')
    return task_set


def check_sample_sources(upstream, batch):
    """Check normalized AND original HTTP evidence; never infer tokens from text."""
    binding_ref = batch.get('role_bindings') or batch['source_binding'].get('role_bindings')
    if binding_ref is None:
        raise ValueError('Actual role bindings are missing from the sampled collection')
    binding = checked(binding_ref)
    role_config = checked(binding['roles_config'])
    plan = batch.get('plan') or batch['source_binding']['plan']
    for fragment in batch['fragments']:
        raw = checked({'path': fragment['source_curator_raw'], 'sha256': fragment['source_curator_raw_sha256']})
        value, probabilities = upstream.roles.validate_response(raw, fragment['prompt_token_ids'], True)
        context, request = raw['context'], raw['request']
        choice = value['choices'][0]
        if (raw.get('state') != 'COMPLETE' or raw.get('request_id') != 'trainer-' + digest(context)
                or context['role'] != 'curator' or context['policy_version'] != batch['policy_version']
                or context['group_id'] != fragment['group_id'] or context['rollout'] != fragment['rollout']
                or context['position'] != fragment['position'] or context['run_id'] != batch['run_id']
                or context['update_index'] != plan['stream_cursor']
                or context['domain'] != 'Toolathlon'
                or request['model'] != role_config['roles']['curator']['served_model']
                or choice['token_ids'] != fragment['response_token_ids']
                or probabilities != fragment['sampled_model_raw_logprobs']
                or probabilities != fragment['sampled_behavior_logprobs']
                or request.get('prompt') != fragment['prompt_token_ids']
                or any(request.get(k) != v for k, v in {'temperature': 1, 'top_p': 1, 'top_k': -1,
                    'min_p': 0, 'repetition_penalty': 1, 'presence_penalty': 0, 'frequency_penalty': 0,
                    'max_tokens': 4096, 'logprobs': 0}.items()) or 'response_format' in request):
            raise ValueError('Curator sample/identity/behavior evidence changed')
        original = checked(raw['transport_raw'])
        if (original['http_status'] != 200 or original['context'] != context or original['request'] != request
                or original['prompt_ids'] != fragment['prompt_token_ids']):
            raise ValueError('Original HTTP binding differs from the normalized sample')
        original_value = json.loads(original['response_text'])
        if len(original_value.get('choices', [])) != 1:
            raise ValueError('Original HTTP sample count differs')
        original_choice = original_value['choices'][0]
        if (original_value.get('prompt_token_ids', original_choice.get('prompt_token_ids')) != fragment['prompt_token_ids']
                or original_choice.get('token_ids') != fragment['response_token_ids']
                or original_choice['finish_reason'] != choice['finish_reason']
                or (original_choice.get('logprobs') or {}).get('token_logprobs') != probabilities
                or (original_choice.get('logprobs') or {}).get('tokens') != [
                    'token_id:' + str(token) for token in fragment['response_token_ids']]):
            raise ValueError('Actual HTTP token/probability evidence differs')
        record = checked(fragment['source_record'])
        if record['curator_sample_token_ids'] != fragment['response_token_ids']:
            raise ValueError('Original position/token identity differs')
        # All role/executor artifacts saved by the original loop remain bound.
        for reference in record['raw_responses']:
            checked(reference)


def prepare(config, *, run_id, collection, admission_file, lineage_file, role_binding_file, project_root):
    from training.skillos_h20_runtime import Runtime
    from training.skillos_h20_sources import Sources
    sources = Sources(config)
    upstream = sources.upstream
    lineage = read(lineage_file)
    step = lineage['global_step']
    if lineage['run_id'] != run_id or lineage['domain'] != 'Toolathlon' or lineage['policy_version'] != f'V{step:03d}':
        raise ValueError('Native lineage/run/policy mismatch')
    location = paths(config, run_id, step)
    work = Path(location['work'])
    if work.exists():
        raise ValueError('Native update directory already exists; no implicit overwrite/replay')
    batch = read(Path(collection) / 'collected_batch.json')
    identity = checked(file_ref(Path(collection) / 'identity.json'))
    plan = identity['plan']
    if batch['source_binding'] != identity or batch['run_id'] != run_id or batch['policy_version'] != lineage['policy_version']:
        raise ValueError('Collector identity/policy differs from native lineage')
    if plan['stream_cursor'] != step:
        raise ValueError('Collector update cursor differs from native lineage')
    admission = read(admission_file)
    check_groups(admission, plan, config)
    rows, totals, summary = upstream.actor_contract.validate_batch(batch, batch['policy_version'], 32, False)
    upstream.actor_contract.verify_reward_sources(batch, rows)
    check_sample_sources(upstream, batch)
    bindings = read(role_binding_file)
    if identity.get('role_bindings') != file_ref(role_binding_file):
        raise ValueError('Collection did not bind the supplied actual role proofs')
    Runtime.check_role_bindings(bindings, config, lineage, identity)
    repos = []
    for group in plan['groups']:
        for slot in range(8):
            path = Path(collection) / 'groups' / group['group_id'] / ('rollout' + str(slot)) / 'repo-after9.json'
            repo = upstream.reward.SkillRepo.load(path)
            record = read(path.with_name('position9.json'))
            if repo.digest() != record['repository_after_sha256']:
                raise ValueError('Final repo differs from committed last position')
            repos.append({'group_rollout': group['group_id'] + '/' + str(slot), 'last_committed_position': 9,
                          'canonical_content': repo.canonical_bytes().decode(), 'sha256': repo.digest()})
    work.mkdir(parents=True, exist_ok=False)
    (work / 'collection').mkdir()
    config_path = work / 'deployment.json'
    atomic(config_path, config, new=True)
    batch = deepcopy(batch)
    batch.update(status='sealed_for_native_actor', training_admitted=True, update_index=step,
        global_step_before=step, next_policy_version=f'V{step+1:03d}', source_actor_checkpoint=file_ref(lineage_file),
        admission=file_ref(admission_file), role_bindings=file_ref(role_binding_file), plan=plan)
    atomic(location['batch'], batch, new=True)
    app = {'run_id': run_id, 'domain': 'Toolathlon', 'training_seed': 42,
        'global_step': step, 'update_index': step, 'policy_version': lineage['policy_version'],
        'next_policy_version': f'V{step+1:03d}', 'phase': 'BATCH_SEALED',
        'sealed_batch_sha256': file_ref(location['batch'])['sha256'], 'stream_sha256': digest(plan),
        'source_actor_checkpoint': file_ref(lineage_file), 'skill_repositories': repos,
        'role_versions': bindings['role_versions'],
        'applied_update_uuids': lineage.get('applied_update_uuids', []),
        'rng_state': {'python': base64.b64encode(pickle.dumps(random.Random(42).getstate())).decode(),
                      'role_seeds': [group['rollout_seeds'] for group in plan['groups']],
                      'rank_torch_rng': 'owned by original FSDP full native checkpoint'}}
    atomic(location['app'], app, new=True)
    for folder in ('actor', 'validation', 'export'):
        (work / folder).mkdir()
    for folder in ('checkpoints', 'serving-policies', 'final-models', 'io-tests'):
        (Path(config['storage_root']) / folder).mkdir(parents=True, exist_ok=True)
    (Path(config['control_root']) / 'checkpoints').mkdir(parents=True, exist_ok=True)
    sources_root = Path(project_root)
    source_files = [p for p in (sources_root / 'training').glob('skillos_h20*.py')]
    source_files += [sources_root / name for name in ('scripts/skillos_h20_worker.py', 'scripts/train_skillos_h20.py',
        'scripts/serve_skillos_content.py', 'scripts/generator_python.sh', 'configs/skillos-content.requirements.in')]
    source_files += [Path(config[name]) for name in ('roles_file', 'executor_config', 'verified_manifest', 'training_registry')]
    source_files += list((sources_root / 'integrations/skill_generator/skillos').glob('*.py'))
    context = {'schema_version': 'workagent-skillos-h20-context-v1', 'run_id': run_id, 'step': step,
        'config': file_ref(config_path), 'batch': file_ref(location['batch']), 'app': file_ref(location['app']),
        'lineage': file_ref(lineage_file), 'admission': file_ref(admission_file),
        'role_bindings': file_ref(role_binding_file), 'collection_identity': file_ref(Path(collection) / 'identity.json'),
        'source_files': [file_ref(path) for path in sorted(set(source_files))], 'paths': location,
        'summary': summary, 'gpu_runtime_verified': False}
    atomic(location['context'], context, new=True)
    State(work).create(file_ref(location['context']))
    # Re-run the same complete CPU admission used at each worker boundary.
    Runtime(location['context']).load_contract()
    return context
