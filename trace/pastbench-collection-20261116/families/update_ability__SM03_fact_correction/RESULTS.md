# PAST-Bench full-suite results

{
  "attempted": 7,
  "execution_complete": 7,
  "pipeline_complete": 7,
  "passed": 5,
  "failed": 2,
  "unknown": 0,
  "skill_reads": 0,
  "memory_writes": 1,
  "session_searches": 0
}

| Family | Attempts | Pass | Fail | Unknown | Mean task_score | Skill reads |
|---|---:|---:|---:|---:|---:|---:|
| update_ability/SM03_fact_correction | 7 | 5 | 2 | 0 | 0.8857 | 0 |

## Individual traces

- [SM03_COLD_001](traces/update_ability/SM03_fact_correction/SM03_COLD_001.md) — 0
- [SM03_UPDATE_001](traces/update_ability/SM03_fact_correction/SM03_UPDATE_001.md) — 1
- [SM03_EVAL_NEAR_001](traces/update_ability/SM03_fact_correction/SM03_EVAL_NEAR_001.md) — 1
- [SM03_EVAL_FAR_001](traces/update_ability/SM03_fact_correction/SM03_EVAL_FAR_001.md) — 1
- [SM03_CONTROL_003](traces/update_ability/SM03_fact_correction/SM03_CONTROL_003.md) — 0
- [SM03_CONTROL_002](traces/update_ability/SM03_fact_correction/SM03_CONTROL_002.md) — 1
- [SM03_CONTROL_001](traces/update_ability/SM03_fact_correction/SM03_CONTROL_001.md) — 1

## Skills (exact original model/seed bodies)

- [phoenix-rollout-review 64cb19ccd20e](skills/phoenix-rollout-review--64cb19ccd20e.md) — provided_seed
- [phoenix-rollout-review 6fdf4f662d62](skills/phoenix-rollout-review--6fdf4f662d62.md) — candidate
- [phoenix-rollout-review 18ba642ae9ca](skills/phoenix-rollout-review--18ba642ae9ca.md) — candidate
- [phoenix-rollout-review 502eef654815](skills/phoenix-rollout-review--502eef654815.md) — candidate
- [share-source-note-with-action-owners 7608c7422377](skills/share-source-note-with-action-owners--7608c7422377.md) — candidate
- [review-and-share-action-notes 3f84172e463a](skills/review-and-share-action-notes--3f84172e463a.md) — candidate
- [summarize-and-share-coordination-actions 71e8465ee140](skills/summarize-and-share-coordination-actions--71e8465ee140.md) — candidate

No causal skill-effect claim, independent downstream evaluation or weight update. Unknown scores remain null.
