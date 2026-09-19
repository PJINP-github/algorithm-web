# model_descriptor 参数替换工具

导入包含 `model_descriptor.yaml` 与模型文件（`.pt` / `.om`）的文件夹，按照
`config.yaml` 中的 `Algorithm_Order` 白名单，把 yaml 中的锚点值或 `param`
替换为去掉后缀后的模型文件名，并打包输出。

## 目录结构
