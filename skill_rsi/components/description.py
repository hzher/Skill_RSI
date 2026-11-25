"""Human-facing package descriptions without loading their executable payloads."""

import json
from pathlib import Path
import re

from .manifest import relative_file


BUILTIN = {
    "tool-result-view": {
        "display_name": "工具结果按需展开", "category": "工具结果管理",
        "description": "保存长日志原文，先展示错误附近片段，需要时按引用展开。",
        "use_when": ["长工具输出中已有可定位的错误，需要聚焦排障。"],
        "skip_when": ["输出较短、没有错误位置、工具失败或无法保存原文。"],
    },
    "constraint-check": {
        "display_name": "旧约束核验", "category": "约束与版本管理",
        "description": "使用旧配置值前，对照当前来源字段与版本。",
        "use_when": ["下一步依赖先前记录的配置字段，需要确认它仍然适用。"],
        "skip_when": ["当前动作不依赖该字段，或同版字段已在当前上下文中核实。"],
    },
}


def _text(path, limit=131072):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _skill_description(text):
    """Present existing authored sections; do not infer a policy from its code."""
    title = re.search(r"(?m)^#\s+(.+)$", text)
    result = {"display_name": title[1].strip(), "description": title[1].strip()} if title else {}
    header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if header:
        for key, value in re.findall(r"(?m)^(name|description):\s*(.+)$", header[1]):
            value = value.strip().strip("\"'")
            if value and value not in {"|", ">"}:
                if key == "description":
                    result["description"] = value
                else:
                    result.setdefault("display_name", value)
    sections = re.split(r"(?m)^#{1,6}\s+([^\n]+)\n", text)
    labels = {"触发条件": "use_when", "适用场景": "use_when", "when to use": "use_when",
              "跳过条件": "skip_when", "不适用场景": "skip_when", "when to skip": "skip_when"}
    for heading, body in zip(sections[1::2], sections[2::2]):
        key = labels.get(heading.strip().casefold())
        if key and body.strip():
            result[key] = [body.strip()]
    return result


def describe(component_id, source=None):
    result = dict(BUILTIN.get(component_id, {}))
    if source is None:
        return result
    root = Path(source)
    if not root.is_dir() or root.is_symlink():
        return result
    for name in ("manifest.json", "review.json"):
        try:
            data = json.loads(_text(root / name))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if name == "manifest.json" and data.get("form") == "skill":
            try:
                entry = root / relative_file(data.get("entrypoint"))
                if entry.resolve().is_relative_to(root.resolve()):
                    result.update(_skill_description(_text(entry)))
            except (OSError, ValueError):
                pass
        for key in ("display_name", "description", "category", "purpose", "use_when", "skip_when", "mechanism", "limitations", "evaluation_plan"):
            value = data.get(key)
            if isinstance(value, str) or (isinstance(value, list) and all(isinstance(item, str) for item in value)):
                result[key] = value
    readme = _text(root / "README.md")
    if readme:
        result.update(readme=readme, readme_path=str(root / "README.md"))
    result.update(source_path=str(root), storage_path=str(root))
    result.setdefault("validation_status", "installed")
    return result
