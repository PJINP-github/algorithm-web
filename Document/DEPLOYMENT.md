# 发布与隔离

测试目录是 `D:\Code\18.algorithm-web`，发布目录是 `D:\AlgorithmWeb`。发布端通过 `RUST_PORTAL_PROJECT_ROOT` 固定自己的根目录，上传文件、模型导入、推理输出、日志和数据库不会回写测试目录。

## 首次发布

首次发布并复制当前业务数据：

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\tools\publish.ps1 -IncludeData
```

发布脚本会先生成文件清单和目标备份，复制完成后校验文件大小与 SHA256；失败时自动恢复目标目录。备份默认保留，确认发布成功后可使用 `-RemoveBackupOnSuccess` 清理本次备份。

## 后续发布

后续默认只发布代码、配置、模型和运行所需文件，不覆盖发布端数据库、上传内容和历史结果：

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\tools\publish.ps1
```

执行前预览文件范围：

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\tools\publish.ps1 -Preview
```

需要再次同步业务数据时显式添加 `-IncludeData`。`-SourceRoot` 和 `-TargetRoot` 可用于指定其他测试目录或发布目录。

## 启动发布端

在 `D:\AlgorithmWeb` 运行 `run.bat`。脚本默认使用 `cargo run --release`、端口 `3000`，并将项目根目录设置为当前发布目录。需要临时改变端口时，可在启动前设置 `PORT`。

发布脚本会排除 Git、Codex、编辑器配置、缓存、临时目录、虚拟环境和编译产物；模型描述文件、`portal_bridge.py`、`src/algorithm`、`src/base` 以及模型权重会纳入发布清单。

## 离线包局域网审核

离线包 M/H 审核任务会在服务主机上为每个任务启动独立的 Python Web 进程，绑定 `0.0.0.0`，从端口 `8000` 起自动选择空闲端口，并在工作区显示服务主机局域网 IP 与该任务端口。M 模式可以并发运行；H 模式为保护共享历史数据库，全局一次只运行一个审核任务。导出完成或主工作区终止任务后，子进程会退出并释放端口。

发布机的 Windows 防火墙规则由管理员手工配置。请在实际使用的网络配置文件中放行离线审核端口范围（至少覆盖 `8000` 及并发任务可能使用的后续端口），不要将端口绑定到 `127.0.0.1`。若局域网 IP 无法自动检测，任务会失败并在主工作区日志中说明原因，不会提供不可访问的本地地址。
