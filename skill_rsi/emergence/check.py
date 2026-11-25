"""Validate and pack candidate files without importing their implementation."""

import argparse
import json
from pathlib import Path
import shutil

from ..components import pack
from ..components.archive import read_component
from ..evidence.io import inside, read_json

_TEXT = ("display_name", "description", "category", "mechanism")
_LISTS = ("use_when", "skip_when", "limitations", "evaluation_plan")


def review_metadata(source):
    source = Path(source)
    readme = inside(source, "README.md")
    if not readme.is_file() or readme.stat().st_size > 8 * 1024 * 1024 or not readme.read_text().strip():
        raise ValueError("候选需要面向使用者的 README.md")
    review_path = inside(source, "review.json")
    if not review_path.is_file() or review_path.stat().st_size > 1024 * 1024:
        raise ValueError("候选需要普通JSON文件review.json（不超过1MiB）")
    review = read_json(review_path)
    if not isinstance(review, dict):
        raise ValueError("review.json 必须是对象")
    for key in _TEXT:
        if not isinstance(review.get(key), str) or not review[key].strip():
            raise ValueError("review.json 缺少文字字段：" + key)
    for key in _LISTS:
        if not isinstance(review.get(key), list) or not all(isinstance(item, str) and item.strip() for item in review[key]):
            raise ValueError("review.json 需要字符串数组：" + key)
    return {key: review[key] for key in (*_TEXT, *_LISTS)}


def validate(source, output):
    source, output = Path(source), Path(output)
    review = review_metadata(source)
    release = pack(source, output)
    manifest, _ = read_component(output)
    if manifest != release.manifest:
        raise ValueError("打包结果与校验清单不一致")
    engine = manifest.get("engine", "skill" if release.form == "skill" else "native")
    support = {"skill": "host_capability_dependent", "python": "requires_trusted_or_isolated_executor",
               "wasm": "requires_wasmtime_and_host_capabilities", "native": "requires_host_executor"}[engine]
    note = {"skill": "Skill正文已静态打包；是否能交付需核对宿主当前真实节点与能力，效果未评估。",
            "python": "新增Python候选未获得可信执行授权；当前需可信或隔离执行器，不能直接视为可运行。",
            "native": "该形式仍需宿主对应执行器和能力接线；清单和ZIP有效不代表已可执行。",
            "wasm": ("本机未发现Wasmtime；" if not shutil.which("wasmtime") else "本机发现Wasmtime，尚未运行候选；")
                    + "仍需核对WASI ABI、宿主节点和预算，效果未评估。"}[engine]
    return {**release.to_dict(), **review, "status": "valid", "engine": engine,
            "source_path": str(source), "artifact_path": str(output), "archive_path": str(output),
            "readme_path": str(source / "README.md"), "review_path": str(source / "review.json"),
            "human_review_required": True, "execution_status": "not_verified", "runtime_support": support,
            "execution_note": note, "readiness": {"packaging": "valid", "execution": "not_verified", "effectiveness": "not_evaluated"},
            "validation_status": "static_only", "effectiveness": "not_evaluated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(validate(options.source, options.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
