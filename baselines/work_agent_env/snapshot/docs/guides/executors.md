# 模型 Executor

integrations/executors/api.py 执行模型与工具循环；models.py 适配 API 协议；tools.py 处理工具目录与调用；registry.py 分发与持久化批次回执。并发、预算和固定模型由 ExecutorOptions 声明。

完整格式、启动方式和示例见[通用任务包指南](generic_benchmarks.md)。
