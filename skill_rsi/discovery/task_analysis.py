"""Produce source-backed, local Skill candidates from recorded task facts."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from ..adapters.native_events import mapping, native_parts
from ..adapters.registry import available
from ..components import pack
from ..components.manifest import canonical, relative_file, sha256
from ..evidence.io import at_pointer, file_lock, inside, source_bytes, write_bytes, write_json
from ..review.projection import SECRET

_SCHEMA = "skill-rsi.task-analysis.v1"
_ALGORITHM = "local-memory-rules-v1"
_MAX_RECORDS = 2000
_MAX_BYTES = 16 * 1024 * 1024
_ERROR = re.compile(r"\b(?:fatal|error|traceback|panic|failed)\b", re.I)
_ASSERTION = re.compile(r"\b(?:AssertionError|assertion failed|FAILED\s+\S+::|expected\s+.+\s+(?:but|got))", re.I)
_GAP_REASONS = {"input_version_unknown", "source_unverified", "action_boundary_capability_missing",
                "host_capabilities_changed", "missing_original_or_target"}
_RULES = {
    "continuity": {
        "title": "恢复长程任务前核对进度与版本",
        "observation": "记录中存在会话恢复或上下文压缩边界。",
        "trigger": "恢复任务、上下文压缩后，或准备沿用先前结论时。",
        "steps": ["先核对当前文件版本和已有改动，确认旧结论依赖的内容仍然成立。",
                  "整理用户约束、未完成事项、仍失败的断言与下一步，各项保留可回查的来源。",
                  "只补读本轮动作缺少的证据，完成事项压缩为带出处的简短结论。"],
        "skip": "当前上下文已经包含上述事项且文件版本核对一致时，继续当前动作。",
    },
    "assertions": {
        "title": "接续排障时保留尚未通过的断言",
        "observation": "工具结果中记录了断言或测试失败信号。",
        "trigger": "排障中切换阶段、恢复会话，或准备再次修改相关代码时。",
        "steps": ["把仍未通过的断言、最近复验结果和当前改动的版本一起保留。",
                  "继续动作前确认失败是否仍可复现，已修复项缩成带原始结果引用的结论。"],
        "skip": "已有同一版本上的通过结果，或该失败已被明确判定与当前任务无关时。",
    },
    "logs": {
        "title": "为长日志保留原件并按需展开",
        "observation": "记录中存在长工具输出的错误窗口或日志聚焦决定。",
        "trigger": "工具成功返回至少 60 行文本，且其中存在可定位的错误词时。",
        "steps": ["保存可回查的原文引用，先阅读首个错误位置及相邻行。",
                  "将采用的结论关联到原文行号，需要解释因果时再展开附近片段。"],
        "skip": "结果较短、工具失败、找不到相关错误位置，或原件无法回查时使用完整结果。",
    },
    "repeat": {
        "title": "重复读取前核对已有结果的版本",
        "observation": "不同工具调用记录了内容摘要相同的长输出。",
        "trigger": "准备再次读取已查过的长文件或输出时。",
        "steps": ["对照读取对象、参数、文件版本和现有覆盖范围。",
                  "对象和版本一致且内容足够时沿用带出处的结果，只补查缺失片段。"],
        "skip": "文件已修改、读取范围不同、原结果不完整或当前缺少有效版本证据时重新读取。",
    },
    "constraints": {
        "title": "使用旧约束前核验来源字段",
        "observation": "记录中存在旧约束来源变化、召回或版本未确认的策略信号。",
        "trigger": "下一步操作明确依赖之前记录的配置字段时。",
        "steps": ["读取项目允许访问的当前来源字段，核对其对象、值和版本。",
                  "来源变化时以当前证据修正工作笔记，并保留原有结论与更新依据的关联。"],
        "skip": "操作不依赖该字段，或当前可见材料已包含核实过的同版字段时。",
    },
    "gaps": {
        "title": "继续动作前补齐已暴露的证据缺口",
        "observation": "策略回执记录了来源、输入版本或交付能力缺失。",
        "trigger": "准备依赖一项尚未核实的旧结论或未确认交付的策略动作时。",
        "steps": ["明确缺少的来源、版本或宿主能力，并做一次针对性的核查。",
                  "将已确认事实与待核实事项一同留在任务笔记，并记录下一次核查动作。"],
        "skip": "当前动作不依赖缺失证据，或已取得与本次动作对应的有效回执时。",
    },
}


def _read(path, limit=_MAX_BYTES):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected an ordinary recorded file: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("record_exceeds_analysis_budget")
    return raw


def _journal(task, gaps):
    root = inside(task, "journal")
    database = root / "journal.sqlite3"
    if not database.is_file():
        return []
    if database.is_symlink():
        gaps.add("journal_symlink_rejected")
        return []
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT payload FROM events ORDER BY ordinal LIMIT ?", (_MAX_RECORDS + 1,)).fetchall()
    except sqlite3.Error:
        gaps.add("journal_unavailable")
        return []
    if len(rows) > _MAX_RECORDS:
        gaps.add("journal_record_limit")
    records, total = [], 0
    for (payload,) in rows[:_MAX_RECORDS]:
        try:
            event = json.loads(payload)
            ref = event["raw_ref"]
            if type(ref.get("bytes")) is not int or not 0 <= ref["bytes"] <= _MAX_BYTES - total:
                gaps.add("journal_byte_limit")
                break
            source = inside(root, relative_file(ref["path"]))
            if source.is_symlink() or source.stat().st_size != ref["bytes"]:
                raise ValueError("source_size_mismatch")
            raw = source_bytes(root, ref)
            total += len(raw)
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("source_record_must_be_object")
            record.setdefault("type", event.get("type"))
            records.append((record, {"path": "journal/" + ref["path"], "sha256": ref["sha256"]}))
        except (OSError, ValueError, KeyError, TypeError):
            gaps.add("journal_source_unverified")
    return records


def _capture(task, gaps):
    names = ["capture/stdout.jsonl", "capture/codex-rollout.jsonl",
             "capture/capture/session-events.jsonl", "capture/capture/tools.jsonl"]
    names += [path.relative_to(task).as_posix()
              for path in sorted(task.glob("capture/threads/*/codex-rollout.jsonl"))]
    names = [name for name in names if inside(task, name).is_file()]
    if not names:
        return []
    if len(names) > _MAX_RECORDS:
        gaps.add("capture_stream_limit")
        names = names[:_MAX_RECORDS]
    records = []
    record_budget, byte_budget = _MAX_RECORDS // len(names), _MAX_BYTES // len(names)
    for name in names:
        source = inside(task, name)
        consumed = 0
        with source.open("rb") as stream:
            for number in range(1, record_budget + 2):
                raw = stream.readline(byte_budget - consumed + 1)
                if not raw:
                    break
                consumed += len(raw)
                if consumed > byte_budget or number > record_budget:
                    gaps.add("capture_analysis_limit")
                    break
                if not raw.endswith(b"\n"):
                    gaps.add("capture_partial_record")
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    gaps.add("capture_non_json_record")
                    continue
                if isinstance(record, dict):
                    records.append((record, {"path": name, "sha256": sha256(raw),
                                             "locator": {"type": "line", "line": number}}))
            else:
                gaps.add("capture_record_limit")
    return records


def _text(value, depth=0):
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("aggregated_output", "output", "stdout", "text", "content"):
            if key in value:
                return _text(value[key], depth + 1)
    if isinstance(value, list):
        return "\n".join(item["text"] for item in value
                         if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
    return ""


def _tool_results(record, host):
    if host == "dsh" and record.get("kind") == "tool_end":
        yield _text(record.get("result")), record.get("tool_call_id")
    native = mapping(record.get("native_record"))
    if native and record.get("type") in ("tool_result", "mcp_tool_result"):
        payload = mapping(native.get("payload"))
        value = native.get("tool_response", payload.get("text"))
        yield _text(value), native.get("tool_use_id") or native.get("call_id")
    if host in {"codex", "claude_code"}:
        source = native or record
        for kind, pointer, fields in native_parts(source, host):
            if kind != "tool_result":
                continue
            try:
                value = at_pointer(source, pointer)
            except (TypeError, KeyError, ValueError, IndexError):
                continue
            yield _text(value), fields.get("tool_call_id")


def _findings(records, host):
    refs = defaultdict(dict)
    outputs = defaultdict(dict)
    seen_calls = set()

    def add(signal, ref):
        refs[signal][sha256(canonical(ref))] = ref

    for record, ref in records:
        if host == "dsh" and record.get("kind") == "session_event":
            record = mapping(record.get("event"))
        elif host == "dsh" and record.get("method") == "session.event":
            record = mapping(mapping(record.get("params")).get("event"))
        native = mapping(record.get("native_record"))
        kind = record.get("type")
        if not isinstance(kind, str):
            kind = None
        compact = (kind in {"compaction", "session_resume", "compacted"}
                   or (host == "dsh" and kind in {"compaction/start", "compaction/summary", "compaction/end", "compaction/prune"})
                   or record.get("change_kind") == "context_compact"
                   or (kind == "system" and record.get("subtype") == "compact_boundary")
                   or (kind == "session_start" and native.get("source") in ("compact", "resume"))
                   or mapping(record.get("payload")).get("type") == "context_compacted")
        if host == "codex":
            compact = compact or any(kind == "context_change" and fields.get("change_kind") == "context_compact"
                                      for kind, _, fields in native_parts(native or record, host))
        if compact:
            add("continuity", ref)
        decision = mapping(record.get("decision"))
        reason = decision.get("reason")
        if not isinstance(reason, str):
            reason = None
        if reason in {"long_text_focused", "long_text_fixed_mask"}:
            add("logs", ref)
        if reason in {"source_changed", "recall_verified_constraint", "source_unverified", "conflicting_fact_for_same_revision"}:
            add("constraints", ref)
        if reason in _GAP_REASONS:
            add("gaps", ref)
        for text, call_id in _tool_results(record, host):
            if not text:
                continue
            content = sha256(text.encode())
            identity = str(call_id) if call_id is not None else sha256(canonical(ref))
            if (identity, content) in seen_calls:
                continue
            seen_calls.add((identity, content))
            if _ASSERTION.search(text):
                add("assertions", ref)
            if len(text.splitlines()) < 60:
                continue
            outputs[content][identity] = ref
            if _ERROR.search(text):
                add("logs", ref)
    for occurrences in outputs.values():
        if len(occurrences) > 1:
            for ref in occurrences.values():
                add("repeat", ref)
    return [{"signal": key, "title": rule["title"], "observation": rule["observation"],
             "evidence_count": len(refs[key]), "source_refs": list(refs[key].values())[:12]}
            for key, rule in _RULES.items() if key in refs]


def _skill(component_id, findings):
    lines = ["---", f"name: {component_id}", 'description: "基于已记录任务的长程记忆管理候选规则"', "---", "",
             "# 长程任务的记忆管理候选", "", "本版本由本地规则分析生成，效果待独立任务验证。",
             "把下面的检查用于相关动作。保留用户要求和可回查原文，按当前文件版本决定下一步。"]
    for finding in findings:
        rule = _RULES[finding["signal"]]
        lines += ["", "## " + rule["title"], "", rule["trigger"], ""]
        lines += ["- " + step for step in rule["steps"]]
        lines += ["", "跳过条件为" + rule["skip"]]
    lines += ["", "## 使用与复验", "", "来源引用见包内 evidence.json。每次采用结论时保留其文件版本和原文定位。",
              "当前安装方式每个会话最多提醒一次。压缩或跨天续接后，可重新读取本规则与任务笔记。",
              "在未参与生成的长程任务中比较启用前后结果，记录遗漏约束、失败断言、重复读取和额外调用次数。",
              "复验应包含触发样例、应跳过样例和来源不可用样例，再决定是否用于后续项目。", ""]
    return "\n".join(lines).encode()


def analyze_task(task_path, output=None) -> dict:
    """Analyze a bounded, committed local snapshot and package an untested Skill."""
    task_path = Path(task_path).expanduser().resolve(strict=True)
    task_raw = _read(inside(task_path, "task.json"), limit=1024 * 1024)
    task = json.loads(task_raw)
    if task.get("schema") != "skill-rsi.task.v1" or task.get("host") not in available():
        raise ValueError("Expected a Skill RSI task record")
    gaps = set()
    running = task.get("status") in {"queued", "running", "cancelling"} or task.get("cleanup_pending")
    records = [] if running else _journal(task_path, gaps) + _capture(task_path, gaps)
    if running:
        gaps.add("managed_task_still_running")
    if not records:
        gaps.add("no_readable_task_records")
    if task.get("mode") == "observed":
        gaps.add("model_input_visibility_unverified")
    findings = _findings(records, task["host"])
    if not findings:
        gaps.add("no_supported_memory_signal")
    source_index = [ref for _, ref in records]
    task_identity = sha256(canonical({key: task.get(key) for key in ("task_id", "host", "project")}))
    identity = sha256(canonical({"algorithm": _ALGORITHM, "host": task["host"],
                                 "task_identity_sha256": task_identity, "sources": source_index,
                                 "missing_reasons": sorted(gaps)}))
    component_id = "task-memory-" + identity[:20]
    root = Path(output).expanduser().resolve() if output is not None else task_path / "analysis"
    destination = root / identity[:20]
    status = "candidate" if findings else "inconclusive"
    summary = (f"从 {len(records)} 条记录识别出 {len(findings)} 类信号，已生成待验证的记忆管理候选。"
               if findings else f"已读取 {len(records)} 条记录，本机规则未命中预设的记忆管理信号。可以切换到 LLM 分析这份轨迹。")
    report = {"schema": _SCHEMA, "algorithm": _ALGORITHM, "status": status, "summary": summary,
              "validation_status": "not_tested", "evidence_origin": "synthesized",
              "comparison_status": "single_task_only", "input_sha256": identity,
              "snapshot_scope": "committed_prefix" if task.get("mode") == "observed" else "recorded_task",
              "host": task["host"], "record_count": len(records), "findings": findings,
              "missing_reasons": sorted(gaps), "component_id": component_id if findings else None,
              "version": "1.0.0" if findings else None}
    with file_lock(root / ".analysis.lock"):
        if not destination.is_dir():
            with TemporaryDirectory(dir=root, prefix=".analysis-") as temporary:
                stage = Path(temporary) / "result"
                stage.mkdir(mode=0o700)
                write_json(stage / "analysis.json", report)
                if findings:
                    skill = _skill(component_id, findings)
                    if SECRET.search(skill.decode()):
                        raise ValueError("Generated Skill failed the credential-pattern check")
                    write_bytes(stage / "SKILL.md", skill)
                    package = Path(temporary) / "package"
                    write_bytes(package / "payload/SKILL.md", skill)
                    write_json(package / "evidence.json", report)
                    write_json(package / "review.json", {"method": "fixed-template-and-source-hash-check",
                               "human_approved": False, "validation_status": "not_tested", "skill_sha256": sha256(skill)})
                    write_json(package / "manifest.json", {
                        "schema": "skill-rsi.component.v1", "component_id": component_id, "version": "1.0.0",
                        "form": "skill", "engine": "skill", "entrypoint": "payload/SKILL.md",
                        "scope": {"hosts": [task["host"]], "modes": ["shadow", "suggest", "apply"]},
                        "trigger": {"boundaries": ["before_action", "mcp_before_action", "before_use"]},
                        "action": {"kind": "append_context", "target": "context.skill." + component_id, "writes": []},
                        "required_capabilities": {"shadow": [], "suggest": ["suggest"], "apply": ["append_context"]},
                        "budget": {"max_action_bytes": 16384, "max_actions_per_session": 1}, "fallback": "native"})
                    pack(package, stage / "candidate.zip")
                stage.rename(destination)
    return {**report, "analysis_path": str(destination / "analysis.json"),
            "skill_path": str(destination / "SKILL.md") if findings else None,
            "artifact_path": str(destination / "candidate.zip") if findings else None}
