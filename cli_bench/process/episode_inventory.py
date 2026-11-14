"""Publish one cross-run index of actual scored episodes; never call a model."""
import csv
import io
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLES = {"codebase_adaptation": "Codebase", "sales_prediction": "Sales", "database_exploration": "Database",
          "cohort_studies": "Cohort", "blind_spectrum_monitoring": "BSM", "exploitable_poker": "Poker"}


def atomic_write(path, content, encoding="utf-8"):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(status):
    rows = []
    batches = []
    for run in sorted((ROOT / "runs/collections").iterdir()):
        path = run / "audit/collection-index.json"
        if not path.exists():
            continue
        index = json.loads(path.read_text(encoding="utf-8-sig"))
        plan = json.loads((run / "control/bridge-plan.json").read_text(encoding="utf-8-sig"))
        count = 0
        for entry in index["episodes"]:
            rows.append({"task": index["task"], "run_id": run.name, "phase": entry["phase"],
                         "instance_index": entry["instance_index"], "instance_id": entry["instance_id"],
                         "completed_interactions": entry["completed_interactions"],
                         "evidence_kind": entry.get("evidence_kind", "model_interaction" if entry["completed_interactions"] else "unclassified"),
                         "native_success": entry.get("success"), "native_reward": entry.get("reward"),
                         "episode_path": f"runs/collections/{run.name}/{entry['path']}",
                         "requested_model": plan["requested_model"], "codex_cli_version": plan["effective_config"]["system"]["params"]["version"],
                         "api_base_url": plan["api_base_url"], "use_oauth": plan["effective_config"]["system"]["params"]["use_oauth"]})
            count += 1
        batches.append((index["task"], run.name, count))
    kinds = Counter(row["evidence_kind"] for row in rows)
    unique = len({(row["task"], row["instance_id"]) for row in rows})
    stream = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    atomic_write(ROOT / "reports/all-episodes.csv", stream.getvalue(), encoding="utf-8-sig")
    lines = ["# 全部批次轨迹总索引", "", f"更新时间：{status['updated_at']}。统计只包括已有原生评分并建立索引的实例；运行中的未完成题目不提前计入。", "",
             f"共覆盖 **{unique} / {status['planned_unique_instances']} 个唯一实例**，索引 **{len(rows)} 条实例记录**。",
             f"其中 {kinds['model_interaction']} 条含模型交互，{kinds['environment_auto_resolution']} 条为原生环境自动结束；重放部分不重复计数。", "",
             "一份 episode 文件表示一道题的过程，可能含多轮交互；文件数不是 API 请求次数。旧中断批次与新批次分别保留，跨批次同题重复尝试不增加唯一实例数。", "",
             "当前执行：原生 Codex CLI + 组内 AffordableRouter Responses API，配置模型 gpt-6.1-sol，use_oauth=false。按工作指导跑的是 CL-Bench（Continual Learning Bench），不是另一个 CLI-Bench 项目。", "",
             "逐条文件位置、评分、交互轮数和类型见 [all-episodes.csv](all-episodes.csv)。", "",
             "| 场景 | 已评分唯一实例 | 目标 | 索引记录数 |", "| --- | ---: | ---: | ---: |"]
    for task, item in status["tasks"].items():
        chosen = [row for row in rows if row["task"] == task]
        lines.append(f"| {TITLES.get(task,task)} | {len({r['instance_id'] for r in chosen})} | {item['planned_unique_instances']} | {len(chosen)} |")
    lines += ["", "| 场景 | 批次索引 | 本批记录数 |", "| --- | --- | ---: |"]
    for task, run_id, count in batches:
        lines.append(f"| {TITLES.get(task,task)} | [{run_id}](../runs/collections/{run_id}/audit/collection-index.json) | {count} |")
    active = any(item.get("status") == "running" for item in status.get("runs", []))
    archive_note = ("原生 CLI 事件、会话、完整提交及配置在对应批次的 native/ 和 control/；运行批次结束后封存完整导出。" if active else
                    "全部批次已结束，原生 CLI 事件、会话、完整提交及配置已封存在对应批次的 native/ 和 control/。最终覆盖与完整性结论见 collection-completion.json。")
    lines += ["", archive_note,
              "包含未评分中断、恢复关系、验证样本和压缩历史记录的位置见 [原始证据总索引](raw-evidence-inventory.json)。",
              "评分数量不等于通过数量；目标模型名称不代表上游实际模型身份已被独立验证。"]
    atomic_write(ROOT / "reports/trajectory-inventory.md", "\n".join(lines) + "\n")
    return {"unique_instances": unique, "indexed_records": len(rows), "evidence_kinds": dict(kinds)}


if __name__ == "__main__":
    current = json.loads((ROOT / "reports/coverage-status.json").read_text(encoding="utf-8-sig"))
    print(json.dumps(publish(current)))
