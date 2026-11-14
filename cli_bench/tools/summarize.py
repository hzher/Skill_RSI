"""Recompute per-episode and per-task native outcomes from the exported episodes."""
from collections import Counter, defaultdict
import csv
import io
import json
import statistics

from export import PACK, dump, fs


def distribution(values):
    values = [float(v) for v in values if isinstance(v, (float, int)) and not isinstance(v, bool)]
    return {'count': len(values), 'sum': sum(values), 'mean': statistics.mean(values),
            'median': statistics.median(values), 'min': min(values), 'max': max(values)} if values else {'count': 0}


def summarize():
    with (PACK/'reports/all-episodes.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    metrics = []
    grouped = defaultdict(list)
    for row in rows:
        episode = json.loads(fs(PACK/row['episode_path']).read_text(encoding='utf-8-sig'))
        outcome = episode['native_outcome']
        meta = outcome.get('metadata') or {}
        replayed = episode.get('replayed_prefix_interactions', 0)
        item = {'role': row['role'], 'task': row['task'], 'run_id': row['run_id'],
                'phase': row['phase'], 'instance_index': row['instance_index'], 'instance_id': row['instance_id'],
                'native_success': outcome.get('success'), 'native_reward': outcome.get('reward'),
                'raw_metric_name': outcome.get('raw_metric_name'), 'raw_metric_value': outcome.get('raw_metric_value'),
                'raw_metric_higher_is_better': outcome.get('raw_metric_higher_is_better'),
                'grading_status': episode['grading_status'], 'native_eval_status': meta.get('eval_status'),
                'native_timed_out': meta.get('timed_out'), 'native_steps': meta.get('steps'),
                'completed_interactions_including_replay': len(episode['interactions']),
                'replayed_prefix_interactions': replayed,
                'new_action_feedback_turns': len(episode['interactions']) - replayed,
                'evidence_kind': episode['evidence_kind'],
                'latency_seconds': outcome.get('latency_seconds'), 'estimated_cost_usd': outcome.get('cost_usd'),
                'resume_from_run': episode.get('resume_from_run'),
                'full_submission_captures': len(episode.get('full_submission_evidence',{}).get('matching_captures',[])),
                'episode_path': row['episode_path'], 'exported_episode_sha256': row['exported_episode_sha256']}
        metrics.append(item)
        grouped[(row['role'], row['task'], row['phase'])].append((item, meta))
    groups = []
    for (role, task, phase), items in sorted(grouped.items()):
        selected = [i for i,_ in items]
        valid = [i for i in selected if isinstance(i['native_success'], bool)]
        failures = [i for i in selected if i['native_success'] is False]
        metrics_by_name = defaultdict(list)
        for item in selected:
            metrics_by_name[item['raw_metric_name']].append(item['raw_metric_value'])
        groups.append({'role': role, 'task': task, 'phase': phase, 'records': len(selected),
                       'success_true': sum(i['native_success'] is True for i in selected),
                       'success_false': len(failures), 'success_unavailable': len(selected)-len(valid),
                       'native_success_rate': sum(i['native_success'] is True for i in valid)/len(valid) if valid else None,
                       'reward': distribution([i['native_reward'] for i in selected]),
                       'raw_metrics': {str(k): distribution(v) for k,v in metrics_by_name.items()},
                       'latency_seconds': distribution([i['latency_seconds'] for i in selected]),
                       'framework_estimated_cost_usd': distribution([i['estimated_cost_usd'] for i in selected]),
                       'new_action_feedback_turns': sum(i['new_action_feedback_turns'] for i in selected),
                       'replayed_prefix_turns': sum(i['replayed_prefix_interactions'] for i in selected),
                       'evidence_kind_counts': dict(Counter(i['evidence_kind'] for i in selected)),
                       'eval_status_counts': dict(Counter(str(i['native_eval_status']) for i in selected)),
                       'native_timed_out_true': sum(i['native_timed_out'] is True for i in selected),
                       'native_metadata_keys_present': dict(Counter(k for _,m in items for k in m)),
                       'failure_instances': [{'instance_id': i['instance_id'], 'run_id': i['run_id'],
                                              'eval_status': i['native_eval_status'], 'timed_out': i['native_timed_out'],
                                              'native_reward': i['native_reward'], 'episode_path': i['episode_path']}
                                             for i in failures]})
    report = {'groups': groups, 'indexed_records': len(metrics),
              'notes': ['Native success is authoritative; no new scoring or model calls.',
                        'Task rewards/raw metrics have different scales and must not be pooled as accuracy.',
                        'Action/feedback turns are not HTTP request counts; cost is an estimate, not billing.',
                        'Null original metadata stays null; no inferred failure causes.',
                        'Auxiliary interrupted Codebase records are excluded from the primary summary.']}
    dump(PACK/'reports/detailed-statistics.json', report)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(metrics[0]), lineterminator='\n')
    writer.writeheader(); writer.writerows(metrics)
    (PACK/'reports/episode-metrics.csv').write_text(stream.getvalue(), encoding='utf-8')
    lines = ['# 最终原生评分与过程统计', '',
             '来源为本目录已导出的逐题原生评分。主采集按固定题序统计，旧中断批次单列。', '',
             '| 范围 | 场景 | 阶段 | 评分记录 | 原生成功 | 原生成功率 | 平均 reward | 新动作/反馈轮数 |',
             '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for group in groups:
        lines.append(f"| {group['role']} | {group['task']} | {group['phase']} | {group['records']} | {group['success_true']} | {group['native_success_rate']:.2%} | {group['reward']['mean']:.6f} | {group['new_action_feedback_turns']} |")
    lines += ['', '主采集共 320 条评分记录，含 Codebase 两个阶段；覆盖 301 个唯一实例。另有 2 条旧 Codebase 辅助记录，不能加入主采集分母。', '',
              '逐题 reward、原始指标、成功判定、耗时、估算成本、重放、恢复来源、完整补丁数量及轨迹位置见 [episode-metrics.csv](episode-metrics.csv)。', '',
              '各组 reward/耗时/估算成本的数量、总和、均值、中位数、最小值、最大值、失败题清单和原生状态字段见 [detailed-statistics.json](detailed-statistics.json)。缺失失败原因保持空值，不补写推测。', '',
              'Poker 的 16 条对手先弃牌记录没有模型交互，保留原生手牌历史和评分。Cohort 的 2 轮重放前缀单列；共 991 轮新增动作/反馈不是 991 次 API 请求。', '',
              '本结果来自一次覆盖性采集，源版本为未合并候选；不代表共享 RSI 平台的实跑结果，也不代表正式跨模型对照实验。']
    (PACK/'reports/RESULTS.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'episode_metrics': len(metrics), 'groups': len(groups), 'model_calls': 0}))


if __name__ == '__main__':
    summarize()
