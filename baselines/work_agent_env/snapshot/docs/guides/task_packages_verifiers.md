# 原评分与统一结果

integrations/verifiers/service.py 分发验证；adapters.py 调用任务绑定的评分程序，nemo.py 提供 NeMo 辅助接口。原 evaluator 的薄适配位于任务包内；有效业务失败与测量未知分别输出。

完整格式、启动方式和示例见[通用任务包指南](generic_benchmarks.md)。

valid=true 表示评分协议有效，不代表已证明环境/工具和评分语义正确。有效未通过需要单独核对数据前提、工具调用与评分规则，不能直接写成模型能力失败。原评分缺陷的修订必须单独标识，并保留 original_evaluation 与 reviewed_evaluation、说明及版本身份。
