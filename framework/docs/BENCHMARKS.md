# Benchmark可替换接口

任务和客观评分由benchmark适配器提供，LLM价值Judge使用统一事件与版本合同。WorkBuddy可以使用其原始任务环境和原生评分器，也可以替换为其他来源。原生评分器的代码和依赖由对应benchmark版本提供。

## 使用统一公开题单

公开manifest是按执行顺序排列的数组，每项只有以下三个字段。

```json
[{"id":"original-task-id","instruction":"原始完整公开任务","public_dir":"public/original-task-id"}]
```

public_dir相对manifest解析，只允许普通文件和目录。参考答案、私有checker与其配置放在题单之外，通过独立evaluator接入。环境内只得到公开工作区及只读技能快照。

`ManifestBenchmark`适合已有工作区导出的其他数据。`JSONExactEvaluator`只供简单严格JSON案例使用，生产benchmark应按其官方评测合同编写评分适配器，原多指标可保存在details，二值奖励的转换规则由适配器明确规定。

## WorkBuddy原题接入

先固定官方版本、选题清单和各题镜像。当前导出器兼容已有源目录的`instruction.md`及`environment/workspace.tar.gz`布局，只读取这两份公开输入，不复制整个任务目录。

```sh
python3 -m skillrl.workbuddy \
  --dataset /data/workbuddy/datasets \
  --selection examples/workbuddy-selection.json \
  --out /data/workbuddy-public-export
```

如果原归档明确带一层workspace目录，可显式加 `--strip-prefix workspace`。程序保存原instruction字节SHA、原archive SHA和逐文件清单，拒绝软链、设备、路径穿越及超预算归档。不同WorkBuddy版本布局或合法软链需求应通过新适配器支持，避免擅自改变原题语义。

把`examples/workbuddy.json`中的manifest指向生成的tasks.json，images按原task id映射到各自固定镜像digest。原题执行权限由network、user和镜像共同设置，不按标签修改任务或过滤技能。

本框架不自动下载WorkBuddy、不提供其私有答案，不将派生示例冒称官方原题。导出器和评分命令协议均有独立测试。真实官方任务成绩由实际接入的原生评分器产生。

## 原生评分命令

`CommandEvaluator`在宿主侧以argv数组启动评分程序，无shell插值。stdin内容如下。

```json
{"task_id":"original-task-id","workspace":"/absolute/evaluation-copy","environment":{"image":"original-image","container_id":"archived-original-id","retired_after_archive":true}}
```

stdout严格返回一个JSON对象。

```json
{"score":1,"details":{"native_metric":1,"original_exit":0}}
```

评分器可通过自己固定的索引和依赖调用对应版本的官方verifier。调用错误或评测基础设施缺失返回score=null。命令非0由框架记录为评分器错误，保留null。

任务容器在完整工作区归档并校验后退役。评分钩子使用评测副本，按原镜像重建验证环境；传入的原container_id用于证据定位。需要工作区之外的持久环境状态或自定义浏览器/远程环境时，应通过下面的benchmark插件实现其状态封存与恢复合同。

## 实现其他benchmark

可直接实现三个方法，通过`module:factory`配置加载。

```python
from pathlib import Path
from skillrl.benchmarks import Task
from skillrl.environments import DirectoryEnvironment
from skillrl.storage import copy_public

class MyBenchmark:
    def tasks(self):
        return [Task("task-1", "完整公开任务", Path("/data/public/task-1"))]

    def prepare(self, task, workspace):
        copy_public(task.public_dir, workspace)
        return DirectoryEnvironment(workspace)

    def evaluate(self, task, workspace, execution):
        # 在此调用该benchmark真实的独立评分程序。
        return {"score": None, "details": {"status": "implement-native-verifier"}}

def create():
    return MyBenchmark()
```

```json
{"benchmark":{"kind":"plugin","factory":"my_adapter:create","options":{}}}
```

只替换评分时，evaluator也支持plugin。工厂模块须安装在当前Python环境中。`Task.id`要求稳定且唯一，公开输入必须可冻结；环境提供start/close/read/write，提供bash方法时执行器才会获得Bash工具。已有agent harness也可实现execute接口，输出同一封存事件合同，并以真实请求记录提供正文曝光证据。

## 其他工具与价值评审

可在运行配置的reviewer.action_tools中声明该环境真正执行交付或状态更改的工具名称。默认是Write、Edit、Bash。它只影响离线证据门，不改变执行器的工具选择或Skill目录。

```json
{"reviewer":{"action_tools":["Write","Edit","Bash","BrowserClick"]}}
```

每个动作仍需发生在完整正文曝光之后，并由Judge对应具体条款解释。只读问答等不同交付协议可通过相应的Reviewer实现接入Python API，保留原输出与判据。
