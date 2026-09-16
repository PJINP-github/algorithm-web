"""本地推理命令行入口。

用途：
    在不打开网页的情况下，用一张图片快速验证描述文件、模型解析、
    ROI 和推理结果。网页服务也复用同一个 ``LocalInferenceEngine``。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import LocalInferenceEngine


def _parse_roi(value: str | None) -> list[int] | None:
    """解析命令行中的 ``x1,y1,x2,y2`` ROI。"""
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI 格式应为 x1,y1,x2,y2。")
    try:
        return [int(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI 坐标必须是整数。") from exc


def build_parser() -> argparse.ArgumentParser:
    """创建本地推理命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Trueno 3 本地模型推理")
    parser.add_argument("--image", required=True, help="输入图片路径")
    parser.add_argument("--ability", required=True, help="model_descriptor 中的能力名")
    parser.add_argument("--model", help="model 目录中的模型文件名")
    parser.add_argument("--roi", type=_parse_roi, help="ROI: x1,y1,x2,y2")
    parser.add_argument("--device", default="auto", help="auto、cpu 或 cuda:0")
    parser.add_argument("--conf", type=float, help="覆盖描述文件置信度")
    parser.add_argument("--iou", type=float, help="覆盖 IoU 阈值")
    parser.add_argument("--imgsz", type=int, help="覆盖模型输入尺寸")
    parser.add_argument("--output", help="保存标注结果图片路径")
    return parser


def main() -> int:
    """读取图片、执行推理并打印 JSON 结果。"""
    parser = build_parser()
    args = parser.parse_args()
    try:
        import cv2
    except ImportError as exc:
        parser.error(f"缺少 OpenCV: {exc}")
    image = cv2.imread(str(Path(args.image)), cv2.IMREAD_COLOR)
    if image is None:
        parser.error(f"无法读取图片: {args.image}")
    engine = LocalInferenceEngine(device=args.device)
    settings = {
        key: value
        for key, value in {
            "conf": args.conf,
            "iou": args.iou,
            "imgsz": args.imgsz,
        }.items()
        if value is not None
    }
    result = engine.infer(
        image,
        ability_name=args.ability,
        roi=args.roi,
        model_override=args.model,
        settings=settings,
    )
    if args.output:
        prefix, encoded = result["annotated_image"].split(",", 1)
        import base64

        Path(args.output).write_bytes(base64.b64decode(encoded))
        result["output"] = str(Path(args.output).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
