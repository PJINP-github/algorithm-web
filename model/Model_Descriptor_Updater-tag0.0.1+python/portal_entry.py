"""model_descriptor.yaml 参数替换工具入口：本地 .bat/.sh 与网页 --request-file 双模式。"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _write_result(result_path: Path | None, payload: dict) -> None:
    if not result_path:
        return
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with result_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()


def run_with_request_file(request_file: str) -> int:
    request_path = Path(request_file).resolve()
    if not request_path.exists():
        print(f"[ERROR] 请求文件不存在: {request_path}", file=sys.stderr)
        return 1

    with request_path.open("r", encoding="utf-8") as f:
        request = json.load(f)

    result_path = request.get("result_path")
    result_path_obj = Path(result_path) if result_path else None

    try:
        from src.updater import run_from_request

        result = run_from_request(request)
        _write_result(result_path_obj, result)
        print(f"[OK] {result.get('summary', 'done')}")
        return 0
    except Exception as exc:
        traceback.print_exc()
        _write_result(
            result_path_obj,
            {"status": "failed", "summary": f"处理失败：{exc}"},
        )
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


def run_local() -> int:
    try:
        from src.updater import run_local

        return run_local()
    except Exception as exc:
        traceback.print_exc()
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="model_descriptor.yaml 参数替换工具")
    parser.add_argument(
        "--request-file",
        help="网页模式：Rust 传入的请求 JSON 绝对路径",
    )
    args = parser.parse_args()

    if args.request_file:
        sys.exit(run_with_request_file(args.request_file))
    sys.exit(run_local())


if __name__ == "__main__":
    main()