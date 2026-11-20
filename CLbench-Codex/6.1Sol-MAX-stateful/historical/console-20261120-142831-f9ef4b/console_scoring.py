"""Read saved native verdicts for display without changing frozen shared scores."""
import math

from console_common import read, digest, UserError

PINNED_BENCHMARK = '5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26'
LOCAL_INDEX_FAMILIES = {'cohort_studies', 'database_exploration'}


def native_verdict(case, result, task):
    empty = {'native_success': None, 'native_reward': None, 'native_metric': None,
             'native_raw_metric_name': None, 'native_raw_metric_value': None,
             'native_binding': 'unavailable', 'native_binding_note': '尚无可关联的原生回执',
             'native_receipt_sha256': None}
    if not result:
        return {**empty, 'native_binding': 'pending'}
    try:
        identity = result.get('identity') or {}
        link = read(case/'scoring-link.json')
        receipt = read(case/'native-objective.json')
        expected = (identity.get('task_id') == task['id'] and identity.get('family') == task['family']
                    and identity.get('canonical_index') == task['canonical_index']
                    and bool(identity.get('attempt_id')) and bool(identity.get('instance_id')))
        if not expected or not isinstance(link, dict) or not isinstance(receipt, dict):
            return {**empty, 'native_binding_note': '任务身份或评分关联文件缺失'}
        sha = digest(case/'native-objective.json')
        if not (identity == link.get('identity') == read(case/'identity.json')
                and link.get('query_instance_id') == identity['instance_id']
                and sha == link.get('verifier_receipt_sha256')
                and digest(case/'artifact-manifest.json') == link.get('artifact_manifest_sha256') == result.get('artifact_manifest_sha256')
                and receipt == result.get('objective')):
            return {**empty, 'native_binding_note': '身份、回执或产物清单摘要不一致'}
        outcomes = receipt.get('instance_outcomes') or []
        if len(outcomes) != 1:
            return {**empty, 'native_binding_note': '本次尝试没有唯一的原生评分结果'}
        outcome = outcomes[0]
        idx = outcome.get('instance_index')
        exact = outcome.get('instance_id') == identity['instance_id']
        binding = None
        if exact and type(idx) is int and idx == task['canonical_index']:
            binding = 'exact'
        elif (exact and type(idx) is int and idx == 0 and task['family'] in LOCAL_INDEX_FAMILIES
              and result.get('configuration', {}).get('benchmark_commit') == PINNED_BENCHMARK):
            binding = 'pinned_single_instance_local_index'
        elif (task['family'] == 'sales_prediction' and identity.get('schedule') == 'default'
              and str(identity['instance_id']).startswith('sales_prediction:default:years:')
              and outcome.get('instance_id') == identity['instance_id'].replace('sales_prediction:default:', 'sales_prediction:', 1)
              and type(idx) is int and idx == task['canonical_index']
              and result.get('configuration', {}).get('benchmark_commit') == PINNED_BENCHMARK):
            binding = 'pinned_sales_alias'
        if binding is None:
            return {**empty, 'native_binding_note': '原生题目 ID 或题号不能安全关联'}
        reward = outcome.get('reward')
        if type(reward) not in (int, float) or not math.isfinite(reward): reward = None
        success = outcome.get('success') if type(outcome.get('success')) is bool else None
        raw_value = outcome.get('raw_metric_value')
        if type(raw_value) not in (int, float) or not math.isfinite(raw_value): raw_value = None
        return {'native_success': success, 'native_reward': reward,
                'native_metric': task['family']+':reward',
                'native_raw_metric_name': outcome.get('raw_metric_name'), 'native_raw_metric_value': raw_value,
                'native_binding': binding,
                'native_binding_note': '已核对任务身份、原生回执与产物清单摘要；共享 score 保留原值',
                'native_receipt_sha256': sha}
    except (OSError, ValueError, KeyError, TypeError, UserError):
        return {**empty, 'native_binding_note': '原生回执读取失败，未计入判定'}
