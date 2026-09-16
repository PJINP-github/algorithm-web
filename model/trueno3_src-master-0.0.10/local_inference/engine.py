"""本地模型推理引擎。

功能：
    读取 ``storage/model_descriptor.yaml``，解析能力和流水线配置，
    在 ``model/`` 中查找本地权重，并通过 Ultralytics 执行 CPU 或 CUDA
    推理。引擎还复现了项目中最常用的 ROI 裁剪、检测框中心点筛选、
    类别筛选、类别置信度筛选以及多阶段裁剪推理行为。

设计说明：
    服务器版本依赖 ``storage/params``、硬件专用基类和编译授权文件， 
    这些条件不适合本机开发。因此本模块保持描述文件的数据契约，
    将模型加载和检测结果转换实现为一个独立的本地适配层。
"""

from __future__ import annotations

import base64
import copy
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
from typing import Any, Iterable


# 模块级 src 日志单例缓存，避免重复创建 WeeklyLogger 时添加重复 handler。
_SRC_LOGGER_LOCK = threading.Lock()
_SRC_LOGGERS: dict[tuple[str, str], logging.Logger] = {}


class LocalInferenceError(RuntimeError):
    """本地推理相关的可预期错误。"""


class LocalDependencyError(LocalInferenceError):
    """本机缺少推理依赖时抛出的错误。"""


class ModelResolutionError(LocalInferenceError):
    """无法从本地模型目录解析权重时抛出的错误。"""


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
        self.root_dir = Path(root_dir or Path(__file__).resolve().parents[1]).resolve()
        self.descriptor_path = Path(
            descriptor_path or self.root_dir / "storage" / "model_descriptor.yaml"
        ).resolve()
        self.model_dir = Path(model_dir or self.root_dir / "model").resolve()
        self.additional_model_dirs = []
        for directory in additional_model_dirs or []:
            resolved = Path(directory).resolve()
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
            dictionary_path = Path(dictionary)
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
                from base.base_crnn_nv.base_crnn import CRNN

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
                from base.base_ppocr_nv.base_ppocr import BasePPOCR

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
            for index, stage in self._pipeline_items(ability):
                base_name = str(stage.get("base", ""))
                executor = self._stage_executor(stage)
                executable = executor in {"yolo", "crnn", "ppocr"} and (
                    executor != "crnn" or self._torch is not None
                )
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
                    candidate, resolution = self._resolve_model(stage.get("param"), None, stage)
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
                        "unsupported_reason": unsupported_reason,
                        "ret_keys": [str(item) for item in (stage.get("ret_keys") or [])],
                        "kpt_names": [str(item) for item in (stage.get("kpt_names") or [])],
                        "class_names": self._normalise_names(
                            stage.get("cls_names", ability.get("cls_names", {}))
                        ),
                    }
                )
            entries.append(
                {
                    "id": str(ability_name),
                    "description": str(ability.get("desc", ability_name)),
                    "debug": bool(ability.get("debug", False)),
                    "ret_img": bool(ability.get("ret_img", True)),
                    "supported": supported,
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
        normalized_roi = self._normalise_roi(roi, width, height)
        normalized_helpers = self._normalise_helpers(helpers, width, height)
        request_logs: list[str] = []
        started_at = time.perf_counter()
        ability = self._abilities[ability_name]
        self._log(
            f"开始推理: ability={ability_name}, size={width}x{height}, "
            f"roi={normalized_roi or 'full'}",
            request_logs,
        )

        if ability_name == "image_black":
            response = self._infer_image_black(
                image, ability_name, ability, normalized_roi, request_logs
            )
        else:
            if any(
                self._stage_executor(stage) == "yolo"
                for _, stage in self._pipeline_items(ability)
            ):
                self._ensure_runtime()
            response = self._infer_pipeline(
                image,
                ability_name,
                ability,
                normalized_roi,
                model_override,
                stage_models,
                normalized_helpers,
                settings,
                request_logs,
            )

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

    def _infer_pipeline(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        roi: list[int] | None,
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
                roi,
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
                if roi is not None:
                    x1, y1, x2, y2 = roi
                    source = image[y1:y2, x1:x2]
                    offset = (x1, y1)
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
            roi,
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
            "roi": roi,
            "detections": detections,
            "stages": stage_results,
            "skipped_stages": skipped_stages,
            "models": list(model_infos.values()),
            "inference_context": {
                "points": helpers.get("points", []),
                "boxes": helpers.get("boxes", []),
                "box_adjustments": helpers.get("box_adjustments", []),
                "ret_keys": ret_keys,
                "values": returned_values,
            },
            "annotated_image": self._image_to_data_url(annotated),
            "compatibility": (
                "本地适配器按描述文件执行 YOLO 检测、ROI 裁剪和类别过滤；"
                "特殊硬件算子仍以服务器运行时为准。"
            ),
        }

    def _infer_meter_pipeline(
        self,
        image: Any,
        ability_name: str,
        ability: dict[str, Any],
        roi: list[int] | None,
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
        if roi:
            x1, y1, x2, y2 = roi
            source = image[y1:y2, x1:x2]
            offset = (x1, y1)
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
                image, [], roi, ability_name, helpers.get("points", [])
            )
            return self._meter_response(
                image, ability_name, ability, roi, [], stages_out, model_infos,
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
            image, final_detections, roi, helpers.get("points", [])
        )
        return self._meter_response(
            image,
            ability_name,
            ability,
            roi,
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
        roi: list[int] | None,
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
            "roi": roi,
            "detections": detections,
            "stages": stages,
            "skipped_stages": [],
            "models": list(model_infos.values()),
            "inference_context": {
                "points": helpers.get("points", []),
                "boxes": helpers.get("boxes", []),
                "box_adjustments": helpers.get("box_adjustments", []),
                "meter_pipeline": True,
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
        roi: list[int] | None,
        points: Iterable[dict[str, Any]],
    ) -> Any:
        canvas = self._annotate(image, detections, roi, "__portal_meter__", points)
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
        roi: list[int] | None,
        ability_name: str,
        points: Iterable[dict[str, Any]] | None = None,
    ) -> Any:
        """在图片上绘制 ROI、检测框和置信度标签。"""
        canvas = image.copy()
        if roi:
            x1, y1, x2, y2 = roi
            self._cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 215, 255), 3)
            self._cv2.putText(
                canvas,
                "ROI",
                (x1 + 8, max(24, y1 + 24)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (0, 215, 255),
                2,
                self._cv2.LINE_AA,
            )
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
