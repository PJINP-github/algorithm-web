"""本地模型推理引擎。

功能：
    读取 ``storage/model_descriptor.yaml``，解析能力和流水线配置，
    在 ``model/`` 中查找本地权重，并执行多段推理。

设计说明：
    推理存在两条等价路径，二者都以描述文件为唯一入口：

    1. ``src 调用链``（默认）：完全复用服务器的调用约定，由
       ``src/base/<base>_nv`` 的 ``create_base_core_object`` 构建模型对象，
       再把 ``src/algorithm/<patch>`` 中的 ``patch_*`` 函数绑定为
       ``infer``，按 ``pipeline_idx`` 依次调用。阶段之间原样透传
       ``prv_image``、``prv_result`` 和 ``prv_config``，因此像
       ``meter_p2`` 依赖 ``prv_config['p1_box']`` 这类跨阶段状态、
       以及 ``pre_process: [crop_by_roi]`` 的内部兼容处理都由原脚本完成。
    2. ``本地执行器``（回退）：上一节所述脚本在本地不可加载时，使用本模块
       内置的 YOLO/CRNN/PaddleOCR 适配器执行同一份流水线配置。

    服务器版本还依赖 ``core.magic`` 编译授权、``storage/params`` 权重目录
    和硬件专用基类，这些不适合本机开发；本模块因此不加载 ``core.loader``，
    而是复刻其按描述文件装载 base 与 patch 的最小逻辑。
"""

from __future__ import annotations

import base64
import copy
import importlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MethodType
from typing import Any, Iterable


# 模块级 src 日志单例缓存，避免重复创建 WeeklyLogger 时添加重复 handler。
_SRC_LOGGER_LOCK = threading.Lock()
_SRC_LOGGERS: dict[tuple[str, str], logging.Logger] = {}

# 模块级 src 导入路径状态。``src/base`` 与 ``src/algorithm`` 顶层包名不带
# 前缀（如 ``base_yolo_nv``、``light_pgzsd``），因此必须把这两个目录加入
# ``sys.path``，与 ``src/core/loader.py`` 的服务端做法保持一致。
_SRC_PATH_LOCK = threading.Lock()
_SRC_PATHS: set[str] = set()


def _normalise_path(value: str | Path) -> Path:
    """Accept YAML/CLI paths written with either Windows or POSIX separators."""
    expanded = os.path.expandvars(os.path.expanduser(str(value).strip()))
    return Path(expanded.replace("\\", "/"))


class LocalInferenceError(RuntimeError):
    """本地推理相关的可预期错误。"""


class LocalDependencyError(LocalInferenceError):
    """本机缺少推理依赖时抛出的错误。"""


class ModelResolutionError(LocalInferenceError):
    """无法从本地模型目录解析权重时抛出的错误。"""


class SourceChainError(LocalInferenceError):
    """``src`` 调用链无法加载或执行时抛出的错误。

    这类错误在推理时会被捕获并回退到内置本地执行器，因此表示的是
    “原项目脚本在本机不可用”，而不是请求本身非法。
    """


@dataclass(frozen=True)
class ModelCandidate:
    """描述一个可供本地推理使用的模型文件。"""

    name: str
    relative_path: str
    absolute_path: Path
    size: int
    suffix: str

    def to_dict(self) -> dict[str, Any]:
        """将模型候选转换为网页 API 可序列化的字典。"""
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "size": self.size,
            "suffix": self.suffix,
        }


class LocalInferenceEngine:
    """管理描述文件、模型缓存和本地推理请求。"""

    SUPPORTED_MODEL_SUFFIXES = {".pt", ".onnx", ".torchscript", ".engine"}
    YOLO_BASE_NAMES = {
        "base_yolo",
        "base_yolo_nv",
        "base_yolo_om",
        "base_yolo_rknn",
    }
    CRNN_BASE_NAMES = {"base_crnn", "base_crnn_nv", "base_crnn_om"}
    PPOCR_BASE_NAMES = {"base_ppocr", "base_ppocr_nv", "base_ppocr_om"}
    # 描述文件里的 base 是设备无关名（base_yolo / base_ppocr），
    # 需要按权重格式映射到 src/base 下的硬件专用实现。
    SOURCE_BASE_SUFFIXES = {
        ".pt": "_nv",
        ".onnx": "_nv",
        ".om": "_om",
        ".rknn": "_rknn",
    }

    def __init__(
        self,
        root_dir: str | Path | None = None,
        descriptor_path: str | Path | None = None,
        model_dir: str | Path | None = None,
        additional_model_dirs: Iterable[str | Path] | None = None,
        device: str = "auto",
        log_capacity: int = 500,
    ) -> None:
        """初始化本地引擎并读取模型描述文件。

        参数：
            root_dir: 项目根目录。默认取当前文件的上两级目录。
            descriptor_path: 模型描述 YAML 路径。
            model_dir: 本地模型目录。
            additional_model_dirs: 额外的本地模型目录，例如用户导入目录。
            device: ``auto``、``cpu`` 或形如 ``cuda:0`` 的设备名。
            log_capacity: 内存中保留的最近日志数量。
        """
        self.root_dir = _normalise_path(
            root_dir or Path(__file__).resolve().parents[1]
        ).resolve()
        descriptor_value = _normalise_path(
            descriptor_path or self.root_dir / "storage" / "model_descriptor.yaml"
        )
        self.descriptor_path = (
            (self.root_dir / descriptor_value) if not descriptor_value.is_absolute() else descriptor_value
        ).resolve()
        model_value = _normalise_path(model_dir or self.root_dir / "model")
        self.model_dir = (
            (self.root_dir / model_value) if not model_value.is_absolute() else model_value
        ).resolve()
        self.additional_model_dirs = []
        for directory in additional_model_dirs or []:
            directory_value = _normalise_path(directory)
            resolved = (
                (self.root_dir / directory_value)
                if not directory_value.is_absolute()
                else directory_value
            ).resolve()
            if resolved != self.model_dir and resolved not in self.additional_model_dirs:
                self.additional_model_dirs.append(resolved)
        self.requested_device = device
        self._models: dict[str, Any] = {}
        self._model_infos: dict[str, dict[str, Any]] = {}
        self._log_records: deque[str] = deque(maxlen=log_capacity)
        self._log_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._logger = self._setup_src_logger()
        self._numpy = None
        self._cv2 = None
        self._yolo = None
        self._torch = None
        self._runtime_error = ""
        self._image_runtime_error = ""
        self._cn_font_cache: str | None = None
        # src 调用链缓存：执行器实例、模块、以及本机是否可用的判定结果。
        self._src_executors: dict[tuple[str, str, str], Any] = {}
        self._src_modules: dict[str, Any] = {}
        self._src_result_types: tuple[type, Any, Any] | None = None
        self._src_ability_cache: dict[str, bool] = {}
        self._descriptor = self._load_descriptor()
        self._abilities = self._descriptor.get("abilities", {})
        self.device = self._select_device(device)
        self._try_load_runtime()

    def _setup_src_logger(self) -> logging.Logger:
        """复用 ``src/config/log_utils.WeeklyLogger`` 的日志实例。

        本地调用日志与服务器共用 ``src/logs/trueno3.log``，网页即可直接
        读取同一份日志文件。加载失败时回退到进程内 ``trueno.local`` logger。
        """
        cache_key = (str(self.root_dir), "trueno3")
        with _SRC_LOGGER_LOCK:
            cached = _SRC_LOGGERS.get(cache_key)
            if cached is not None:
                return cached
            try:
                source_dir = self.root_dir / "src"
                log_dir = source_dir / "logs"
                if str(source_dir) not in sys.path:
                    sys.path.insert(0, str(source_dir))
                from config.log_utils import WeeklyLogger

                logger = WeeklyLogger(
                    log_dir=str(log_dir), name="trueno3", level=logging.INFO
                ).get_logger()
            except Exception as exc:  # pragma: no cover - 依赖缺失时回退
                logger = logging.getLogger("trueno.local")
                logger.warning("无法加载 src/config/log_utils.WeeklyLogger: %s", exc)
            _SRC_LOGGERS[cache_key] = logger
            return logger

    def src_log_file(self) -> Path:
        """返回与服务器一致的 ``src/logs/trueno3.log`` 路径。"""
        return (self.root_dir / "src" / "logs" / "trueno3.log").resolve()

    def read_src_log_lines(self, limit: int = 500) -> list[str]:
        """读取 ``src/logs/trueno3.log`` 尾部内容，作为网页日志来源。"""
        log_file = self.src_log_file()
        if not log_file.is_file():
            return []
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-max(0, limit):]

    def _load_descriptor(self) -> dict[str, Any]:
        """读取并检查 YAML 描述文件。"""
        if not self.descriptor_path.exists():
            raise LocalInferenceError(
                f"模型描述文件不存在: {self.descriptor_path}"
            )
        try:
            import yaml
        except ImportError as exc:
            raise LocalDependencyError(
                "缺少 PyYAML，请安装 local_requirements.txt 中的本地依赖。"
            ) from exc

        with self.descriptor_path.open("r", encoding="utf-8") as descriptor_file:
            data = yaml.safe_load(descriptor_file) or {}
        if not isinstance(data, dict) or not isinstance(data.get("abilities"), dict):
            raise LocalInferenceError(
                f"描述文件格式不正确，未找到 abilities: {self.descriptor_path}"
            )
        self._log(f"已读取模型描述: {self.descriptor_path}")
        return data

    def _select_device(self, requested: str) -> str:
        """根据用户配置和本机 CUDA 状态选择设备。"""
        if requested and requested.lower() != "auto":
            return requested
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda:0"
        except Exception:
            pass
        return "cpu"

    def _try_load_runtime(self) -> None:
        """可选地加载推理依赖，使网页在未装依赖时仍能打开。"""
        try:
            import cv2
            import numpy as np
            import torch

            self._cv2 = cv2
            self._numpy = np
            self._torch = torch
        except Exception as exc:
            self._image_runtime_error = (
                "OpenCV、NumPy 或 PyTorch 不可用: "
                f"{type(exc).__name__}: {exc}"
            )
            self._log(self._image_runtime_error)
            return

        try:
            from ultralytics import YOLO

            self._yolo = YOLO
            self._runtime_error = ""
            self._log(
                f"本地运行时已就绪: torch={torch.__version__}, "
                f"device={self.device}"
            )
        except Exception as exc:
            self._runtime_error = (
                "Ultralytics 不可用，YOLO 能力暂时无法推理: "
                f"{type(exc).__name__}: {exc}"
            )
            self._log(self._runtime_error)

    def _log(self, message: str, request_logs: list[str] | None = None) -> None:
        """记录一条带时间的引擎日志，并可同时加入当前请求日志。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._log_lock:
            self._log_records.append(line)
        if request_logs is not None:
            request_logs.append(line)
        self._logger.info(message)

    @staticmethod
    def _normalise_mapping(value: Any) -> dict[str, Any]:
        """兼容 YAML 中的字典、数字键字典和键值对列表。"""
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, list):
            converted: dict[str, Any] = {}
            for item in value:
                if isinstance(item, dict):
                    converted.update({str(key): value for key, value in item.items()})
            return converted
        return {}

    @staticmethod
    def _normalise_names(value: Any) -> dict[int, str]:
        """将模型类别名转换为以整数类别 ID 为键的字典。"""
        if isinstance(value, dict):
            result: dict[int, str] = {}
            for key, name in value.items():
                try:
                    result[int(key)] = str(name)
                except (TypeError, ValueError):
                    continue
            return result
        if isinstance(value, (list, tuple)):
            return {index: str(name) for index, name in enumerate(value)}
        return {}

    @staticmethod
    def _pipeline_items(ability: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        """按流水线编号排序，并兼容 YAML 将编号读成字符串的情况。"""
        pipeline = ability.get("pipeline") or {}
        items: list[tuple[int, dict[str, Any]]] = []
        for key, value in pipeline.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                items.append((index, value))
        return sorted(items, key=lambda item: item[0])

    @staticmethod
    def _normalise_roi(
        roi: Any, width: int, height: int
    ) -> list[int] | None:
        """规范化用户绘制的矩形 ROI，并限制在图像边界内。"""
        if roi is None or roi == "":
            return None
        if isinstance(roi, list) and roi and isinstance(roi[0], dict):
            rectangle = next(
                (
                    item
                    for item in roi
                    if str(item.get("type", "")).lower() == "rectangle"
                ),
                roi[0],
            )
            roi = rectangle.get("bbox") or rectangle.get("points") or rectangle.get("roi")
        if isinstance(roi, dict):
            roi = roi.get("bbox") or roi.get("points") or roi.get("roi")
        if not isinstance(roi, (list, tuple)) or len(roi) < 4:
            raise LocalInferenceError("ROI 必须是 [x1, y1, x2, y2]。")
        # 兼容服务器接口的 rectangle points:
        # [x1, y1, 0, 0, x2, y2, 0, 0]。
        if len(roi) >= 8 and len(roi) % 2 == 0:
            roi = [roi[0], roi[1], roi[4], roi[5]]
        try:
            x1, y1, x2, y2 = [int(round(float(value))) for value in roi[:4]]
        except (TypeError, ValueError) as exc:
            raise LocalInferenceError("ROI 坐标必须是数字。") from exc
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise LocalInferenceError("ROI 区域太小，至少需要 2 x 2 像素。")
        return [x1, y1, x2, y2]

    @staticmethod
    def _normalise_bbox(value: Any, width: int, height: int) -> list[int]:
        """将辅助框统一为图像坐标中的矩形。"""
        if isinstance(value, dict):
            value = value.get("bbox") or value.get("roi") or value.get("points")
        if not isinstance(value, (list, tuple)) or len(value) < 4:
            raise LocalInferenceError("辅助框必须是 [x1, y1, x2, y2]。")
        if len(value) >= 8 and len(value) % 2 == 0:
            value = [value[0], value[1], value[4], value[5]]
        try:
            x1, y1, x2, y2 = [int(round(float(item))) for item in value[:4]]
        except (TypeError, ValueError) as exc:
            raise LocalInferenceError("辅助框坐标必须是数字。") from exc
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            raise LocalInferenceError("辅助框区域太小，至少需要 2 x 2 像素。")
        return [x1, y1, x2, y2]

    @staticmethod
    def _normalise_polygon(value: Any, width: int, height: int) -> list[list[int]]:
        """规范化一个标定多边形，至少需要 4 个顶点。

        支持原项目 ``on_intersection_output`` 的写法：既接受
        ``{'points': [x1, y1, x2, y2, ...]}`` 的扁平坐标，也接受
        ``[[x, y], [x, y], ...]`` 的顶点列表。
        """
        points = value.get("points") if isinstance(value, dict) else value
        if not isinstance(points, (list, tuple)) or not points:
            raise LocalInferenceError("标定框必须是非空的点数组。")
        if not isinstance(points[0], (list, tuple)):
            if len(points) % 2 != 0:
                raise LocalInferenceError("标定框坐标个数必须成对。")
            points = [
                [points[index], points[index + 1]]
                for index in range(0, len(points), 2)
            ]
        result: list[list[int]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise LocalInferenceError("标定框顶点必须是 [x, y]。")
            try:
                x = max(0, min(width, int(round(float(point[0])))))
                y = max(0, min(height, int(round(float(point[1])))))
            except (TypeError, ValueError) as exc:
                raise LocalInferenceError("标定框坐标必须是数字。") from exc
            result.append([x, y])
        if len(result) < 4:
            raise LocalInferenceError("标定框至少需要 4 个顶点。")
        return result

    def _normalise_calibration_polygons(
        self, value: Any, width: int, height: int
    ) -> list[list[list[int]]]:
        """把网页提交的标定框规范化成多边形列表。

        支持原项目 ``on_intersection_output`` 的坐标写法：
        ``{'points': [x1, y1, ...]}``、``[[x, y], ...]`` 顶点列表，
        以及兼容历史矩形 ``[x1, y1, x2, y2]``（展开为四点多边形）。
        单个无效标注会被跳过，不影响其余多边形。
        """
        if value in (None, ""):
            return []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not value:
            return []
        first = value[0]
        if not isinstance(first, (list, tuple, dict)) and len(value) == 4:
            x1, y1, x2, y2 = [int(round(float(item))) for item in value]
            value = [
                [min(x1, x2), min(y1, y2)],
                [max(x1, x2), min(y1, y2)],
                [max(x1, x2), max(y1, y2)],
                [min(x1, x2), max(y1, y2)],
            ]
        multiple = isinstance(first, dict) or (
            isinstance(first, (list, tuple))
            and bool(first)
            and isinstance(first[0], (list, tuple))
        )
        candidates = value if multiple else [value]
        polygons: list[list[list[int]]] = []
        for item in candidates:
            try:
                polygon = self._normalise_polygon(item, width, height)
            except LocalInferenceError:
                continue
            if len(polygon) >= 4:
                polygons.append(polygon)
        return polygons

    @staticmethod
    def _polygon_bounds(
        polygons: list[list[list[int]]],
    ) -> list[int] | None:
        """取标定多边形的外接矩形，仅供原脚本兼容参数使用。"""
        points = [point for polygon in polygons for point in polygon]
        if not points:
            return None
        xs = [int(point[0]) for point in points]
        ys = [int(point[1]) for point in points]
        if max(xs) - min(xs) < 2 or max(ys) - min(ys) < 2:
            return None
        return [min(xs), min(ys), max(xs), max(ys)]

    def _detection_in_calibration(
        self,
        detection: dict[str, Any],
        calibration: list[list[list[int]]],
    ) -> bool:
        """判断检测框中心是否落在任一标定多边形内。"""
        if not calibration:
            return True
        bbox = detection.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            return False
        center = detection.get("center")
        if not isinstance(center, (list, tuple)) or len(center) < 2:
            center = [
                (float(bbox[0]) + float(bbox[2])) / 2,
                (float(bbox[1]) + float(bbox[3])) / 2,
            ]
        try:
            point = (float(center[0]), float(center[1]))
        except (TypeError, ValueError):
            return False
        for polygon in calibration:
            contour = self._numpy.asarray(polygon, dtype=self._numpy.float32)
            if contour.shape[0] < 4:
                continue
            if self._cv2.pointPolygonTest(contour, point, False) >= 0:
                return True
        return False

    def _filter_detections_by_calibration(
        self,
        detections: Iterable[dict[str, Any]],
        calibration: list[list[list[int]]],
    ) -> list[dict[str, Any]]:
        """保留标定区内的结果，不对输入图片做裁剪或遮罩。"""
        return [
            detection
            for detection in detections
            if self._detection_in_calibration(detection, calibration)
        ]

    @classmethod
    def _normalise_helpers(
        cls, helpers: Any, width: int, height: int
    ) -> dict[str, list[dict[str, Any]]]:
        """规范化网页传入的标定点、人工框和框修正信息。"""
        if helpers in (None, ""):
            return {"points": [], "boxes": [], "box_adjustments": []}
        if not isinstance(helpers, dict):
            raise LocalInferenceError("helpers 必须是 JSON 对象。")

        points: list[dict[str, Any]] = []
        raw_points = helpers.get("points") or helpers.get("calibration_points") or []
        if not isinstance(raw_points, list):
            raise LocalInferenceError("helpers.points 必须是数组。")
        for index, item in enumerate(raw_points):
            if isinstance(item, dict):
                point = item.get("point") or [item.get("x"), item.get("y")]
                name = item.get("name") or item.get("id") or f"point_{index + 1}"
            else:
                point = item
                name = f"point_{index + 1}"
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise LocalInferenceError("标定点必须是 [x, y] 或包含 point 的对象。")
            try:
                x = max(0, min(width, int(round(float(point[0])))))
                y = max(0, min(height, int(round(float(point[1])))))
            except (TypeError, ValueError) as exc:
                raise LocalInferenceError("标定点坐标必须是数字。") from exc
            points.append({"id": str(name), "x": x, "y": y})
            if isinstance(item, dict) and item.get("value") not in (None, ""):
                points[-1]["value"] = item.get("value")
            if isinstance(item, dict) and item.get("stage") not in (None, ""):
                points[-1]["stage"] = item.get("stage")

        boxes: list[dict[str, Any]] = []
        raw_boxes = helpers.get("boxes") or helpers.get("manual_boxes") or []
        if not isinstance(raw_boxes, list):
            raise LocalInferenceError("helpers.boxes 必须是数组。")
        for index, item in enumerate(raw_boxes):
            if not isinstance(item, dict):
                item = {"bbox": item}
            box = cls._normalise_bbox(item, width, height)
            boxes.append(
                {
                    "id": str(item.get("id") or f"manual_box_{index + 1}"),
                    "bbox": box,
                    "class_name": str(item.get("class_name") or "manual"),
                    "stage": item.get("stage"),
                    "use_as_parent": bool(item.get("use_as_parent", True)),
                }
            )

        adjustments: list[dict[str, Any]] = []
        raw_adjustments = (
            helpers.get("box_adjustments")
            or helpers.get("box_adjustment")
            or helpers.get("adjustments")
            or []
        )
        if not isinstance(raw_adjustments, list):
            raise LocalInferenceError("helpers.box_adjustments 必须是数组。")
        for item in raw_adjustments:
            if not isinstance(item, dict):
                raise LocalInferenceError("框修正必须是对象。")
            adjustment = {
                "stage": item.get("stage"),
                "index": item.get("index"),
                "class_name": item.get("class_name"),
            }
            if item.get("bbox") is not None or item.get("roi") is not None:
                adjustment["bbox"] = cls._normalise_bbox(item, width, height)
            if (
                item.get("target_bbox") is not None
                or item.get("target_roi") is not None
            ):
                adjustment["target_bbox"] = cls._normalise_bbox(
                    item.get("target_bbox") or item.get("target_roi"),
                    width,
                    height,
                )
            for key in ("dx", "dy", "dw", "dh"):
                if item.get(key) not in (None, ""):
                    try:
                        adjustment[key] = int(round(float(item[key])))
                    except (TypeError, ValueError) as exc:
                        raise LocalInferenceError(
                            f"框修正字段 {key} 必须是数字。"
                        ) from exc
            if "bbox" not in adjustment and not any(
                key in adjustment for key in ("dx", "dy", "dw", "dh")
            ):
                raise LocalInferenceError("框修正需要 bbox 或 dx/dy/dw/dh。")
            if (
                adjustment.get("index") in (None, "")
                and not adjustment.get("class_name")
                and "target_bbox" not in adjustment
            ):
                raise LocalInferenceError(
                    "框修正需要 index、class_name 或 target_bbox 定位目标。"
                )
            adjustments.append(adjustment)
        return {
            "points": points,
            "boxes": boxes,
            "box_adjustments": adjustments,
        }

    @staticmethod
    def _stage_override(
        stage_models: dict[str, Any] | list[dict[str, Any]] | None,
        stage_index: int,
    ) -> str | None:
        """读取指定流水线阶段的模型覆盖值。"""
        value = None
        if isinstance(stage_models, dict):
            value = stage_models.get(str(stage_index), stage_models.get(stage_index))
        elif isinstance(stage_models, list):
            for item in stage_models:
                if not isinstance(item, dict):
                    continue
                item_stage = item.get("stage", item.get("index"))
                if item_stage in (stage_index, str(stage_index)):
                    value = item.get("model", item.get("path"))
                    break
        return str(value) if value not in (None, "") else None

    @classmethod
    def _apply_box_adjustments(
        cls,
        detections: list[dict[str, Any]],
        helpers: dict[str, list[dict[str, Any]]],
        stage_index: int,
        width: int,
        height: int,
    ) -> list[dict[str, Any]]:
        """将 UI 提交的框修正应用到当前阶段的检测结果。"""
        adjustments = helpers.get("box_adjustments", [])
        if not adjustments:
            return detections
        result = copy.deepcopy(detections)
        for adjustment in adjustments:
            stage = adjustment.get("stage")
            if stage not in (None, "", stage_index, str(stage_index)):
                continue
            targets = []
            for index, detection in enumerate(result):
                if adjustment.get("index") not in (None, "", index, str(index)):
                    target_bbox = adjustment.get("target_bbox")
                    if not target_bbox:
                        continue
                    current_bbox = detection.get("bbox") or []
                    if len(current_bbox) != 4 or any(
                        abs(float(current_bbox[pos]) - float(target_bbox[pos])) > 2
                        for pos in range(4)
                    ):
                        continue
                class_name = adjustment.get("class_name")
                if class_name and detection.get("class_name") != str(class_name):
                    continue
                targets.append(index)
            for index in targets:
                detection = result[index]
                old = detection["bbox"]
                if "bbox" in adjustment:
                    bbox = list(adjustment["bbox"])
                else:
                    dx = int(adjustment.get("dx", 0))
                    dy = int(adjustment.get("dy", 0))
                    dw = int(adjustment.get("dw", 0))
                    dh = int(adjustment.get("dh", 0))
                    bbox = [old[0] + dx, old[1] + dy, old[2] + dx + dw, old[3] + dy + dh]
                bbox = cls._normalise_bbox(bbox, width, height)
                detection["bbox"] = bbox
                detection["center"] = [
                    int(round((bbox[0] + bbox[2]) / 2)),
                    int(round((bbox[1] + bbox[3]) / 2)),
                ]
                detection["box_adjusted"] = True
        return result

    @staticmethod
    def _apply_point_helpers(
        detections: list[dict[str, Any]],
        helpers: dict[str, list[dict[str, Any]]],
        stage_index: int,
        stage: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """用 UI 标定点覆盖同名模型关键点，并保留人工点上下文。"""
        points = [
            item
            for item in helpers.get("points", [])
            if item.get("stage") in (None, "", stage_index, str(stage_index))
        ]
        if not points:
            return detections
        point_map = {str(item["id"]): [int(item["x"]), int(item["y"])] for item in points}
        result = copy.deepcopy(detections)
        keypoint_names = [str(item) for item in (stage.get("kpt_names") or [])]
        for detection in result:
            keypoints = detection.setdefault("keypoints", {})
            for name in keypoint_names:
                if name in point_map:
                    keypoints[name] = {"point": point_map[name], "manual": True}
            if point_map:
                detection["calibrated_points"] = point_map
                if "center_pointer" in point_map:
                    detection["center"] = point_map["center_pointer"]
        return result

    @staticmethod
    def _manual_parent_boxes(
        helpers: dict[str, list[dict[str, Any]]],
        stage_index: int,
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        """取当前阶段可作为后续裁剪输入的人工框。"""
        result = []
        for item in helpers.get("boxes", []):
            stage = item.get("stage")
            if stage in (None, "") and not include_unassigned:
                continue
            if stage not in (None, "", stage_index, str(stage_index)):
                continue
            if not item.get("use_as_parent", True):
                continue
            bbox = item["bbox"]
            result.append(
                {
                    "class_id": -1,
                    "class_name": item["class_name"],
                    "display_name": item["class_name"],
                    "confidence": 1.0,
                    "bbox": list(bbox),
                    "center": [
                        int(round((bbox[0] + bbox[2]) / 2)),
                        int(round((bbox[1] + bbox[3]) / 2)),
                    ],
                    "manual": True,
                    "manual_id": item["id"],
                }
            )
        return result

    def discover_models(self) -> list[ModelCandidate]:
        """扫描本地模型目录并返回支持的权重文件。"""
        candidates: list[ModelCandidate] = []
        seen: set[Path] = set()
        for directory in [self.model_dir, *self.additional_model_dirs]:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if (
                    not path.is_file()
                    or path.suffix.lower() not in self.SUPPORTED_MODEL_SUFFIXES
                ):
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                relative_root = self.model_dir
                if directory != self.model_dir and resolved.is_relative_to(self.root_dir):
                    relative_root = self.root_dir
                relative_path = resolved.relative_to(relative_root).as_posix()
                candidates.append(
                    ModelCandidate(
                        name=resolved.name,
                        relative_path=relative_path,
                        absolute_path=resolved,
                        size=resolved.stat().st_size,
                        suffix=resolved.suffix.lower(),
                    )
                )
        return candidates

    def _resolve_model(
        self,
        parameter: str | None,
        override: str | None = None,
        stage: dict[str, Any] | None = None,
    ) -> tuple[ModelCandidate, str]:
        """根据流水线参数或用户选择解析本地权重。

        返回值：
            ``(模型文件, 解析方式)``，解析方式可能是 exact、fuzzy、
            single-local-fallback、user-selected 或 best-effort-fallback。
            当本地没有匹配权重时，不再中断整条流水线，而是按阶段任务
            特征回退到最可能可用的本地模型，网页日志会明确记录该回退。
        """
        candidates = self.discover_models()
        if not candidates:
            raise ModelResolutionError(
                f"本地模型目录没有可用权重: {self.model_dir}"
            )

        if override:
            override_lower = str(override).replace("\\", "/").lower()
            for candidate in candidates:
                if override_lower in {
                    candidate.name.lower(),
                    candidate.relative_path.lower(),
                }:
                    return candidate, "user-selected"
            raise ModelResolutionError(
                f"用户选择的模型不在本地模型目录中: {override}"
            )

        parameter_text = str(parameter or "").strip().lower().replace("\\", "/")
        parameter_stem = Path(parameter_text).stem

        def signature(value: str) -> str:
            """去掉项目编号和版本号，保留模型家族标识。"""
            value = Path(value).stem.lower()
            value = value.split("+", 1)[0]
            if ".at-" in value:
                value = value[value.index(".at-") + 1 :]
            value = re.sub(
                r"_\d{8}(?:\.\d+)?(?:\.[a-z0-9-]+)?$",
                "",
                value,
            )
            return value

        parameter_signature = signature(parameter_text)
        for candidate in candidates:
            candidate_stem = candidate.absolute_path.stem.lower()
            if candidate_stem in {
                parameter_text,
                parameter_stem,
            } or candidate.name.lower() in {parameter_text, parameter_text + ".pt"}:
                return candidate, "exact"

        fuzzy_matches = [
            candidate
            for candidate in candidates
            if parameter_text
            and (
                parameter_text in candidate.absolute_path.stem.lower()
                or candidate.absolute_path.stem.lower() in parameter_text
                or (
                    parameter_signature
                    and signature(candidate.absolute_path.stem) == parameter_signature
                )
            )
        ]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], "fuzzy"
        if len(candidates) == 1:
            return candidates[0], "single-local-fallback"
        if fuzzy_matches:
            return fuzzy_matches[0], "fuzzy-first"
        best, fallback_reason = self._best_effort_candidate(candidates, stage)
        if best is not None:
            return best, fallback_reason
        raise ModelResolutionError(
            f"找不到参数 {parameter!r} 对应的本地模型，请在网页中手动选择。"
        )

    @staticmethod
    def _best_effort_candidate(
        candidates: list[ModelCandidate],
        stage: dict[str, Any] | None,
    ) -> tuple[ModelCandidate | None, str]:
        """没有精确匹配时，按阶段任务特征选择最可能的本地模型。

        评分只使用文件名启发式，避免在目录扫描阶段就加载全部权重。
        返回 ``(候选模型, "best-effort-fallback")``；完全没有候选时
        返回 ``(None, "unavailable")``。
        """
        if not candidates:
            return None, "unavailable"
        stage_config = stage or {}
        lower_patch = str(stage_config.get("patch", "")).lower()
        lower_base = str(stage_config.get("base", "")).lower()
        lower_param = str(stage_config.get("param", "")).lower()
        wants_pose = bool(stage_config.get("kpt_names"))
        wants_ocr = "crnn" in lower_base or "ocr" in lower_patch or "paddle" in lower_param

        def score(candidate: ModelCandidate) -> int:
            name = candidate.name.lower()
            points = 0
            if wants_pose and "pose" in name:
                points += 6
            if wants_ocr and ("ocr" in name or "crnn" in name):
                points += 6
            if "obb" in lower_patch and "obb" in name:
                points += 6
            if "det" in name:
                points += 2
            if "seg" in name:
                points += 1
            return points

        ocr_available = any(
            "ocr" in candidate.name.lower() or "crnn" in candidate.name.lower()
            for candidate in candidates
        )
        pose_available = any(
            "pose" in candidate.name.lower() for candidate in candidates
        )
        # OCR 与关键点阶段必须使用对应类型的权重，找不到时如实报告，
        # 避免把检测模型错误地当成 OCR/pose 模型使用。
        if wants_ocr and not ocr_available:
            return None, "unavailable"
        if wants_pose and not pose_available:
            return None, "unavailable"

        ordered = sorted(candidates, key=score, reverse=True)
        return ordered[0], "best-effort-fallback"

    @staticmethod
    def _is_yolo_stage(stage: dict[str, Any]) -> bool:
        """判断流水线阶段是否可以用通用 YOLO 适配器执行。"""
        base_name = str(stage.get("base", "")).lower()
        return (
            base_name in LocalInferenceEngine.YOLO_BASE_NAMES
            or base_name.startswith("base_yolo")
        )

    @classmethod
    def _stage_executor(cls, stage: dict[str, Any]) -> str:
        """返回阶段所需的本地执行器名称。"""
        base_name = str(stage.get("base", "")).lower()
        if cls._is_yolo_stage(stage):
            return "yolo"
        if base_name in cls.CRNN_BASE_NAMES or base_name.startswith("base_crnn"):
            return "crnn"
        if base_name in cls.PPOCR_BASE_NAMES or base_name.startswith("base_ppocr"):
            return "ppocr"
        return "unsupported"

    def _load_yolo(self, path: Path) -> Any:
        """加载并缓存一个 Ultralytics 模型。"""
        self._ensure_runtime()
        key = str(path.resolve())
        if key in self._models:
            return self._models[key]
        with self._model_lock:
            if key in self._models:
                return self._models[key]
            self._log(f"加载模型: {path}")
            try:
                model = self._yolo(str(path))
                model.to(self.device)
            except Exception as exc:
                raise LocalInferenceError(
                    f"加载模型失败 {path.name}: {type(exc).__name__}: {exc}"
                ) from exc
            self._models[key] = model
            self._model_infos[key] = self._build_model_info(model, path)
            return model

    def _load_crnn(self, path: Path, stage: dict[str, Any]) -> Any:
        """加载可选的本地 CRNN 执行器并缓存模型。"""
        self._ensure_image_runtime()
        dictionary = str(stage.get("dictionary", "")).strip()
        config: dict[str, Any] = {}
        if dictionary:
            dictionary_path = _normalise_path(dictionary)
            candidates = [
                dictionary_path,
                self.root_dir / dictionary_path,
                self.root_dir / "src" / dictionary_path,
                self.root_dir / "src" / "base" / "base_crnn_nv" / "config" / dictionary_path.name,
            ]
            for candidate in candidates:
                if candidate.exists() and candidate.is_file():
                    try:
                        import yaml

                        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            config = loaded
                    except Exception as exc:
                        raise LocalInferenceError(
                            f"读取 CRNN 配置失败 {candidate}: {type(exc).__name__}: {exc}"
                        ) from exc
                    break
        key = f"crnn|{path.resolve()}|{dictionary}"
        if key in self._models:
            return self._models[key]
        with self._model_lock:
            if key in self._models:
                return self._models[key]
            try:
                source_dir = self.root_dir / "src"
                if str(source_dir) not in sys.path:
                    sys.path.insert(0, str(source_dir))
                # 用 src/base 下的顶层包名导入：加入 src/algorithm 后，
                # ``base`` 会指向 algorithm.base 常规包，不能再作为前缀。
                base_dir = source_dir / "base"
                if str(base_dir) not in sys.path:
                    sys.path.append(str(base_dir))
                from base_crnn_nv.base_crnn import CRNN

                self._log(f"加载 CRNN 模型: {path}")
                model = CRNN(str(path), config)
                if self._torch is not None and hasattr(model, "device"):
                    model.device = self._torch.device(self.device)
                    module = getattr(model, "model", None)
                    if module is not None and hasattr(module, "to"):
                        module.to(model.device)
            except Exception as exc:
                raise LocalInferenceError(
                    f"加载 CRNN 模型失败 {path.name}: {type(exc).__name__}: {exc}"
                ) from exc
            self._models[key] = model
            self._model_infos[key] = self._build_model_info(model, path)
            return model

    def _load_ppocr(self, path: Path, stage: dict[str, Any]) -> Any:
        """加载 PaddleOCR 压缩模型包；该格式不能交给 Ultralytics。"""
        self._ensure_image_runtime()
        key = f"ppocr|{path.resolve()}|{stage.get('ocrdict', '')}"
        if key in self._models:
            return self._models[key]
        config = {
            "ocrdict": stage.get("ocrdict") or {"ocrdict": "-0123456789.abc"}
        }
        with self._model_lock:
            if key in self._models:
                return self._models[key]
            try:
                source_dir = self.root_dir / "src"
                if str(source_dir) not in sys.path:
                    sys.path.insert(0, str(source_dir))
                base_dir = source_dir / "base"
                if str(base_dir) not in sys.path:
                    sys.path.append(str(base_dir))
                from base_ppocr_nv.base_ppocr import BasePPOCR

                self._log(f"加载 PaddleOCR 模型: {path}")
                model = BasePPOCR(str(path), config)
            except Exception as exc:
                raise LocalInferenceError(
                    f"加载 PaddleOCR 模型失败 {path.name}: {type(exc).__name__}: {exc}"
                ) from exc
            self._models[key] = model
            self._model_infos[key] = self._build_model_info(model, path)
            return model

    def _ensure_runtime(self) -> None:
        """确保可执行真正的图像推理。"""
        if self._runtime_error or self._yolo is None:
            raise LocalDependencyError(
                self._runtime_error
                or "本地推理运行时未加载，请检查 local_requirements.txt。"
            )

    def _ensure_image_runtime(self) -> None:
        """确保 OpenCV 图片处理能力可用。"""
        if self._image_runtime_error or self._cv2 is None or self._numpy is None:
            raise LocalDependencyError(
                self._image_runtime_error or "OpenCV/NumPy 不可用。"
            )

    def _build_model_info(self, model: Any, path: Path) -> dict[str, Any]:
        """收集网页中展示的模型结构、参数量和算子信息。"""
        module = getattr(model, "model", None)
        operator_counts: Counter[str] = Counter()
        operators: list[dict[str, str]] = []
        parameter_count = 0
        trainable_parameter_count = 0
        if module is not None:
            # YOLO 使用 torch.nn.Module；PaddleOCR 的 TextRecognition 是
            # 推理封装对象，不提供 PyTorch 的 named_modules/parameters 接口。
            named_modules = getattr(module, "named_modules", None)
            if callable(named_modules):
                for name, layer in named_modules():
                    if not name:
                        continue
                    operator_name = type(layer).__name__
                    operator_counts[operator_name] += 1
                    if len(operators) < 160:
                        operators.append({"name": name, "type": operator_name})
            parameters = getattr(module, "parameters", None)
            if callable(parameters):
                for parameter in parameters():
                    parameter_count += int(parameter.numel())
                    if parameter.requires_grad:
                        trainable_parameter_count += int(parameter.numel())

        names = self._normalise_names(getattr(model, "names", {}))
        return {
            "path": str(path.relative_to(self.root_dir).as_posix())
            if path.is_relative_to(self.root_dir)
            else str(path),
            "file_name": path.name,
            "task": str(getattr(model, "task", "unknown")),
            "device": self.device,
            "class_names": names,
            "parameter_count": parameter_count,
            "trainable_parameter_count": trainable_parameter_count,
            "layer_count": sum(operator_counts.values()),
            "operator_counts": dict(sorted(operator_counts.items())),
            "operators": operators,
            "stride": self._to_jsonable(getattr(module, "stride", None)),
            "overrides": self._to_jsonable(getattr(model, "overrides", {})),
        }

    def model_info(self, model_name: str | None = None) -> dict[str, Any]:
        """返回指定模型的结构信息；不指定时返回目录和运行时状态。"""
        candidates = self.discover_models()
        payload: dict[str, Any] = {
            "model_dir": str(self.model_dir),
            "models": [candidate.to_dict() for candidate in candidates],
            "runtime": self.health(),
        }
        if model_name:
            candidate, resolution = self._resolve_model("", model_name)
            self._load_yolo(candidate.absolute_path)
            payload["selected"] = self._model_infos[str(candidate.absolute_path)]
            payload["resolution"] = resolution
            return payload
        return payload

    def health(self) -> dict[str, Any]:
        """返回本地服务、依赖、描述文件和模型目录状态。"""
        torch_version = getattr(self._torch, "__version__", "")
        return {
            "ok": bool(
                self._cv2 is not None
                and self._numpy is not None
                and not self._image_runtime_error
            ),
            "descriptor": str(self.descriptor_path),
            "descriptor_exists": self.descriptor_path.exists(),
            "model_dir": str(self.model_dir),
            "model_count": len(self.discover_models()),
            "device": self.device,
            "torch_version": torch_version,
            "opencv_available": self._cv2 is not None,
            "ultralytics_available": self._yolo is not None,
            "message": self._runtime_error
            or self._image_runtime_error
            or "本地推理服务已就绪。",
        }

    def ability_catalog(self) -> dict[str, Any]:
        """生成供网页选择能力和查看流水线的配置目录。"""
        models = self.discover_models()
        entries: list[dict[str, Any]] = []
        for ability_name, ability in self._abilities.items():
            if not isinstance(ability, dict):
                continue
            stages = []
            supported = True
            unsupported_reasons: list[str] = []
            src_available = self._src_ability_available(ability_name)
            for index, stage in self._pipeline_items(ability):
                base_name = str(stage.get("base", ""))
                executor = self._stage_executor(stage)
                executable = executor in {"yolo", "crnn", "ppocr"} and (
                    executor != "crnn" or self._torch is not None
                )
                if src_available:
                    # src 调用链能加载时，阶段由原脚本执行，内置执行器只作为回退。
                    executor = "src"
                    executable = True
                unsupported_reason = ""
                if not executable and ability_name not in {"image_black"}:
                    supported = False
                    unsupported_reason = (
                        f"阶段 P{index} ({stage.get('patch', '')}) 需要 {executor!r} "
                        "执行器，本地未集成对应算法模块。"
                    )
                    unsupported_reasons.append(unsupported_reason)
                candidate = None
                resolution = "unavailable"
                try:
                    candidate, resolution = self._resolve_model(
                        stage.get("param"), None, stage
                    )
                except ModelResolutionError:
                    pass
                stages.append(
                    {
                        "index": index,
                        "base": base_name,
                        "patch": stage.get("patch", ""),
                        "parameter": stage.get("param", ""),
                        "model": candidate.to_dict() if candidate else None,
                        "resolution": resolution,
                        "executor": executor,
                        "executable": executable,
                        "source_chain": src_available,
                        "unsupported_reason": unsupported_reason,
                        "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
                        "kpt_names": [str(item) for item in (stage.get("kpt_names") or [])],
                        "class_names": self._normalise_names(
                            stage.get("cls_names", ability.get("cls_names", {}))
                        ),
                        "pre_process": [
                            str(item) for item in (stage.get("pre_process") or [])
                        ],
                    }
                )
            entries.append(
                {
                    "id": str(ability_name),
                    "description": str(ability.get("desc", ability_name)),
                    "debug": bool(ability.get("debug", False)),
                    "ret_img": bool(ability.get("ret_img", True)),
                    "supported": supported,
                    "source_chain": src_available,
                    "unsupported_reasons": unsupported_reasons,
                    "stages": stages,
                    "select_cls": ability.get("select_cls"),
                    "trans_cls": self._normalise_mapping(ability.get("trans_cls")),
                    "default_conf": ability.get("conf", 0.25),
                    "helper_schema": {
                        "points": any(
                            bool(stage.get("kpt_names"))
                            for stage in stages
                        ),
                        "boxes": len(stages) > 1,
                        "box_adjustments": len(stages) > 0,
                    },
                }
            )
        return {
            "version": self._descriptor.get("version", ""),
            "class": self._descriptor.get("class", ""),
            "entries": entries,
            "model_count": len(models),
            "recommended_ability": self._recommend_ability(entries),
            "health": self.health(),
        }

    def _recommend_ability(self, entries: list[dict[str, Any]]) -> str:
        """依据本地模型类别名给出一个更可能匹配的默认能力。"""
        if not entries:
            return ""
        model_names: set[str] = set()
        # 首次打开网页时提前读取一个本地模型的元数据，这样默认能力
        # 会基于真实类别名推荐，而不是永远落到 YAML 的第一项。
        if not self._model_infos and self._yolo is not None:
            candidates = self.discover_models()
            if candidates:
                try:
                    self._load_yolo(candidates[0].absolute_path)
                except LocalInferenceError:
                    pass
        for candidate in self.discover_models():
            info = self._model_infos.get(str(candidate.absolute_path))
            if info:
                model_names.update(info.get("class_names", {}).values())
        if not model_names:
            return str(entries[0]["id"])
        scored: list[tuple[int, str]] = []
        for entry in entries:
            configured: set[str] = set()
            for stage in entry["stages"]:
                configured.update(stage.get("class_names", {}).values())
            scored.append((len(model_names & configured), str(entry["id"])))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][1]

    def infer(
        self,
        image: Any,
        ability_name: str,
        roi: Any = None,
        model_override: str | None = None,
        settings: dict[str, Any] | None = None,
        stage_models: dict[str, Any] | list[dict[str, Any]] | None = None,
        helpers: dict[str, Any] | None = None,
        calibration: Any = None,
        inference_mode: str = "local",
    ) -> dict[str, Any]:
        """执行一次描述文件驱动的本地推理请求。"""
        self._ensure_image_runtime()
        settings = settings or {}
        if ability_name not in self._abilities:
            raise LocalInferenceError(f"未知能力: {ability_name}")
        if (
            image is None
            or getattr(image, "ndim", 0) != 3
            or getattr(image, "shape", (0, 0, 0))[2] != 3
        ):
            raise LocalInferenceError("输入图片必须是 BGR 三通道图像。")

        height, width = image.shape[:2]
        calibration_input = calibration if calibration is not None else roi
        calibration = self._normalise_calibration_polygons(
            calibration_input, width, height
        )
        inference_mode = str(inference_mode or "local").strip().lower()
        if inference_mode not in {"local", "src"}:
            raise LocalInferenceError("推理链路必须是 local 或 src。")
        normalized_roi = self._polygon_bounds(calibration)
        normalized_helpers = self._normalise_helpers(helpers, width, height)
        request_logs: list[str] = []
        started_at = time.perf_counter()
        ability = self._abilities[ability_name]
        self._log(
            f"开始推理: ability={ability_name}, size={width}x{height}, "
            f"calibration={'%d 个标定框' % len(calibration) if calibration else '全图'}",
            request_logs,
        )

        if ability_name == "image_black":
            response = self._infer_image_black(
                image, ability_name, ability, normalized_roi, request_logs
            )
        else:
            response = self._infer_with_fallback(
                image,
                ability_name,
                ability,
                calibration,
                model_override,
                stage_models,
                normalized_helpers,
                settings,
                request_logs,
                inference_mode,
            )

        response["requested_inference_mode"] = inference_mode
        response.setdefault("inference_mode", inference_mode)
        response["elapsed_ms"] = round(
            (time.perf_counter() - started_at) * 1000, 2
        )
        response["logs"] = request_logs
        response["helpers"] = normalized_helpers
        response["recent_logs"] = self.recent_logs()
        self._log(
            f"推理完成: ability={ability_name}, "
            f"detections={len(response.get('detections', []))}, "
            f"elapsed_ms={response['elapsed_ms']}",
            request_logs,
        )
        return response

    def _infer_with_fallback(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        model_override: str | None,
        stage_models: dict[str, Any] | list[dict[str, Any]] | None,
        helpers: dict[str, list[dict[str, Any]]],
        settings: dict[str, Any],
        request_logs: list[str],
        inference_mode: str,
    ) -> dict[str, Any]:
        """按用户选择执行 src 调用链或内置本地执行器。

        ``src`` 模式必须使用描述文件指定的原脚本链路；``local`` 模式
        只使用本模块的本地执行器，避免按钮状态与实际调用链不一致。
        """
        if inference_mode == "src":
            reason = self._src_ability_unavailable_reason(ability_name)
            try:
                if reason:
                    raise SourceChainError(reason)
                return self._run_src_pipeline(
                    image,
                    ability_name,
                    ability,
                    calibration,
                    model_override,
                    stage_models,
                    helpers,
                    settings,
                    request_logs,
                )
            except (
                SourceChainError,
                ModelResolutionError,
                LocalDependencyError,
                ImportError,
                ModuleNotFoundError,
                FileNotFoundError,
                OSError,
            ) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._log(
                    "algorithm/src 调用链不可用，原因："
                    f"{reason}；自动回退到本地自推理。",
                    request_logs,
                )
                if any(
                    self._stage_executor(stage) == "yolo"
                    for _, stage in self._pipeline_items(ability)
                ):
                    self._ensure_runtime()
                response = self._infer_pipeline(
                    image,
                    ability_name,
                    ability,
                    calibration,
                    model_override,
                    stage_models,
                    helpers,
                    settings,
                    request_logs,
                )
                response["inference_mode"] = "local"
                response["inference_fallback"] = {
                    "from": "src",
                    "to": "local",
                    "reason": reason,
                }
                response["compatibility"] = (
                    "algorithm/src 调用链不可用，已按日志记录原因并自动回退到本地自推理。"
                )
                return response

        if any(
            self._stage_executor(stage) == "yolo"
            for _, stage in self._pipeline_items(ability)
        ):
            self._ensure_runtime()
        response = self._infer_pipeline(
            image,
            ability_name,
            ability,
            calibration,
            model_override,
            stage_models,
            helpers,
            settings,
            request_logs,
        )
        response["inference_mode"] = "local"
        response["compatibility"] = (
            "使用 model_descriptor.yaml 驱动的本地自推理执行器，未调用 algorithm/src 原脚本。"
        )
        return response

    def _infer_pipeline(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        model_override: str | None,
        stage_models: dict[str, Any] | list[dict[str, Any]] | None,
        helpers: dict[str, list[dict[str, Any]]],
        settings: dict[str, Any],
        request_logs: list[str],
    ) -> dict[str, Any]:
        """按阶段执行推理，并把每一段结果汇总成可消费的上下文。"""
        stages = self._pipeline_items(ability)
        if not stages:
            raise LocalInferenceError(f"能力没有可执行流水线: {ability_name}")
        if ability.get("meter_pipeline"):
            return self._infer_meter_pipeline(
                image,
                ability_name,
                ability,
                calibration,
                model_override,
                stage_models,
                helpers,
                settings,
                request_logs,
            )

        stage_results: list[dict[str, Any]] = []
        parents: list[dict[str, Any]] | None = None
        model_infos: dict[str, dict[str, Any]] = {}
        skipped_stages: list[dict[str, Any]] = []
        last_nonempty_detections: list[dict[str, Any]] = []
        for stage_position, (stage_index, stage) in enumerate(stages):
            if self._stage_executor(stage) not in {"yolo", "crnn", "ppocr"}:
                stage_info = {
                    "index": stage_index,
                    "base": stage.get("base", ""),
                    "patch": stage.get("patch", ""),
                    "parameter": stage.get("param", ""),
                    "executor": self._stage_executor(stage),
                    "status": "skipped",
                    "parent_count": 0,
                    "detections": [],
                    "raw_count": 0,
                    "returned_count": 0,
                    "elapsed_ms": 0,
                    "models": [],
                    "values": {},
                    "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
                }
                stage_results.append(stage_info)
                skipped_stages.append(stage_info)
                message = (
                    f"跳过阶段 P{stage_index}: base={stage.get('base','')} "
                    f"patch={stage.get('patch','')} 没有本地执行器，已保留其余阶段结果。"
                )
                self._log(message, request_logs)
                continue
            if stage_position == 0:
                source = image
                offset = (0, 0)
                detections, stage_info = self._run_stage(
                    source,
                    offset,
                    ability,
                    stage_index,
                    stage,
                    self._stage_override(stage_models, stage_index) or model_override,
                    settings,
                    request_logs,
                )
                detections = self._apply_box_adjustments(
                    detections,
                    helpers,
                    stage_index,
                    image.shape[1],
                    image.shape[0],
                )
                detections = self._apply_point_helpers(
                    detections, helpers, stage_index, stage
                )
                stage_info["detections"] = detections
                stage_info["returned_count"] = len(detections)
                manual_boxes = self._manual_parent_boxes(
                    helpers, stage_index, include_unassigned=True
                )
                if manual_boxes:
                    detections.extend(manual_boxes)
                    stage_info["manual_box_count"] = len(manual_boxes)
                    stage_info["returned_count"] = len(detections)
                    stage_info["detections"] = detections
                detections = self._filter_detections_by_calibration(
                    detections, calibration
                )
                stage_info["detections"] = detections
                stage_info["returned_count"] = len(detections)
                parents = detections
            else:
                next_detections: list[dict[str, Any]] = []
                parent_count = len(parents or [])
                stage_info = {
                    "index": stage_index,
                    "base": stage.get("base", ""),
                    "patch": stage.get("patch", ""),
                    "parameter": stage.get("param", ""),
                    "executor": self._stage_executor(stage),
                    "parent_count": parent_count,
                    "detections": [],
                    "raw_count": 0,
                    "returned_count": 0,
                    "elapsed_ms": 0,
                    "models": [],
                    "values": {},
                    "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
                }
                stage_started = time.perf_counter()
                adjusted_parents = list(parents or [])
                adjusted_parents.extend(
                    self._manual_parent_boxes(
                        helpers, stage_index, include_unassigned=False
                    )
                )
                parent_count = len(adjusted_parents)
                stage_info["parent_count"] = parent_count
                for parent in adjusted_parents:
                    ret_keys = {
                        str(item) for item in (stage.get("ret_keys") or [])
                    }
                    if (
                        ret_keys
                        and not parent.get("manual")
                        and parent.get("class_name") not in ret_keys
                        and parent.get("display_name") not in ret_keys
                    ):
                        continue
                    x1, y1, x2, y2 = parent["bbox"]
                    x1 = max(0, min(image.shape[1], int(x1)))
                    y1 = max(0, min(image.shape[0], int(y1)))
                    x2 = max(0, min(image.shape[1], int(x2)))
                    y2 = max(0, min(image.shape[0], int(y2)))
                    if x2 <= x1 or y2 <= y1:
                        continue
                    cropped = image[y1:y2, x1:x2]
                    detections, child_info = self._run_stage(
                        cropped,
                        (x1, y1),
                        ability,
                        stage_index,
                        stage,
                        self._stage_override(stage_models, stage_index) or model_override,
                        settings,
                        request_logs,
                    )
                    detections = self._apply_point_helpers(
                        detections, helpers, stage_index, stage
                    )
                    stage_info["raw_count"] += child_info["raw_count"]
                    stage_info.setdefault("models", []).extend(
                        child_info.get("models", [])
                    )
                    if child_info.get("model_path") and not stage_info.get("model_path"):
                        stage_info["model_path"] = child_info["model_path"]
                    if (
                        child_info.get("model_resolution")
                        and not stage_info.get("model_resolution")
                    ):
                        stage_info["model_resolution"] = child_info[
                            "model_resolution"
                        ]
                    stage_info.setdefault("values", {}).update(
                        child_info.get("values", {})
                    )
                    for detection in detections:
                        detection["parent_bbox"] = [x1, y1, x2, y2]
                        if parent.get("class_name"):
                            detection["parent_class_name"] = parent["class_name"]
                            if self._stage_executor(stage) == "crnn" and detection.get(
                                "text"
                            ):
                                detection["ocr_class_name"] = detection.get(
                                    "class_name"
                                )
                                detection["class_name"] = str(parent["class_name"])
                                detection["display_name"] = detection["text"]
                                stage_info.setdefault("values", {})[
                                    str(parent["class_name"])
                                ] = detection["text"]
                        if stage.get("return_outer_bbox", False):
                            detection["bbox"] = [x1, y1, x2, y2]
                        next_detections.append(detection)
                next_detections = self._apply_box_adjustments(
                    next_detections,
                    helpers,
                    stage_index,
                    image.shape[1],
                    image.shape[0],
                )
                next_detections = self._filter_detections_by_calibration(
                    next_detections, calibration
                )
                manual_box_count = len(
                    self._manual_parent_boxes(
                        helpers, stage_index, include_unassigned=False
                    )
                )
                if manual_box_count:
                    stage_info["manual_box_count"] = manual_box_count
                stage_info["detections"] = next_detections
                stage_info["returned_count"] = len(next_detections)
                stage_info["adjusted_parent_count"] = len(adjusted_parents)
                stage_info["elapsed_ms"] = round(
                    (time.perf_counter() - stage_started) * 1000, 2
                )
                parents = next_detections
                self._log(
                    f"流水线阶段 {stage_index}: parents={parent_count}, "
                    f"raw={stage_info['raw_count']}, "
                    f"returned={stage_info['returned_count']}",
                    request_logs,
                )
            stage_results.append(stage_info)
            for model_info in stage_info.get("models", []):
                model_infos[model_info["path"]] = model_info
            if parents:
                last_nonempty_detections = parents

        detections = parents or []
        if not detections and last_nonempty_detections:
            # 后续阶段（如 OCR/分类）没有返回时，沿用上一阶段的检测框，
            # 保证第一阶段识别到的目标仍然绘制到结果图并进入最终结果。
            detections = last_nonempty_detections
            self._log(
                f"后续阶段无返回，回退使用上一阶段结果: {len(detections)} 个检测框",
                request_logs,
            )
        annotated = self._annotate(
            image,
            detections,
            calibration,
            ability_name,
            helpers.get("points", []),
        )
        max_conf = max((item["confidence"] for item in detections), default=0.0)
        first_name = detections[0]["display_name"] if detections else "正常"
        ret_keys = [
            str(item)
            for _, stage in stages
            for item in (stage.get("ret_keys") or [])
        ]
        returned_values = {
            key: next(
                (
                    value
                    for stage_result in stage_results
                    for values in [stage_result.get("values", {})]
                    for value in [values.get(key)]
                    if value not in (None, "")
                ),
                None,
            )
            for key in ret_keys
        }
        value = next(
            (item for item in returned_values.values() if item not in (None, "")),
            "1" if detections else "0",
        )
        result_desc = (
            f"{ability.get('desc', ability_name)}: "
            + "; ".join(
                f"{key}={item}"
                for key, item in returned_values.items()
                if item not in (None, "")
            )
            if any(item not in (None, "") for item in returned_values.values())
            else f"{ability.get('desc', ability_name)}: {first_name}"
        )
        return {
            "ability": ability_name,
            "description": str(ability.get("desc", ability_name)),
            "status": "detected" if detections else "empty",
            "value": value,
            "desc": result_desc,
            "confidence": round(max_conf, 6),
            "image_size": {"width": int(image.shape[1]), "height": int(image.shape[0])},
            "roi": None,
            "calibration_polygons": calibration,
            "detections": detections,
            "stages": stage_results,
            "skipped_stages": skipped_stages,
            "models": list(model_infos.values()),
            "inference_context": {
                "points": helpers.get("points", []),
                "boxes": helpers.get("boxes", []),
                "box_adjustments": helpers.get("box_adjustments", []),
                "calibration_polygons": calibration,
                "ret_keys": ret_keys,
                "values": returned_values,
            },
            "annotated_image": self._image_to_data_url(annotated),
            "compatibility": (
                "本地适配器按描述文件执行 YOLO 检测、标定区过滤和类别过滤；"
                "特殊硬件算子仍以服务器运行时为准。"
            ),
        }

    def _infer_meter_pipeline(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        model_override: str | None,
        stage_models: dict[str, Any] | list[dict[str, Any]] | None,
        helpers: dict[str, list[dict[str, Any]]],
        settings: dict[str, Any],
        request_logs: list[str],
    ) -> dict[str, Any]:
        """按 meter_p1 -> meter_p2 -> meter_p3 执行连续表计推理。

        P2 和 P3 都以 P1 的表盘框为父级。通用流水线会把 P2 的指针框
        继续传给 P3，这与原项目 meter_p3 的调用契约不一致。
        """
        stages = self._pipeline_items(ability)
        stage_map = {
            str(stage.get("patch", "")): (index, stage)
            for index, stage in stages
        }
        try:
            p1_index, p1_stage = stage_map["meter_p1"]
            p2_index, p2_stage = stage_map["meter_p2"]
            p3_index, p3_stage = next(
                item for patch, item in stage_map.items()
                if patch.startswith("meter_p3")
            )
        except (KeyError, StopIteration) as exc:
            raise LocalInferenceError(
                "连续表计能力必须包含 meter_p1、meter_p2、meter_p3。"
            ) from exc

        source = image
        offset = (0, 0)
        p1_detections, p1_info = self._run_stage(
            source,
            offset,
            ability,
            p1_index,
            p1_stage,
            self._stage_override(stage_models, p1_index),
            settings,
            request_logs,
        )
        p1_detections = self._apply_box_adjustments(
            p1_detections, helpers, p1_index, image.shape[1], image.shape[0]
        )
        p1_detections = self._apply_point_helpers(
            p1_detections, helpers, p1_index, p1_stage
        )
        p1_detections = self._filter_detections_by_calibration(
            p1_detections, calibration
        )
        p1_info["detections"] = p1_detections
        p1_info["returned_count"] = len(p1_detections)
        stages_out = [p1_info]
        model_infos = {
            item["path"]: item
            for item in p1_info.get("models", [])
            if item.get("path")
        }

        if not p1_detections:
            self._log("表计阶段 1 未识别到表盘，停止后续依赖链。", request_logs)
            annotated = self._annotate(
                image, [], calibration, ability_name, helpers.get("points", [])
            )
            return self._meter_response(
                image, ability_name, ability, calibration, [], stages_out, model_infos,
                annotated, helpers, "empty", "未识别到表盘"
            )

        descriptor = ability.get("meter_descriptor") or {}
        manual_marks = self._meter_manual_marks(helpers)
        final_detections: list[dict[str, Any]] = []
        for parent_index, parent in enumerate(p1_detections):
            x1, y1, x2, y2 = [int(value) for value in parent["bbox"]]
            x1 = max(0, min(image.shape[1], x1))
            y1 = max(0, min(image.shape[0], y1))
            x2 = max(0, min(image.shape[1], x2))
            y2 = max(0, min(image.shape[0], y2))
            if x2 <= x1 or y2 <= y1:
                continue
            cropped = image[y1:y2, x1:x2]

            pointer_detections, p2_child = self._run_stage(
                cropped,
                (x1, y1),
                ability,
                p2_index,
                p2_stage,
                self._stage_override(stage_models, p2_index),
                settings,
                request_logs,
            )
            pointer_detections = self._apply_point_helpers(
                pointer_detections, helpers, p2_index, p2_stage
            )
            p2_child["parent_index"] = parent_index
            p2_child["parent_bbox"] = [x1, y1, x2, y2]
            p2_child["detections"] = pointer_detections
            p2_child["returned_count"] = len(pointer_detections)
            stages_out.append(p2_child)
            for item in p2_child.get("models", []):
                if item.get("path"):
                    model_infos[item["path"]] = item

            mark_detections, p3_child = self._run_stage(
                cropped,
                (x1, y1),
                ability,
                p3_index,
                p3_stage,
                self._stage_override(stage_models, p3_index),
                settings,
                request_logs,
            )
            mark_detections = self._apply_point_helpers(
                mark_detections, helpers, p3_index, p3_stage
            )
            p3_child["parent_index"] = parent_index
            p3_child["parent_bbox"] = [x1, y1, x2, y2]
            p3_child["detections"] = mark_detections
            p3_child["returned_count"] = len(mark_detections)
            stages_out.append(p3_child)
            for item in p3_child.get("models", []):
                if item.get("path"):
                    model_infos[item["path"]] = item

            center = self._meter_detection_point(
                [parent], "center_pointer"
            ) or tuple(parent.get("center") or ((x1 + x2) // 2, (y1 + y2) // 2))
            pointer = self._meter_detection_point(
                pointer_detections, "mark_pointer_black"
            )
            marks = manual_marks or self._meter_detected_marks(
                mark_detections, p3_stage, descriptor
            )
            calculation = self._calculate_meter_value(
                center, pointer, marks, descriptor, request_logs
            )
            result = copy.deepcopy(parent)
            result["stage"] = p3_index
            result["meter_marks"] = marks
            result["keypoints"] = copy.deepcopy(result.get("keypoints") or {})
            result["keypoints"]["center_pointer"] = {
                "point": [int(center[0]), int(center[1])]
            }
            if pointer:
                result["keypoints"]["mark_pointer_black"] = {
                    "point": [int(pointer[0]), int(pointer[1])]
                }
            if calculation is not None:
                value, ordered_marks = calculation
                result["class_name"] = "meter_value"
                result["display_name"] = str(value)
                result["value"] = str(value)
                result["meter_value"] = value
                result["meter_marks"] = ordered_marks
            final_detections.append(result)

        computed = [
            item for item in final_detections
            if item.get("meter_value") is not None
        ]
        if computed:
            status = "detected"
            desc = "; ".join(str(item["meter_value"]) for item in computed)
        elif manual_marks:
            status = "empty"
            desc = "表计刻度计算失败"
            self._log(desc, request_logs)
        else:
            status = "needs_points"
            desc = "请使用“点绘制”补充至少两个带数值的刻度点"
            self._log(desc, request_logs)
        annotated = self._annotate_meter(
            image, final_detections, calibration, helpers.get("points", [])
        )
        return self._meter_response(
            image,
            ability_name,
            ability,
            calibration,
            final_detections,
            stages_out,
            model_infos,
            annotated,
            helpers,
            status,
            desc,
        )

    @staticmethod
    def _meter_detection_point(
        detections: list[dict[str, Any]],
        name: str,
    ) -> tuple[int, int] | None:
        for detection in detections:
            point = (detection.get("keypoints") or {}).get(name)
            if isinstance(point, dict):
                point = point.get("point")
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                return int(point[0]), int(point[1])
            for value in (detection.get("keypoints") or {}).values():
                point = value.get("point") if isinstance(value, dict) else value
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    return int(point[0]), int(point[1])
        return None

    @staticmethod
    def _meter_manual_marks(
        helpers: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        result = []
        for index, point in enumerate(helpers.get("points", []), start=1):
            if point.get("stage") not in (None, "", 3, "3"):
                continue
            if point.get("value") in (None, ""):
                continue
            try:
                value = float(point["value"])
            except (TypeError, ValueError):
                continue
            result.append({
                "id": str(point.get("id") or index),
                "point": [int(point["x"]), int(point["y"])],
                "value": value,
                "manual": True,
            })
        return result

    @staticmethod
    def _meter_detected_marks(
        detections: list[dict[str, Any]],
        stage: dict[str, Any],
        descriptor: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        name = str((stage.get("kpt_names") or ["mark"])[0])
        result = []
        for index, detection in enumerate(detections, start=1):
            point = LocalInferenceEngine._meter_detection_point([detection], name)
            if point:
                result.append({"id": str(index), "point": list(point)})
        configured_values = (descriptor or {}).get("marks")
        if isinstance(configured_values, list):
            for index, mark in enumerate(result):
                if index >= len(configured_values):
                    break
                value = configured_values[index]
                if isinstance(value, dict):
                    value = value.get("value")
                if value not in (None, ""):
                    mark["value"] = value
        return result

    def _calculate_meter_value(
        self,
        center: tuple[int, int] | None,
        pointer: tuple[int, int] | None,
        marks: list[dict[str, Any]],
        descriptor: dict[str, Any],
        request_logs: list[str],
    ) -> tuple[Any, list[dict[str, Any]]] | None:
        if not center or not pointer:
            return None
        valued = []
        for mark in marks:
            try:
                valued.append({**mark, "value": float(mark["value"])})
            except (KeyError, TypeError, ValueError):
                continue
        if len(valued) < 2:
            return None
        reverse = bool(descriptor.get("reverse", False))
        valued.sort(key=lambda item: item["value"], reverse=reverse)
        cx, cy = center
        if all(item.get("manual") for item in valued):
            ordered = valued
            mark_angles = [
                math.atan2(item["point"][1] - cy, item["point"][0] - cx)
                for item in ordered
            ]
            candidates = []
            for direction in (1, -1):
                offsets = [
                    ((angle - mark_angles[0]) * direction) % (2 * math.pi)
                    for angle in mark_angles
                ]
                if all(
                    offsets[index] + 1e-9 >= offsets[index - 1]
                    for index in range(1, len(offsets))
                ):
                    candidates.append((offsets[-1], direction, offsets))
            if not candidates:
                return None
            _, direction, offsets = min(candidates, key=lambda item: item[0])
        else:
            polar = sorted(
                valued,
                key=lambda item: math.atan2(
                    item["point"][1] - cy, item["point"][0] - cx
                ),
            )
            angles = [
                math.atan2(item["point"][1] - cy, item["point"][0] - cx)
                for item in polar
            ]
            gaps = [
                (angles[(index + 1) % len(angles)] - angle) % (2 * math.pi)
                for index, angle in enumerate(angles)
            ]
            start = (max(range(len(gaps)), key=gaps.__getitem__) + 1) % len(polar)
            ordered = [
                {**item, "value": valued[index]["value"]}
                for index, item in enumerate(polar[start:] + polar[:start])
            ]
            direction = 1
            start_angle = math.atan2(
                ordered[0]["point"][1] - cy, ordered[0]["point"][0] - cx
            )
            offsets = [
                (
                    math.atan2(item["point"][1] - cy, item["point"][0] - cx)
                    - start_angle
                ) % (2 * math.pi)
                for item in ordered
            ]
        start_angle = math.atan2(
            ordered[0]["point"][1] - cy, ordered[0]["point"][0] - cx
        )
        pointer_angle = math.atan2(pointer[1] - cy, pointer[0] - cx)
        pointer_offset = ((pointer_angle - start_angle) * direction) % (2 * math.pi)
        adjacent = None
        for index in range(1, len(offsets)):
            if offsets[index - 1] <= pointer_offset <= offsets[index]:
                adjacent = index - 1
                break
        if adjacent is None:
            adjacent = 0 if pointer_offset < offsets[0] else len(offsets) - 2
        if adjacent < 0 or adjacent + 1 >= len(ordered):
            return None
        lower, upper = ordered[adjacent], ordered[adjacent + 1]
        denominator = offsets[adjacent + 1] - offsets[adjacent]
        ratio = 0.0 if abs(denominator) < 1e-9 else (
            pointer_offset - offsets[adjacent]
        ) / denominator
        ratio = max(0.0, min(1.0, ratio))
        value = lower["value"] + (upper["value"] - lower["value"]) * ratio
        scale = float(descriptor.get("scale", 1) or 1)
        precision = descriptor.get("precision")
        value *= scale
        if precision not in (None, ""):
            value = round(value, int(precision))
        point_map = descriptor.get("point_map") or {}
        if point_map:
            mapped = []
            for key, mapped_value in point_map.items():
                try:
                    mapped.append((abs(float(key) - value), mapped_value))
                except (TypeError, ValueError):
                    continue
            if mapped:
                nearest_distance, nearest_value = min(mapped, key=lambda item: item[0])
                if nearest_distance <= 0.5:
                    value = nearest_value
        if descriptor.get("value_floor") and not isinstance(value, str):
            value = math.floor(value)
        self._log(
            f"表计刻度插值: lower={lower['value']}, upper={upper['value']}, "
            f"ratio={ratio:.4f}, value={value}",
            request_logs,
        )
        return value, ordered

    def _meter_response(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        detections: list[dict[str, Any]],
        stages: list[dict[str, Any]],
        model_infos: dict[str, dict[str, Any]],
        annotated: Any,
        helpers: dict[str, list[dict[str, Any]]],
        status: str,
        desc: str,
    ) -> dict[str, Any]:
        computed = [
            item for item in detections
            if item.get("meter_value") is not None
        ]
        value = computed[0].get("value", "0") if computed else "0"
        confidence = max(
            (float(item.get("confidence", 0.0)) for item in detections),
            default=0.0,
        )
        return {
            "ability": ability_name,
            "description": str(ability.get("desc", ability_name)),
            "status": status,
            "value": value,
            "desc": f"{ability.get('desc', ability_name)}: {desc}",
            "confidence": round(confidence, 6),
            "image_size": {
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
            },
            "roi": None,
            "calibration_polygons": calibration,
            "detections": detections,
            "stages": stages,
            "skipped_stages": [],
            "models": list(model_infos.values()),
            "inference_context": {
                "points": helpers.get("points", []),
                "boxes": helpers.get("boxes", []),
                "box_adjustments": helpers.get("box_adjustments", []),
                "meter_pipeline": True,
                "calibration_polygons": calibration,
            },
            "annotated_image": self._image_to_data_url(annotated),
            "compatibility": (
                "连续表计按原项目的 meter_p1、meter_p2、meter_p3 "
                "父级裁剪关系执行。"
            ),
        }

    def _annotate_meter(
        self,
        image: Any,
        detections: list[dict[str, Any]],
        calibration: list[list[list[int]]],
        points: Iterable[dict[str, Any]],
    ) -> Any:
        canvas = self._annotate(
            image, detections, calibration, "__portal_meter__", points
        )
        for detection in detections:
            keypoints = detection.get("keypoints") or {}
            center = keypoints.get("center_pointer", {}).get("point")
            pointer = keypoints.get("mark_pointer_black", {}).get("point")
            if center and pointer:
                self._cv2.arrowedLine(
                    canvas,
                    tuple(map(int, center)),
                    tuple(map(int, pointer)),
                    (0, 0, 255),
                    4,
                    self._cv2.LINE_AA,
                    tipLength=0.18,
                )
            for mark in detection.get("meter_marks") or []:
                point = mark.get("point")
                if not point:
                    continue
                px, py = int(point[0]), int(point[1])
                self._cv2.circle(canvas, (px, py), 8, (255, 120, 0), -1)
                if mark.get("value") not in (None, ""):
                    canvas = self._draw_cn_text(
                        canvas, str(mark["value"]), (px + 10, py), (255, 120, 0)
                    )
        return canvas

    # ------------------------------------------------------------------
    # src 调用链：完全按服务器约定执行 src/base + src/algorithm 脚本
    # ------------------------------------------------------------------

    def _ensure_src_paths(self) -> None:
        """把 ``src``、``src/base`` 和 ``src/algorithm`` 加入 ``sys.path``。

        ``src/base`` 与 ``src/algorithm`` 下的目录名本身就是顶层包名
        （``base_yolo_nv``、``light_pgzsd`` 等），与
        ``src/core/loader.py`` 服务端的 ``sys.path.append`` 行为一致。
        """
        with _SRC_PATH_LOCK:
            for directory in (
                self.root_dir / "src",
                self.root_dir / "src" / "base",
                self.root_dir / "src" / "algorithm",
                self.root_dir / "src" / "algorithm" / "base",
            ):
                key = str(directory)
                if key in _SRC_PATHS or not directory.is_dir():
                    continue
                if key not in sys.path:
                    sys.path.append(key)
                _SRC_PATHS.add(key)

    def _import_src_module(self, module_name: str) -> Any:
        """按服务器约定导入 ``src`` 下的顶层模块，并缓存结果。"""
        with self._model_lock:
            cached = self._src_modules.get(module_name)
            if cached is not None:
                return cached
        self._ensure_src_paths()
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise SourceChainError(
                f"无法导入 src 模块 {module_name}: {type(exc).__name__}: {exc}"
            ) from exc
        with self._model_lock:
            self._src_modules[module_name] = module
        return module

    def _load_src_result_types(self) -> tuple[type, Any, Any] | None:
        """导入并缓存 ``IRResult``/``IRState``/``DetResult`` 类型。"""
        if self._src_result_types is not None:
            return self._src_result_types
        try:
            module = self._import_src_module("algorithm.base.result")
        except SourceChainError:
            return None
        result_type = getattr(module, "IRResult", None)
        state_type = getattr(module, "IRState", None)
        if result_type is None or state_type is None:
            return None
        self._src_result_types = (
            result_type,
            state_type,
            getattr(module, "DetResult", None),
        )
        return self._src_result_types

    def _src_base_module_name(self, stage: dict[str, Any], path: Path) -> str:
        """把描述文件的 ``base`` 名映射到 ``src/base`` 的硬件专用模块名。

        描述文件写的是设备无关名（``base_yolo``、``base_ppocr``），
        ``src/core/loader.py`` 会按设备附上 ``_nv``、``_om`` 或 ``_rknn``
        后缀。本地权重是 ``.pt``，因此对应 ``_nv``。
        """
        base_name = str(stage.get("base", "")).strip()
        if not base_name:
            raise SourceChainError("流水线阶段缺少 base 字段，无法走 src 调用链。")
        if base_name.endswith(("_nv", "_om", "_rknn")):
            return base_name
        suffix = self.SOURCE_BASE_SUFFIXES.get(path.suffix.lower(), "_nv")
        return f"{base_name}{suffix}"

    def _src_patch_module_name(self, stage: dict[str, Any]) -> str:
        """读取阶段 ``patch`` 名，没有 patch 时回退到 ``base`` 名。"""
        patch_name = str(stage.get("patch", "")).strip()
        if patch_name:
            return patch_name
        return str(stage.get("base", "")).strip()

    def _build_prv_config(
        self,
        ability_name: str,
        ability: dict[str, Any],
        calibration: Any,
        helpers: dict[str, list[dict[str, Any]]],
        settings: dict[str, Any],
        width: int,
        height: int,
    ) -> dict[str, Any]:
        """构造与原项目 ``handler._core_infer`` 等价的 ``prv_config``。

        原脚本会直接索引 ``descriptor['conf']``、``params[ability]['marks']``
        等键，因此这里必须补齐这些字段，不能只填流水线信息。
        """
        descriptor = copy.deepcopy(ability)
        pipeline = descriptor.get("pipeline") or {}
        descriptor["pipeline"] = {
            int(key) if str(key).isdigit() else key: value
            for key, value in pipeline.items()
        }
        for field in (
            "cls_names",
            "select_cls",
            "trans_cls",
            "cls_conf",
            "kpt_names",
            "kpt_dims",
        ):
            if ability.get(field) is not None:
                descriptor[field] = copy.deepcopy(ability[field])
        descriptor.setdefault("conf", ability.get("conf", settings.get("conf", 0.25)))
        descriptor.setdefault("iou", ability.get("iou", settings.get("iou", 0.7)))
        if settings.get("conf") not in (None, ""):
            descriptor["conf"] = settings["conf"]
        if settings.get("iou") not in (None, ""):
            descriptor["iou"] = settings["iou"]

        params: dict[str, Any] = {ability_name: {}}
        polygons = self._normalise_calibration_polygons(calibration, width, height)
        if polygons:
            # 原项目的标定语义：多边形通过 on_intersection_output 交给
            # patch 脚本，由项目自己的 `_fetch_intersections_polygons`
            # 过滤框中心落在标定区的检测结果。
            params[ability_name]["on_intersection_output"] = [
                {
                    "type": "polygon",
                    "points": [
                        coordinate
                        for point in polygon
                        for coordinate in (int(point[0]), int(point[1]))
                    ],
                    "usage": "analyze",
                }
                for polygon in polygons
            ]
            needs_internal_roi = any(
                "crop_by_roi" in {
                    str(item).strip() for item in (stage.get("pre_process") or [])
                }
                for _, stage in self._pipeline_items(ability)
            )
            if needs_internal_roi:
                # 只有明确声明 crop_by_roi 的原脚本才接收这个兼容字段；
                # 网页和通用推理逻辑始终使用多边形标定区。
                bounds = self._polygon_bounds(polygons)
                if bounds is not None:
                    params[ability_name]["roi"] = bounds
        marks = []
        for point in helpers.get("points", []):
            if point.get("value") in (None, ""):
                continue
            try:
                value = float(point["value"])
            except (TypeError, ValueError):
                continue
            marks.append({"value": value, "point": [int(point["x"]), int(point["y"])]})
        if marks:
            params[ability_name]["marks"] = marks

        return {
            "task_id": datetime.now().strftime("%H%M%S%f"),
            "ability": ability_name,
            "descriptor": descriptor,
            "params": params,
            "normal_image": None,
            "pipeline_idx": 1,
        }

    def _source_stage_config(
        self,
        stage: dict[str, Any],
        ability: dict[str, Any],
        path: Path,
    ) -> dict[str, Any]:
        """构造基类构造函数所需的 config，字段名与描述文件保持一致。"""
        config = copy.deepcopy(stage)
        for field in (
            "cls_names",
            "select_cls",
            "trans_cls",
            "cls_conf",
            "kpt_names",
            "kpt_dims",
        ):
            if stage.get(field) is None and ability.get(field) is not None:
                config[field] = copy.deepcopy(ability[field])
        config["device"] = "cpu" if self.device == "auto" else self.device
        config["param"] = str(stage.get("param", ""))
        config["base"] = str(stage.get("base", ""))
        config["model_path"] = str(path)
        return config

    @staticmethod
    def _infer_name_for_stage(stage: dict[str, Any]) -> str:
        """无 patch 阶段按描述文件推断应调用的基类推理方法。"""
        if stage.get("kpt_names"):
            return "_infer_kpt"
        if "obb" in str(stage.get("base", "")).lower():
            return "_infer_obb"
        return "_infer_det"

    def _load_src_executor(
        self,
        stage: dict[str, Any],
        ability: dict[str, Any],
        path: Path,
    ) -> Any:
        """按 ``src/core/loader.py`` 的方式构建一个可调用的阶段执行器。"""
        base_module_name = self._src_base_module_name(stage, path)
        patch_module_name = self._src_patch_module_name(stage)
        cache_key = (base_module_name, patch_module_name, str(path.resolve()))
        with self._model_lock:
            cached = self._src_executors.get(cache_key)
        if cached is not None:
            return cached

        identifier = f"{patch_module_name}@{path.name}"
        self._log(f"src 调用链加载 {identifier}: base={base_module_name}")
        base_module = self._import_src_module(base_module_name)
        create_object = getattr(base_module, "create_base_core_object", None)
        if not callable(create_object):
            raise SourceChainError(
                f"src 模块 {base_module_name} 未提供 create_base_core_object。"
            )

        config = self._source_stage_config(stage, ability, path)
        try:
            executor = create_object(model_path=str(path), config=config)
        except Exception as exc:
            raise SourceChainError(
                f"src 调用链创建模型对象失败 {identifier}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if str(stage.get("patch", "")).strip():
            patch_module = self._import_src_module(patch_module_name)
            patch_functions = [
                name
                for name in dir(patch_module)
                if name.startswith("patch_") and callable(getattr(patch_module, name))
            ]
            if not patch_functions:
                raise SourceChainError(
                    f"src 算法模块 {patch_module_name} 没有可绑定的 patch_* 函数。"
                )
            for function_name in patch_functions:
                bound_name = function_name.split("patch_", 1)[1]
                function = getattr(patch_module, function_name)
                setattr(executor, bound_name, MethodType(function, executor))
            self._log(
                f"src 调用链绑定 {patch_module_name}: "
                f"{', '.join(sorted(patch_functions))}"
            )
        else:
            infer_name = self._infer_name_for_stage(stage)
            infer_function = getattr(base_module, infer_name, None)
            if not callable(infer_function):
                raise SourceChainError(
                    f"src 基类 {base_module_name} 缺少 {infer_name}，且阶段未声明 patch。"
                )
            setattr(executor, "infer", MethodType(infer_function, executor))

        with self._model_lock:
            self._src_executors[cache_key] = executor
        return executor

    def _src_ability_available(self, ability_name: str) -> bool:
        """判断能力是否可以在本地走 src 调用链。

        只做文件存在性检查，不导入模块：网页加载配置时不应加载全部权重。
        """
        cached = self._src_ability_cache.get(ability_name)
        if cached is not None:
            return cached
        available = not self._src_ability_unavailable_reason(ability_name)
        self._src_ability_cache[ability_name] = available
        return available

    def _src_ability_unavailable_reason(self, ability_name: str) -> str:
        """Return the concrete reason the descriptor-driven src chain is unavailable."""
        ability = self._abilities.get(ability_name)
        if not isinstance(ability, dict):
            return f"能力 {ability_name} 不存在于 model_descriptor.yaml。"
        stages = self._pipeline_items(ability)
        if not stages:
            return f"能力 {ability_name} 没有可执行 pipeline。"
        for stage_index, stage in stages:
            patch_name = str(stage.get("patch", "")).strip()
            base_name = str(stage.get("base", "")).strip()
            if not base_name:
                return f"P{stage_index} 未配置 base。"
            base_candidates = [base_name]
            if not base_name.endswith(("_nv", "_om", "_rknn")):
                base_candidates = [
                    f"{base_name}{suffix}"
                    for suffix in ("_nv", "_om", "_rknn")
                ]
            base_roots = (
                self.root_dir / "src" / "base",
                self.root_dir / "src" / "algorithm" / "base",
            )
            if not any(
                (root / candidate).is_dir() or (root / f"{candidate}.py").is_file()
                for root in base_roots
                for candidate in base_candidates
            ):
                return (
                    f"P{stage_index} 找不到 base 模块 {base_name}"
                    "（已检查 src/base 和 src/algorithm/base）。"
                )
            if patch_name and not (
                self.root_dir / "src" / "algorithm" / patch_name
            ).is_dir():
                return (
                    f"P{stage_index} 找不到 algorithm patch {patch_name}"
                    "（已检查 src/algorithm）。"
                )
        return ""

    @staticmethod
    def _src_result_objects(stage_output: Any, result_type: type) -> list[Any]:
        """把脚本返回值规范化为 ``IRResult`` 列表。"""
        if stage_output is None:
            return []
        items = (
            list(stage_output)
            if isinstance(stage_output, (list, tuple))
            else [stage_output]
        )
        return [item for item in items if isinstance(item, result_type)]

    @staticmethod
    def _src_stage_state(result_objects: list[Any], state_type: Any) -> str:
        """把 ``IRState`` 映射为网页使用的状态字符串。"""
        if not result_objects:
            return ""
        state = getattr(result_objects[0], "state", None)
        mapping = {
            getattr(state_type, "FINAL", None): "final",
            getattr(state_type, "INTERMEDIATE", None): "intermediate",
            getattr(state_type, "EMPTY", None): "empty",
            getattr(state_type, "ERROR", None): "error",
        }
        return mapping.get(state, "")

    @staticmethod
    def _src_flatten_detections(
        stage_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """取最后一个有检测框的阶段结果作为最终展示结果。"""
        for stage_result in reversed(stage_results):
            detections = stage_result.get("detections") or []
            if detections:
                return detections
        return []

    def _src_detections(
        self,
        result_objects: list[Any],
        stage: dict[str, Any],
        stage_index: int,
    ) -> list[dict[str, Any]]:
        """把 ``IRResult`` 中的框、关键点和返回值转成网页结构。"""
        detections: list[dict[str, Any]] = []
        for result_object in result_objects:
            box = getattr(result_object, "result", None)
            if box is None:
                continue
            try:
                top_left, bottom_right = box.xyxy
            except Exception:
                continue
            x1, y1 = int(top_left[0]), int(top_left[1])
            x2, y2 = int(bottom_right[0]), int(bottom_right[1])
            class_name = str(getattr(box, "cls_name", "") or "")
            prompt = str(getattr(result_object, "prompt_str", "") or "")
            value_result = getattr(result_object, "value_result", None) or {}
            detection: dict[str, Any] = {
                "stage": stage_index,
                "class_id": getattr(box, "cls_id", -1),
                "class_name": class_name,
                "display_name": self._src_display_name(class_name, prompt),
                "confidence": round(float(getattr(box, "conf", 0.0) or 0.0), 6),
                "bbox": [x1, y1, x2, y2],
                "center": [int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))],
            }
            keypoints = self._src_keypoints(box, stage)
            if keypoints:
                detection["keypoints"] = keypoints
            if isinstance(value_result, dict) and value_result.get("value") not in (
                None,
                "",
            ):
                detection["value"] = str(value_result["value"])
            detections.append(detection)
        return detections

    @staticmethod
    def _src_display_name(class_name: str, prompt: str) -> str:
        """优先使用脚本给出的描述文本，其次使用类别名。"""
        if prompt and ":" in prompt:
            tail = prompt.split(":", 1)[1].strip()
            if tail:
                return tail
        return class_name or prompt

    @staticmethod
    def _src_keypoints(
        box: Any, stage: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """读取 ``KptResult`` 关键点，保留描述文件中的名称顺序。"""
        names = [str(item) for item in (stage.get("kpt_names") or [])]
        if not names or not hasattr(box, "kpt_xyxy"):
            return {}
        keypoints: dict[str, dict[str, Any]] = {}
        for index, name in enumerate(names):
            try:
                point = box.kpt_xyxy(index)
            except Exception:
                continue
            if point is None or len(point) < 2:
                continue
            keypoints[name] = {"point": [int(point[0]), int(point[1])]}
        return keypoints

    @staticmethod
    def _src_stage_values(
        result_objects: list[Any],
        stage: dict[str, Any],
    ) -> dict[str, Any]:
        """从阶段结果提取 ``ret_keys`` 或脚本值，供后续阶段与网页使用。"""
        values: dict[str, Any] = {}
        keys = [str(item) for item in (stage.get("ret_keys") or [])]
        for result_object in result_objects:
            value_result = getattr(result_object, "value_result", None) or {}
            if not isinstance(value_result, dict):
                continue
            value = value_result.get("value")
            if value in (None, ""):
                continue
            if keys:
                for key in keys:
                    values.setdefault(key, value)
            else:
                values.setdefault("value", value)
        return values

    @staticmethod
    def _src_decode_value(value: Any) -> Any:
        """解码算法脚本常见的 JSON 字符串返回值。"""
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def _src_categories(
        self,
        result_objects: list[Any],
        stage: dict[str, Any],
        ability: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """按描述文件的 select_cls/trans_cls 解析脚本返回的类别结果。"""
        names = self._normalise_names(
            stage.get("cls_names", ability.get("cls_names", {}))
        )
        select_cls = [
            str(item)
            for item in (stage.get("select_cls") or ability.get("select_cls") or [])
        ]
        trans_cls = self._normalise_mapping(
            stage.get("trans_cls", ability.get("trans_cls", {}))
        )
        categories: list[dict[str, Any]] = []

        for result_object in result_objects:
            value_result = getattr(result_object, "value_result", None) or {}
            if not isinstance(value_result, dict):
                continue
            raw_value = value_result.get("value")
            if raw_value in (None, ""):
                continue
            payload = self._src_decode_value(raw_value)
            if isinstance(payload, dict):
                items = list(payload.items())
            elif isinstance(payload, list):
                items = [(str(index + 1), item) for index, item in enumerate(payload)]
            else:
                text = str(payload).strip()
                if text in {"", "0"}:
                    continue
                items = [("value", payload)]

            for source_key, item in items:
                class_id: int | None = None
                class_name = ""
                try:
                    class_id = int(item)
                except (TypeError, ValueError):
                    class_name = str(item).strip()
                if class_id is not None:
                    class_name = names.get(class_id, "")
                    if not class_name and 0 <= class_id < len(select_cls):
                        class_name = select_cls[class_id]
                    if not class_name and 1 <= class_id <= len(select_cls):
                        class_name = select_cls[class_id - 1]
                    if not class_name:
                        class_name = str(item)
                display_name = str(trans_cls.get(class_name, class_name or item))
                categories.append(
                    {
                        "source_key": str(source_key),
                        "class_id": class_id,
                        "class_name": class_name,
                        "display_name": display_name,
                        "value": item,
                        "confidence": None,
                    }
                )
        return categories

    def _src_related_image_to_bgr(self, related_image: str) -> Any:
        """把脚本返回的 base64 结果图解码为 OpenCV 图像。"""
        self._ensure_image_runtime()
        encoded = (
            related_image.split(",", 1)[1] if "," in related_image else related_image
        )
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            return None
        return self._cv2.imdecode(
            self._numpy.frombuffer(raw, dtype=self._numpy.uint8),
            self._cv2.IMREAD_COLOR,
        )

    def _run_src_pipeline(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        model_override: str | None,
        stage_models: dict[str, Any] | list[dict[str, Any]] | None,
        helpers: dict[str, list[dict[str, Any]]],
        settings: dict[str, Any],
        request_logs: list[str],
    ) -> dict[str, Any]:
        """完全按服务器约定执行 src/base + src/algorithm 多段推理。"""
        stages = self._pipeline_items(ability)
        if not stages:
            raise LocalInferenceError(f"能力没有可执行流水线: {ability_name}")

        result_types = self._load_src_result_types()
        if result_types is None:
            raise SourceChainError("无法加载 algorithm.base.result，src 调用链不可用。")
        result_type, state_type, _ = result_types

        prv_config = self._build_prv_config(
            ability_name,
            ability,
            calibration,
            helpers,
            settings,
            image.shape[1],
            image.shape[0],
        )
        prv_image = image
        prv_result: list[Any] | None = None
        stage_results: list[dict[str, Any]] = []
        ret_keys = [
            str(item)
            for _, stage in stages
            for item in (stage.get("ret_keys") or [])
        ]
        returned_values: dict[str, Any] = {}
        final_related_image = ""
        final_state = "empty"
        categories: list[dict[str, Any]] = []

        for stage_index, stage in stages:
            candidate, resolution = self._resolve_model(
                stage.get("param"),
                self._stage_override(stage_models, stage_index) or model_override,
                stage,
            )
            executor = self._load_src_executor(stage, ability, candidate.absolute_path)
            prv_config["pipeline_idx"] = stage_index
            stage_started = time.perf_counter()
            try:
                prv_image, stage_output, prv_config = executor.infer(
                    prv_image, prv_result, prv_config
                )
            except Exception as exc:
                hint = ""
                if isinstance(exc, KeyError):
                    missing = str(exc).strip("'\"")
                    if missing == "marks":
                        hint = "；该能力需要在网页“点绘制”里补充带数值的标定点"
                    elif missing == "roi":
                        hint = "；该能力需要先绘制标定框"
                    elif missing.startswith("p1_") or missing.startswith("p2_"):
                        hint = "；该阶段依赖上一阶段输出，请确认前置阶段识别成功"
                raise SourceChainError(
                    f"src 算法阶段 P{stage_index} "
                    f"({stage.get('patch', stage.get('base', ''))}) 执行失败: "
                    f"{type(exc).__name__}: {exc}{hint}"
                ) from exc
            elapsed_ms = round((time.perf_counter() - stage_started) * 1000, 2)

            result_objects = self._src_result_objects(stage_output, result_type)
            stage_state = self._src_stage_state(result_objects, state_type)
            # EMPTY/ERROR 的占位框不是真实检测结果：服务器语义里
            # 它们代表“没有识别到目标”，不应出现在网页检测列表中。
            detections = (
                []
                if stage_state in {"empty", "error"}
                else self._src_detections(result_objects, stage, stage_index)
            )
            values = self._src_stage_values(result_objects, stage)
            stage_categories = (
                []
                if stage_state in {"empty", "error"}
                else self._src_categories(result_objects, stage, ability)
            )
            categories.extend(stage_categories)
            returned_values.update(
                {key: value for key, value in values.items() if value not in (None, "")}
            )
            if stage_state:
                final_state = stage_state
            for result_object in result_objects:
                related = getattr(result_object, "related_image_str", "") or ""
                if related:
                    final_related_image = related

            stage_results.append(
                {
                    "index": stage_index,
                    "base": stage.get("base", ""),
                    "patch": stage.get("patch", ""),
                    "parameter": stage.get("param", ""),
                    "executor": "src",
                    "model_path": candidate.relative_path,
                    "model_resolution": resolution,
                    "source_module": self._src_patch_module_name(stage),
                    "state": stage_state,
                    "status": stage_state or "ok",
                    "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
                    "values": values,
                    "raw_count": len(result_objects),
                    "returned_count": len(detections),
                    "elapsed_ms": elapsed_ms,
                    "detections": copy.deepcopy(detections),
                    "categories": copy.deepcopy(stage_categories),
                    "models": [],
                }
            )
            self._log(
                f"src 调用链阶段 {stage_index}: "
                f"{stage.get('patch', stage.get('base', ''))}, "
                f"state={stage_state or 'ok'}, returns={len(result_objects)}, "
                f"elapsed_ms={elapsed_ms}",
                request_logs,
            )
            prv_result = result_objects

            if stage_state in {"empty", "error"}:
                # 与服务器一致：EMPTY/ERROR 表示流水线中断信号。
                self._log(
                    f"src 调用链阶段 {stage_index} 返回 {stage_state}，提前结束流水线。",
                    request_logs,
                )
                break

        return self._src_response(
            image=image,
            ability_name=ability_name,
            ability=ability,
            calibration=calibration,
            helpers=helpers,
            stage_results=stage_results,
            ret_keys=ret_keys,
            returned_values=returned_values,
            categories=categories,
            final_related_image=final_related_image,
            final_state=final_state,
        )

    def _src_response(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        calibration: list[list[list[int]]],
        helpers: dict[str, list[dict[str, Any]]],
        stage_results: list[dict[str, Any]],
        ret_keys: list[str],
        returned_values: dict[str, Any],
        categories: list[dict[str, Any]],
        final_related_image: str,
        final_state: str,
    ) -> dict[str, Any]:
        """把 src 调用链的阶段结果整理成网页消费的响应。"""
        detections = self._src_flatten_detections(stage_results)
        annotated = None
        if final_related_image:
            annotated = self._src_related_image_to_bgr(final_related_image)
        if annotated is None:
            annotated = self._annotate(
                image,
                detections,
                calibration,
                ability_name,
                helpers.get("points", []),
            )
        else:
            annotated = self._draw_calibration_overlay(annotated, calibration)
        for index, category in enumerate(categories):
            source_key = str(category.get("source_key", f"结果 {index + 1}"))
            display_name = str(category.get("display_name", category.get("value", "")))
            annotated = self._draw_cn_text(
                annotated,
                f"{source_key}={display_name}",
                (12, 30 + index * 28),
                (0, 215, 255),
            )
        max_conf = max(
            (float(item.get("confidence", 0.0)) for item in detections), default=0.0
        )
        value = next(
            (item for item in returned_values.values() if item not in (None, "")),
            "1" if detections else "0",
        )
        described = (
            "; ".join(
                f"{item.get('source_key', '结果')}={item.get('display_name', '')}"
                for item in categories
            )
            or "; ".join(
                f"{key}={item}"
                for key, item in returned_values.items()
                if item not in (None, "")
            )
        )
        first_name = (
            categories[0]["display_name"]
            if categories
            else detections[0]["display_name"]
            if detections
            else "正常"
        )
        # 状态以最终阶段为准，与服务器 IRState 语义保持一致：
        # FINAL/INTERMEDIATE 表示识别到目标，EMPTY/ERROR 表示未识别到。
        if final_state in {"empty", "error"}:
            status = "empty"
        elif final_state in {"final", "intermediate"}:
            status = "detected"
        else:
            status = "detected" if detections else "empty"
        return {
            "ability": ability_name,
            "description": str(ability.get("desc", ability_name)),
            "status": status,
            "value": value,
            "desc": described or f"{ability.get('desc', ability_name)}: {first_name}",
            "confidence": round(max_conf, 6),
            "image_size": {
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
            },
            "roi": None,
            "calibration_polygons": calibration,
            "detections": detections,
            "categories": categories,
            "stages": stage_results,
            "skipped_stages": [],
            "models": [],
            "inference_context": {
                "points": helpers.get("points", []),
                "boxes": helpers.get("boxes", []),
                "box_adjustments": helpers.get("box_adjustments", []),
                "ret_keys": ret_keys,
                "values": returned_values,
                "calibration_polygons": calibration,
                "source_chain": True,
            },
            "annotated_image": self._image_to_data_url(annotated),
            "compatibility": (
                "按 src/base + src/algorithm 原脚本调用链执行多段推理，"
                "与服务器 handler._core_infer 的流水线语义一致。"
            ),
        }

    def _run_stage(
        self,
        source: Any,
        offset: tuple[int, int],
        ability: dict[str, Any],
        stage_index: int,
        stage: dict[str, Any],
        model_override: str | None,
        settings: dict[str, Any],
        request_logs: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """分派一个流水线阶段到对应的本地执行器。"""
        executor = self._stage_executor(stage)
        selected_name = str(model_override or "").lower()
        if (
            executor in {"crnn", "ppocr"}
            and ("paddleocr" in selected_name or "ocr-meter-common-paddleocr" in selected_name)
        ):
            executor = "ppocr"
        if executor == "yolo":
            return self._run_yolo_stage(
                source,
                offset,
                ability,
                stage_index,
                stage,
                model_override,
                settings,
                request_logs,
            )
        if executor == "crnn":
            return self._run_crnn_stage(
                source,
                offset,
                ability,
                stage_index,
                stage,
                model_override,
                request_logs,
            )
        if executor == "ppocr":
            return self._run_ppocr_stage(
                source,
                offset,
                ability,
                stage_index,
                stage,
                model_override,
                request_logs,
            )
        raise LocalInferenceError(
            f"流水线阶段 P{stage_index} 没有可用的本地执行器: {stage.get('base', '')}"
        )

    def _run_crnn_stage(
        self,
        source: Any,
        offset: tuple[int, int],
        ability: dict[str, Any],
        stage_index: int,
        stage: dict[str, Any],
        model_override: str | None,
        request_logs: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """执行本地 CRNN 阶段，并把 OCR 文本包装成统一结果。"""
        candidate, resolution = self._resolve_model(
            stage.get("param"), model_override, stage
        )
        model = self._load_crnn(candidate.absolute_path, stage)
        started_at = time.perf_counter()
        try:
            value = model._infer(source)
            if isinstance(value, tuple):
                value = value[0]
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            text = str(value or "").strip()
        except Exception as exc:
            raise LocalInferenceError(
                f"CRNN 推理失败 {candidate.name}: {type(exc).__name__}: {exc}"
            ) from exc
        height, width = source.shape[:2]
        bbox = [
            int(offset[0]),
            int(offset[1]),
            int(offset[0] + width),
            int(offset[1] + height),
        ]
        ret_keys = [str(item) for item in (stage.get("ret_keys") or [])]
        detection = {
            "stage": stage_index,
            "class_id": -1,
            "class_name": ret_keys[0] if ret_keys else "ocr",
            "display_name": text or "未识别",
            "text": text,
            "value": text,
            "confidence": 1.0 if text else 0.0,
            "bbox": bbox,
            "center": [
                int(round((bbox[0] + bbox[2]) / 2)),
                int(round((bbox[1] + bbox[3]) / 2)),
            ],
            "ret_keys": ret_keys,
        }
        detections = [detection] if text else []
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        key = f"crnn|{candidate.absolute_path.resolve()}|{stage.get('dictionary', '')}"
        model_info = copy.deepcopy(self._model_infos.get(key, {}))
        stage_info = {
            "index": stage_index,
            "base": stage.get("base", ""),
            "patch": stage.get("patch", ""),
            "parameter": stage.get("param", ""),
            "executor": "crnn",
            "model_path": candidate.relative_path,
            "model_resolution": resolution,
            "ret_keys": ret_keys,
            "values": {},
            "raw_count": 1 if text else 0,
            "returned_count": len(detections),
            "elapsed_ms": elapsed_ms,
            "detections": detections,
            "models": [model_info] if model_info else [],
        }
        self._log(
            f"流水线阶段 {stage_index}: executor=crnn, model={candidate.name}, "
            f"returned={len(detections)}, elapsed_ms={elapsed_ms}",
            request_logs,
        )
        return detections, stage_info

    def _run_ppocr_stage(
        self,
        source: Any,
        offset: tuple[int, int],
        ability: dict[str, Any],
        stage_index: int,
        stage: dict[str, Any],
        model_override: str | None,
        request_logs: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """执行 PaddleOCR 阶段，并将识别文本包装成统一检测结果。"""
        candidate, resolution = self._resolve_model(
            stage.get("param"), model_override, stage
        )
        model = self._load_ppocr(candidate.absolute_path, stage)
        started_at = time.perf_counter()
        try:
            value = model._infer(source)
            if isinstance(value, tuple):
                value = value[0]
            text = str(value or "").strip()
        except Exception as exc:
            raise LocalInferenceError(
                f"PaddleOCR 推理失败 {candidate.name}: {type(exc).__name__}: {exc}"
            ) from exc
        height, width = source.shape[:2]
        bbox = [
            int(offset[0]),
            int(offset[1]),
            int(offset[0] + width),
            int(offset[1] + height),
        ]
        ret_keys = [str(item) for item in (stage.get("ret_keys") or [])]
        detection = {
            "stage": stage_index,
            "class_id": -1,
            "class_name": ret_keys[0] if ret_keys else "ocr",
            "display_name": text or "未识别",
            "text": text,
            "value": text,
            "confidence": 1.0 if text else 0.0,
            "bbox": bbox,
            "center": [
                int(round((bbox[0] + bbox[2]) / 2)),
                int(round((bbox[1] + bbox[3]) / 2)),
            ],
            "ret_keys": ret_keys,
        }
        detections = [detection] if text else []
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        stage_info = {
            "index": stage_index,
            "base": stage.get("base", ""),
            "patch": stage.get("patch", ""),
            "parameter": stage.get("param", ""),
            "executor": "ppocr",
            "model_path": candidate.relative_path,
            "model_resolution": resolution,
            "ret_keys": ret_keys,
            "values": {},
            "raw_count": 1 if text else 0,
            "returned_count": len(detections),
            "elapsed_ms": elapsed_ms,
            "detections": detections,
            "models": [],
        }
        self._log(
            f"流水线阶段 {stage_index}: executor=ppocr, model={candidate.name}, "
            f"returned={len(detections)}, elapsed_ms={elapsed_ms}",
            request_logs,
        )
        return detections, stage_info

    def _run_yolo_stage(
        self,
        source: Any,
        offset: tuple[int, int],
        ability: dict[str, Any],
        stage_index: int,
        stage: dict[str, Any],
        model_override: str | None,
        settings: dict[str, Any],
        request_logs: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """执行单个 YOLO 阶段并按照描述文件转换检测结果。"""
        candidate, resolution = self._resolve_model(
            stage.get("param"), model_override, stage
        )
        if resolution != "exact" and resolution != "user-selected":
            self._log(
                f"阶段 {stage_index} 使用模型回退方式 {resolution}: "
                f"{candidate.name} (descriptor={stage.get('param', '')})",
                request_logs,
            )
        model = self._load_yolo(candidate.absolute_path)
        stage_conf = settings.get("conf")
        if stage_conf in (None, ""):
            stage_conf = stage.get("conf", ability.get("conf", 0.25))
        stage_iou = settings.get("iou")
        if stage_iou in (None, ""):
            stage_iou = stage.get("iou", ability.get("iou", 0.7))
        stage_imgsz = settings.get("imgsz")
        if stage_imgsz in (None, ""):
            stage_imgsz = stage.get("imgsz", 640)
        try:
            confidence = max(0.0, min(1.0, float(stage_conf)))
            iou = max(0.0, min(1.0, float(stage_iou)))
            imgsz = int(stage_imgsz)
        except (TypeError, ValueError) as exc:
            raise LocalInferenceError("conf、iou、imgsz 必须是数字。") from exc

        stage_started = time.perf_counter()
        try:
            raw_results = model.predict(
                source=source,
                device=self.device,
                imgsz=imgsz,
                iou=iou,
                conf=confidence,
                half=False,
                verbose=False,
                save=False,
            )
        except Exception as exc:
            raise LocalInferenceError(
                f"模型推理失败 {candidate.name}: {type(exc).__name__}: {exc}"
            ) from exc
        if not raw_results:
            raw_result = None
        else:
            raw_result = raw_results[0]
        detections, raw_count = self._extract_detections(
            raw_result, offset, ability, stage
        )
        for detection in detections:
            detection["stage"] = stage_index
        elapsed_ms = round((time.perf_counter() - stage_started) * 1000, 2)
        model_info = copy.deepcopy(
            self._model_infos.get(str(candidate.absolute_path), {})
        )
        stage_info = {
            "index": stage_index,
            "base": stage.get("base", ""),
            "patch": stage.get("patch", ""),
            "parameter": stage.get("param", ""),
            "executor": "yolo",
            "model_path": candidate.relative_path,
            "model_resolution": resolution,
            "conf": confidence,
            "iou": iou,
            "imgsz": imgsz,
            "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
            "values": self._stage_values(detections, stage),
            "raw_count": raw_count,
            "returned_count": len(detections),
            "elapsed_ms": elapsed_ms,
            "detections": detections,
            "models": [model_info] if model_info else [],
        }
        self._log(
            f"流水线阶段 {stage_index}: model={candidate.name}, "
            f"raw={raw_count}, returned={len(detections)}, "
            f"conf={confidence}, imgsz={imgsz}, elapsed_ms={elapsed_ms}",
            request_logs,
        )
        return detections, stage_info

    @staticmethod
    def _stage_values(
        detections: list[dict[str, Any]], stage: dict[str, Any]
    ) -> dict[str, Any]:
        """从阶段结果生成供下一阶段或最终结果消费的键值。"""
        keys = [str(item) for item in (stage.get("ret_keys") or [])]
        values: dict[str, Any] = {}
        for key in keys:
            for detection in detections:
                if key in {
                    str(detection.get("class_name", "")),
                    str(detection.get("display_name", "")),
                }:
                    values[key] = detection.get(
                        "text",
                        detection.get("value", detection.get("display_name")),
                    )
                    break
        return values

    def _extract_detections(
        self,
        raw_result: Any,
        offset: tuple[int, int],
        ability: dict[str, Any],
        stage: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        """从 Ultralytics 结果中提取检测框并应用项目过滤规则。"""
        if raw_result is None or getattr(raw_result, "boxes", None) is None:
            return [], 0
        boxes = raw_result.boxes.cpu()
        raw_count = len(boxes)
        model_names = self._normalise_names(getattr(raw_result, "names", {}))
        descriptor_names = self._normalise_names(
            stage.get("cls_names", ability.get("cls_names", {}))
        )
        selected = stage.get("select_cls")
        if selected is None:
            selected = ability.get("select_cls")
        selected_names = {str(name) for name in selected} if selected else None
        translated = self._normalise_mapping(
            stage.get("trans_cls", ability.get("trans_cls"))
        )
        class_thresholds = self._normalise_mapping(
            stage.get("cls_conf", ability.get("cls_conf"))
        )
        detections: list[dict[str, Any]] = []
        for box_index, box in enumerate(boxes):
            class_id = int(box.cls.item())
            confidence = float(box.conf.item())
            # 本地 .pt 的 names 是训练时的真实类别；描述文件 cls_names
            # 作为兼容旧模型或缺少 names 元数据时的回退。
            raw_name = model_names.get(
                class_id, descriptor_names.get(class_id, f"class_{class_id}")
            )
            if selected_names is not None and raw_name not in selected_names:
                continue
            if class_thresholds:
                threshold = class_thresholds.get(raw_name)
                if threshold is None:
                    continue
                if confidence < float(threshold):
                    continue
            coordinates = [float(value) for value in box.xyxy[0].tolist()]
            x1, y1, x2, y2 = coordinates
            x1 += offset[0]
            y1 += offset[1]
            x2 += offset[0]
            y2 += offset[1]
            detection = {
                "class_id": class_id,
                "class_name": raw_name,
                "display_name": str(translated.get(raw_name, raw_name)),
                "confidence": round(confidence, 6),
                "bbox": [
                    int(round(x1)),
                    int(round(y1)),
                    int(round(x2)),
                    int(round(y2)),
                ],
                "center": [
                    int(round((x1 + x2) / 2)),
                    int(round((y1 + y2) / 2)),
                ],
            }
            keypoints = self._extract_keypoints(raw_result, box_index, offset, stage)
            if keypoints:
                detection["keypoints"] = keypoints
                direction = self._direction_from_keypoints(keypoints, stage)
                if direction is not None:
                    direction_name, direction_value = direction
                    detection["class_name"] = direction_name
                    detection["display_name"] = str(
                        translated.get(direction_name, direction_name)
                    )
                    detection["direction"] = direction_name
                    detection["direction_value"] = direction_value
                    detection["value"] = str(direction_value)
            detections.append(detection)
        return detections, raw_count

    @staticmethod
    def _direction_from_keypoints(
        keypoints: dict[str, dict[str, Any]], stage: dict[str, Any]
    ) -> tuple[str, int] | None:
        """将声明了八方向类别的 tail/head 关键点转换为方向结果。"""
        direction_names = [
            "right",
            "right_up",
            "up",
            "left_up",
            "left",
            "left_bottom",
            "bottom",
            "right_bottom",
        ]
        selected = {str(item) for item in (stage.get("select_cls") or [])}
        if not set(direction_names).issubset(selected):
            return None
        names = [str(item) for item in (stage.get("kpt_names") or [])]
        if len(names) < 2:
            return None
        tail = keypoints.get(names[0])
        head = keypoints.get(names[1])
        if not isinstance(tail, dict) or not isinstance(head, dict):
            return None
        tail_point = tail.get("point")
        head_point = head.get("point")
        if (
            not isinstance(tail_point, (list, tuple))
            or not isinstance(head_point, (list, tuple))
            or len(tail_point) < 2
            or len(head_point) < 2
        ):
            return None
        x1, y1 = float(tail_point[0]), float(tail_point[1])
        x2, y2 = float(head_point[0]), float(head_point[1])
        if x1 == x2 and y1 == y2:
            return None
        # 与 src/base/base_yolo_nv/yolov8.py 保持一致：图像坐标的 Y 轴反向。
        theta = math.degrees(math.atan2(y1 - y2, x2 - x1)) % 360
        thresholds = [22.5, 67.5, 112.5, 157.5, 202.5, 247.5, 292.5, 337.5]
        for index, threshold in enumerate(thresholds):
            in_first_wrap = index == 0 and (
                0 <= theta < threshold or 337.5 <= theta <= 360
            )
            in_range = index > 0 and thresholds[index - 1] <= theta < threshold
            if in_first_wrap or in_range:
                return direction_names[index], index + 1
        return None

    def _extract_keypoints(
        self,
        raw_result: Any,
        detection_index: int,
        offset: tuple[int, int],
        stage: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """提取 Ultralytics pose 结果并按描述文件名称输出关键点。"""
        keypoint_result = getattr(raw_result, "keypoints", None)
        xy = getattr(keypoint_result, "xy", None)
        if xy is None:
            return {}
        try:
            if hasattr(xy, "cpu"):
                xy = xy.cpu().numpy()
            else:
                xy = self._numpy.asarray(xy)
            if len(xy.shape) < 3 or detection_index >= xy.shape[0]:
                return {}
            confidence_values = getattr(keypoint_result, "conf", None)
            if confidence_values is not None and hasattr(confidence_values, "cpu"):
                confidence_values = confidence_values.cpu().numpy()
            names = [str(item) for item in (stage.get("kpt_names") or [])]
            result: dict[str, dict[str, Any]] = {}
            for point_index, point in enumerate(xy[detection_index]):
                if len(point) < 2:
                    continue
                name = names[point_index] if point_index < len(names) else f"kpt_{point_index}"
                x = int(round(float(point[0]) + offset[0]))
                y = int(round(float(point[1]) + offset[1]))
                item: dict[str, Any] = {"point": [x, y]}
                if confidence_values is not None:
                    try:
                        item["confidence"] = round(
                            float(confidence_values[detection_index][point_index]), 6
                        )
                    except (IndexError, TypeError, ValueError):
                        pass
                result[name] = item
            return result
        except (AttributeError, TypeError, ValueError):
            return {}

    def _infer_image_black(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        roi: list[int] | None,
        request_logs: list[str],
    ) -> dict[str, Any]:
        """复现项目中黑屏能力的本地 OpenCV 判断逻辑。"""
        self._ensure_image_runtime()
        source = image
        offset = (0, 0)
        if roi:
            x1, y1, x2, y2 = roi
            source = image[y1:y2, x1:x2]
            offset = (x1, y1)
        gray = self._cv2.cvtColor(source, self._cv2.COLOR_BGR2GRAY)
        brightness = float(self._cv2.mean(gray)[0])
        is_dark = brightness < 100
        self._log(
            f"黑屏检测: average_brightness={brightness:.2f}, dark={is_dark}",
            request_logs,
        )
        detections: list[dict[str, Any]] = []
        if is_dark:
            x1, y1 = offset
            x2, y2 = x1 + source.shape[1], y1 + source.shape[0]
            detections.append(
                {
                    "class_id": 0,
                    "class_name": "black",
                    "display_name": "画面黑屏",
                    "confidence": 1.0,
                    "bbox": [x1, y1, x2, y2],
                    "center": [int((x1 + x2) / 2), int((y1 + y2) / 2)],
                }
            )
        annotated = self._annotate(image, detections, roi, ability_name)
        return {
            "ability": ability_name,
            "description": str(ability.get("desc", ability_name)),
            "status": "detected" if detections else "empty",
            "value": "1" if detections else "0",
            "desc": (
                f"{ability.get('desc', ability_name)}: "
                f"{detections[0]['display_name'] if detections else '正常'}"
            ),
            "confidence": 1.0 if detections else 0.0,
            "image_size": {"width": int(image.shape[1]), "height": int(image.shape[0])},
            "roi": roi,
            "detections": detections,
            "stages": [
                {
                    "index": 1,
                    "base": "opencv",
                    "patch": "image_black",
                    "parameter": "",
                    "raw_count": 1 if is_dark else 0,
                    "returned_count": len(detections),
                    "elapsed_ms": 0,
                    "detections": detections,
                    "operators": ["cv2.cvtColor", "cv2.mean"],
                }
            ],
            "models": [],
            "annotated_image": self._image_to_data_url(annotated),
            "compatibility": "本地复现 src/algorithm/image_black 的亮度阈值判断。",
        }

    def _simhei_font_path(self) -> str | None:
        """返回项目内置的 SimHei.ttf 中文字体路径，找不到返回 None。"""
        if self._cn_font_cache is not None:
            return self._cn_font_cache
        candidates = [
            self.root_dir / "src" / "SimHei.ttf",
            self.root_dir / "src" / "algorithm" / "base" / "SimHei.ttf",
            self.root_dir / "src" / "algorithm" / "crnn" / "SimHei.ttf",
        ]
        for candidate in candidates:
            if candidate.is_file():
                self._cn_font_cache = str(candidate)
                return self._cn_font_cache
        self._cn_font_cache = ""
        return None

    def _draw_cn_text(
        self,
        image: Any,
        text: str,
        org: tuple[int, int],
        color: tuple[int, int, int],
    ) -> Any:
        """在 BGR 图片上绘制支持中文的文本。

        使用项目 ``src/SimHei.ttf`` 字体；字体缺失时退回 OpenCV 内置
        Hershey 字体（此时中文仍可能显示为占位符号，但不会因此崩溃）。
        """
        font_path = self._simhei_font_path()
        if font_path is None:
            self._cv2.putText(
                image,
                text,
                (int(org[0]), int(org[1])),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.68,
                color,
                2,
                self._cv2.LINE_AA,
            )
            return image
        try:
            import numpy as _np
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            try:
                self._cv2.putText(
                    image,
                    text,
                    (int(org[0]), int(org[1])),
                    self._cv2.FONT_HERSHEY_SIMPLEX,
                    0.68,
                    color,
                    2,
                    self._cv2.LINE_AA,
                )
            except Exception:
                pass
            return image
        pil_image = Image.fromarray(self._cv2.cvtColor(image, self._cv2.COLOR_BGR2RGB))
        font = ImageFont.truetype(font_path, 20)
        draw = ImageDraw.Draw(pil_image)
        # OpenCV 的 org 是文字基线，PIL 的坐标是文字左上角，这里近似补偿行高。
        draw.text(
            (int(org[0]), int(org[1]) - 16),
            text,
            font=font,
            fill=(int(color[2]), int(color[1]), int(color[0])),
        )
        return self._cv2.cvtColor(
            _np.asarray(pil_image), self._cv2.COLOR_RGB2BGR
        )

    def _annotate(
        self,
        image: Any,
        detections: Iterable[dict[str, Any]],
        calibration: Any,
        ability_name: str,
        points: Iterable[dict[str, Any]] | None = None,
    ) -> Any:
        """在图片上绘制标定框、检测框和置信度标签。"""
        canvas = image.copy()
        canvas = self._draw_calibration_overlay(canvas, calibration)
        for index, detection in enumerate(detections):
            x1, y1, x2, y2 = detection["bbox"]
            color = self._box_color(index)
            self._cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 3)
            label = (
                f"{detection['display_name']} "
                f"{detection['confidence']:.3f}"
            )
            text_y = y1 - 8 if y1 > 28 else y1 + 24
            canvas = self._draw_cn_text(canvas, label, (x1, text_y), color)
            for point in (detection.get("keypoints") or {}).values():
                coordinates = point.get("point") if isinstance(point, dict) else point
                if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
                    continue
                px, py = int(coordinates[0]), int(coordinates[1])
                self._cv2.circle(canvas, (px, py), 6, color, -1)
            keypoint_names = list((detection.get("keypoints") or {}).keys())
            if len(keypoint_names) >= 2:
                first = (detection["keypoints"] or {}).get(keypoint_names[0], {})
                second = (detection["keypoints"] or {}).get(keypoint_names[1], {})
                first_point = first.get("point") if isinstance(first, dict) else first
                second_point = second.get("point") if isinstance(second, dict) else second
                if (
                    isinstance(first_point, (list, tuple))
                    and isinstance(second_point, (list, tuple))
                    and len(first_point) >= 2
                    and len(second_point) >= 2
                ):
                    self._cv2.arrowedLine(
                        canvas,
                        (int(first_point[0]), int(first_point[1])),
                        (int(second_point[0]), int(second_point[1])),
                        color,
                        3,
                        self._cv2.LINE_AA,
                        tipLength=0.2,
                )
        for point in points or []:
            px, py = int(point["x"]), int(point["y"])
            self._cv2.drawMarker(
                canvas,
                (px, py),
                (0, 80, 255),
                markerType=self._cv2.MARKER_CROSS,
                markerSize=18,
                thickness=2,
            )
            self._cv2.putText(
                canvas,
                str(point["id"]),
                (px + 8, max(18, py - 8)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 80, 255),
                2,
                self._cv2.LINE_AA,
            )
        return canvas

    def _draw_calibration_overlay(self, image: Any, calibration: Any) -> Any:
        """在已有图片上绘制网页提交的多边形标定框。"""
        canvas = image
        polygons = self._normalise_calibration_polygons(
            calibration, canvas.shape[1], canvas.shape[0]
        )
        for index, polygon in enumerate(polygons, start=1):
            points = self._numpy.asarray(polygon, dtype=self._numpy.int32).reshape(
                -1, 1, 2
            )
            self._cv2.polylines(
                canvas,
                [points],
                True,
                (0, 215, 255),
                3,
            )
            x, y = points[0][0]
            canvas = self._draw_cn_text(
                canvas,
                f"标定框 {index}",
                (int(x) + 8, max(24, int(y) + 24)),
                (0, 215, 255),
            )
        return canvas

    @staticmethod
    def _box_color(index: int) -> tuple[int, int, int]:
        """为不同检测框生成稳定的 BGR 颜色。"""
        palette = [
            (52, 211, 153),
            (56, 189, 248),
            (251, 191, 36),
            (244, 114, 182),
            (167, 139, 250),
        ]
        return palette[index % len(palette)]

    def _image_to_data_url(self, image: Any) -> str:
        """将 OpenCV 图片编码成网页可直接展示的 JPEG Data URL。"""
        ok, encoded = self._cv2.imencode(".jpg", image, [self._cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise LocalInferenceError("推理结果图片编码失败。")
        encoded_string = base64.b64encode(encoded.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded_string}"

    def recent_logs(self, limit: int = 200) -> list[str]:
        """返回最近的引擎日志。"""
        with self._log_lock:
            records = list(self._log_records)
        return records[-max(1, min(int(limit), len(records) or 1)) :]

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        """把 torch、NumPy 等对象转换成 JSON 兼容值。"""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): LocalInferenceEngine._to_jsonable(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [LocalInferenceEngine._to_jsonable(item) for item in value]
        if hasattr(value, "tolist"):
            return LocalInferenceEngine._to_jsonable(value.tolist())
        return str(value)
