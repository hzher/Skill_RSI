"""Wheel-contained contracts for autonomous component authors."""

from importlib.resources import files
from pathlib import Path
import shlex
import sys

from ..evidence.io import write_bytes, write_json
from .snapshot import fingerprints

_GUIDE = """# Harness 扩展开发参考

你可以自主选择策略、算法和载体，也可以在证据不足时不生成包。这里仅规定
框架如何识别、打包和交付扩展，不预设研究结论，不要求 Skill 使用固定标题。

在 workspace/components/<组件目录>/ 放 manifest.json、payload/、README.md、review.json。
可以带代码、配置、合成样例和证据说明；在 workspace/REPORT.md 自由说明发现与结果。
README 面向使用者，解释功能、场景、何时跳过、限制、安装和宿主要求。
review.json 的字段为 display_name、description、category（自由分类）、use_when（字符串数组）、
skip_when（字符串数组）、mechanism、limitations（字符串数组）、evaluation_plan（字符串数组）。
这些是人审信息，不能当作效果已经成立的证据。不得把私密轨迹、凭据、原题答案复制进包。

## 清单与运行协议

manifest.json 使用 skill-rsi.component.v1。component_id、version 是稳定标识；form 可选
skill/plugin/mcp/hook/gateway/middleware；engine 可选 skill/python/native/wasm。
entrypoint 指向包内普通文件，不能使用绝对路径、上级路径或符号链接。
模板可省略 files 和 artifact_digest 两项，pack 会计算所有文件摘要并冻结；也可提供正确的摘要。
required_capabilities 按 all/shadow/suggest/apply 声明；scope 声明 hosts/modes及可选版本要求；
trigger.boundaries 指定生命周期；action.kind/writes/target 声明动作范围；budget 声明预算，fallback 必须为 native。
manifest-examples.json 仅示范格式，可以自由改写；协议细节见 reference/ 中当前 wheel 的实际源码。

Python PolicyContext 提供 event、state、options、budget、operation_key、受限 read_origin、
artifact_file 和 store_original。propose(context) 返回 reason、可选 action 和 state；action 包含
kind、target、payload，并符合 manifest 与宿主当前能力。返回 None 可跳过。状态必须声明 state_version。
RuntimeEvent 提供 host/event_id/boundary/session_id/payload/input_version/trigger_ref 等。

## 当前执行边界

能打包安装不代表宿主能执行，更不代表策略有效。Skill engine 使用 payload/SKILL.md 自由正文；
其交付位置仍取决于宿主当前实际支持的能力。任意新 Python 文件可以静态打包，但当前仅 wheel
内可信原字节实现有直接 Python 执行授权，不能把新 Python 候选宣传成已经可执行。
其他 native/MCP/hook/gateway 形式需要相应宿主执行器或接线；manifest 的 form 本身不会创建执行器。

Wasm 使用 skill-rsi.middleware-stdio.v1，当前需要 POSIX 上可用的 Wasmtime CLI。
输入 stdin 是一行 JSON：{abi,method:'decide',event,state,options}；输出 stdout 是一份 JSON
对象，结构同策略 propose。模块为 WASI command，调用参数为 decide，不能预打开项目目录，
不继承环境、凭据或网络；当前有内存、fuel、输出、时限预算。实际执行器见 reference/wasm.py。
静态打包不会运行 Wasm，也不等于 ABI 已通过执行验收。

host-capability-ceilings.json 只是适配器声明的能力上限，不证明某用户项目已加载对应节点。
宿主真实加载、sandbox 和版本兼容需用户后续单独核对。候选的效果由人选择独立任务判断。

## 仅做静态检查

可运行 `python -m skill_rsi.emergence.check components/<目录> --output artifacts/<名字>.zip`。
它仅检查清单、文件布局、人审信息并 pack/read_component，不导入执行 payload、不安装、不启用。
模型可以写自己的辅助脚本或合成检查，但须在报告中区分自己做过的检查、框架静态校验和未做的独立效果评估。
最后由人审阅 REPORT.md、每包 README/review.json 和候选 ZIP，手动决定安装、绑定与启用。
"""


def write_sdk(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    guide = _GUIDE.replace("`python -m skill_rsi.emergence.check", "`" + shlex.quote(sys.executable) + " -m skill_rsi.emergence.check")
    guide += ("\n## Python开发环境\n\n所有Python辅助脚本、合成检查和测试均使用绝对解释器 `"
              + sys.executable + "`；不要依赖裸python/python3。结构化配置见 environment.json。\n")
    write_bytes(directory / "SDK.md", guide.encode())
    write_json(directory / "environment.json", {"schema": "skill-rsi.emergence-environment.v1",
                                               "python_executable": sys.executable,
                                               "usage": "Use this absolute interpreter for every Python helper script and test."})
    base = {"schema": "skill-rsi.component.v1", "version": "0.1.0",
            "required_capabilities": {"apply": ["append_context"]},
            "scope": {"hosts": ["codex"], "modes": ["shadow", "apply"]},
            "trigger": {"boundaries": ["before_action"]},
            "action": {"kind": "append_context", "writes": ["context.example"]},
            "budget": {"max_action_bytes": 8192, "max_state_bytes": 65536, "max_decision_ms": 250},
            "fallback": "native"}
    examples = [{**base, "component_id": "example-skill", "form": "skill", "engine": "skill", "entrypoint": "payload/SKILL.md"},
                {**base, "component_id": "example-python", "form": "plugin", "engine": "python", "entrypoint": "payload/policy.py"},
                {**base, "component_id": "example-wasm", "form": "middleware", "engine": "wasm", "entrypoint": "payload/policy.wasm"}]
    write_json(directory / "manifest-examples.json", examples)
    from ..adapters.registry import available, get_adapter
    write_json(directory / "host-capability-ceilings.json", {
        name: {"nodes": get_adapter(name).nodes, "live_support_verified": False} for name in available()})
    for name in ("components/manifest.py", "runtime/model.py", "runtime/policy.py", "adapters/wasm.py"):
        target = directory / "reference" / name
        write_bytes(target, files("skill_rsi").joinpath(name).read_bytes())
    return {"files": fingerprints(directory)}
