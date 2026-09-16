"""本地推理模块。

这个包提供与 Trueno 3 部署描述文件兼容的本地推理入口。
它只依赖本机的 PyTorch、Ultralytics 和 OpenCV，不会加载服务器专用的
编译授权模块、硬件运行时或远程 FTP 配置。
"""

from .engine import LocalInferenceEngine

__all__ = ["LocalInferenceEngine"]
