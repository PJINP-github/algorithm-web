"""启动 Trueno 3 本地模型验证网页。

运行：
    python run_local.py
    python run_local.py --port 8766 --device cpu
"""

from __future__ import annotations

import argparse
from urllib.parse import urlunsplit

from local_inference import LocalInferenceEngine
from local_web.server import create_server


def build_parser() -> argparse.ArgumentParser:
    """创建本地网页服务命令行参数。"""
    parser = argparse.ArgumentParser(description="Trueno 3 本地模型验证网页")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--device", default="auto", help="auto、cpu 或 cuda:0")
    parser.add_argument("--descriptor", help="自定义模型描述 YAML 路径")
    parser.add_argument("--model-dir", help="自定义本地模型目录")
    return parser


def main() -> int:
    """创建引擎并持续提供本地网页服务。"""
    args = build_parser().parse_args()
    engine = LocalInferenceEngine(
        descriptor_path=args.descriptor,
        model_dir=args.model_dir,
        device=args.device,
    )
    server = create_server(engine, host=args.host, port=args.port)
    address = server.server_address
    url = urlunsplit(("http", f"{address[0]}:{address[1]}", "/", "", ""))
    print(f"[Trueno Local] 网页地址: {url}")
    print("[Trueno Local] 按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Trueno Local] 正在停止...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
