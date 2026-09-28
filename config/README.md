# 实验配置

`experiment_config.json` 已登记本机当前模型标识 `gpt6-Astra-ultra`，但 `model.execution=disabled`。这意味着系统可以校验输入、生成提示词哈希、校验输出结构和编译信号，但不会调用模型、联网研究或启动常驻任务。

只有在单独的实施任务中明确把执行模式切换为 `explicit`，并注册本机模型适配器后，模型运行器才会允许进一步接入；配置不会读取或保存 API 密钥。
