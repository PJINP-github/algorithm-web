"""Rust workspace bridge for the original Trueno local inference engine."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from local_inference.engine import LocalInferenceEngine, LocalInferenceError



def resolve_trueno_root() -> Path:
    """Resolve the Trueno directory independently from the process cwd."""
    configured = os.environ.get("RUST_PORTAL_TRUENO_ROOT", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured.replace("\\", "/")).expanduser())
    candidates.extend((Path(__file__).resolve().parent, Path.cwd()))
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "local_inference").is_dir() and (
            root / "storage" / "model_descriptor.yaml"
        ).is_file():
            return root
    return Path(__file__).resolve().parent


ROOT = resolve_trueno_root()
OCR_MODEL_HINT = "dm-common-paddleocr"
POSE_METER_MODEL = "E2025070221.E2025070221-007.at-pmcnc-single.m-pmcnc-pose-meter-single+20260906.53"
POSE_POINTER_MODEL = "E2025070221.E2025070221-007.at-pmcnc-single.m-pmcnc-pose-pointer-single+20260906.54"
POSE_MARKS_MODEL = "E2025070221.E2025070221-007.at-pmcnc-single.m-pmcnc-pose-marks-single+20260906.54"
DM_COMMON_MODEL = "E2025070221.E2025070221-007.at-dm-common.m-dm-seg-meter-common+20260904.55"


def engine(import_dir: str | Path | None = None) -> LocalInferenceEngine:
    additional_model_dirs = []
    if import_dir:
        candidate = Path(str(import_dir).replace("\\", "/")).expanduser().resolve()
        if candidate.is_dir() and candidate.is_relative_to(ROOT):
            additional_model_dirs.append(candidate)
    return LocalInferenceEngine(
        root_dir=ROOT,
        descriptor_path=ROOT / "storage" / "model_descriptor.yaml",
        model_dir=ROOT / "model",
        additional_model_dirs=additional_model_dirs,
    )


def configured_entries(current: LocalInferenceEngine) -> list[dict[str, Any]]:
    """直接返回描述文件生成的能力目录，不经过项目自定义配置重排。"""
    return [
        copy.deepcopy(entry)
        for entry in current.ability_catalog().get("entries", [])
        if isinstance(entry, dict) and entry.get("id")
    ]


def model_signature(value: str) -> str:
    name = Path(str(value or "")).name.lower()
    match = re.search(r"(at-[^+_]+\.m-[^+_]+)", name)
    return match.group(1) if match else ""


def model_date(candidate: Any) -> tuple[int, int]:
    match = re.search(r"(?:\+|_)(\d{8})(?:\.(\d+))?", candidate.name)
    if not match:
        return (0, 0)
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
    )


def model_version_label(candidate: Any) -> str:
    match = re.search(r"(?:\+|_)(\d{8})(?:\.(\d+))?", candidate.name)
    if not match:
        return ""
    suffix = match.group(2)
    return f"{match.group(1)}.{suffix}" if suffix else match.group(1)


def model_display_name(candidate: Any) -> str:
    version = model_version_label(candidate)
    if version:
        return version
    match = re.search(r"(at-[^+_]+\.m)", candidate.name.lower())
    if match:
        return match.group(1)
    return model_signature(candidate.name) or candidate.name


def model_source(candidate: Any) -> str:
    relative_path = str(getattr(candidate, "relative_path", "")).replace("\\", "/")
    return "import" if relative_path.lower().startswith("sources/temp/") else "native"


def model_payload(candidate: Any) -> dict[str, Any]:
    payload = candidate.to_dict()
    payload["display_name"] = model_display_name(candidate)
    payload["version"] = model_version_label(candidate)
    payload["source"] = model_source(candidate)
    return payload


def model_mtime(candidate: Any) -> int:
    try:
        return candidate.absolute_path.stat().st_mtime_ns
    except OSError:
        return 0


def select_latest_model(candidates: list[Any]) -> Any:
    """按文件名版本优先、文件修改时间兜底选择最新权重。"""
    dated = [candidate for candidate in candidates if model_date(candidate) != (0, 0)]
    pool = dated or candidates
    return max(
        pool,
        key=lambda candidate: (
            *model_date(candidate),
            model_mtime(candidate),
            candidate.name.lower(),
        ),
    )


def latest_model_for_substring(candidates: list[Any], substring: str) -> Any:
    needle = str(substring or "").strip().lower()
    if not needle:
        return None
    matches = [
        candidate
        for candidate in candidates
        if needle in candidate.name.lower()
    ]
    return select_latest_model(matches) if matches else None


def latest_model_for_parameter(candidates: list[Any], parameter: str) -> Any:
    """Match descriptor parameters across weight version and filename suffix changes."""
    selected = latest_model_for_substring(candidates, parameter)
    if selected:
        return selected

    signature = model_signature(parameter)
    if not signature:
        return None
    matches = [candidate for candidate in candidates if signature in candidate.name.lower()]
    return select_latest_model(matches) if matches else None


def selectable_model_candidates(candidates: list[Any]) -> list[Any]:
    """描述文件的每个 pipeline 阶段都可能是用户可选模型。"""
    return list(candidates)


def mapped_model(entry: dict[str, Any], candidates: list[Any]) -> Any:
    """按描述文件 pipeline 的最终阶段参数选择默认模型。"""
    stages = entry.get("stages") if isinstance(entry, dict) else None
    if not isinstance(stages, list):
        return None
    for stage in reversed(stages):
        if not isinstance(stage, dict):
            continue
        selected = latest_model_for_parameter(
            candidates, str(stage.get("parameter", "")).strip()
        )
        if selected:
            return selected
    return None


def resolve_ability_name(
    current: LocalInferenceEngine, ability_name: str, selected_model: str
) -> str:
    if ability_name in current._abilities:
        return ability_name

    aliases = [
        name
        for name in current._abilities
        if name.startswith(f"{ability_name}_") or ability_name.startswith(f"{name}_")
    ]
    if aliases:
        return sorted(aliases, key=len)[0]

    selected_signature = model_signature(selected_model)
    if selected_signature:
        requested_tokens = {
            token
            for token in re.split(r"[_\-]+", ability_name.lower())
            if token and token not in {"mn", "p"}
        }
        scored: list[tuple[int, str]] = []
        for candidate_name, ability in current._abilities.items():
            if not isinstance(ability, dict):
                continue
            score = 0
            for _index, stage in current._pipeline_items(ability):
                stage_text = " ".join(
                    str(stage.get(key, "")).lower()
                    for key in ("base", "patch", "param")
                )
                if selected_signature in stage_text:
                    score += 20
            if score <= 0:
                continue
            candidate_tokens = {
                token
                for token in re.split(r"[_\-]+", candidate_name.lower())
                if token and token not in {"mn", "p"}
            }
            score += len(requested_tokens & candidate_tokens)
            scored.append((score, candidate_name))
        if scored:
            scored.sort(key=lambda item: (-item[0], item[1]))
            return scored[0][1]

    available = ", ".join(sorted(current._abilities.keys())[:20])
    raise LocalInferenceError(
        f"未知能力: {ability_name}；descriptor 已更新，请重新登记模型。可用能力示例: {available}"
    )


def calibration_polygons(value: str) -> list[list[list[int]]]:
    if not value:
        return []
    raw = json.loads(value)
    if isinstance(raw, dict):
        raw = raw.get("polygons", raw.get("points", []))
    if not isinstance(raw, list) or not raw:
        return []

    def normalise_polygon(item: Any) -> list[list[int]]:
        if isinstance(item, dict):
            item = item.get("points", [])
        if (
            isinstance(item, list)
            and len(item) == 4
            and all(not isinstance(point, (list, tuple, dict)) for point in item)
        ):
            item = [[item[0], item[1]], [item[2], item[1]],
                    [item[2], item[3]], [item[0], item[3]]]
        if not isinstance(item, list):
            return []
        points = []
        for point in item:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append([max(0, int(point[0])), max(0, int(point[1]))])
        return points if len(points) >= 4 else []

    first = raw[0]
    candidates = [raw] if (
        isinstance(first, (list, tuple))
        and len(first) >= 2
        and not isinstance(first[0], (list, tuple, dict))
    ) else raw
    return [
        polygon
        for polygon in (normalise_polygon(item) for item in candidates)
        if polygon
    ]


def decode_data_url(value: str) -> Any:
    encoded = value.split(",", 1)[1] if "," in value else value
    raw = base64.b64decode(encoded)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法解析推理输出图片")
    return image


def encode_data_url(image: Any) -> str:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("无法编码推理输出图片")
    return "data:image/png;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")


def stage_overrides(
    current: LocalInferenceEngine, ability_name: str, selected_model: str
) -> dict[str, str]:
    """只把用户选中的权重交给同类型、同语义的流水线阶段。"""
    if not selected_model:
        return {}
    ability = current._abilities.get(ability_name) or {}
    pipeline = current._pipeline_items(ability)
    selected = Path(selected_model).name.lower()
    pose_target = (
        3 if "pose-marks-single" in selected
        else 2 if "pose-pointer-single" in selected
        else 1 if "pose-meter-single" in selected
        else None
    )
    overrides: dict[str, str] = {}
    for index, stage in pipeline:
        executor = current._stage_executor(stage)
        stage_text = " ".join(
            str(stage.get(key, "")).lower()
            for key in ("base", "patch", "param")
        )
        same_executor = (
            ("paddleocr" in selected or "ocr" in selected)
            and executor in {"crnn", "ppocr"}
        ) or (
            "crnn" in selected
            and executor == "crnn"
        ) or (
            executor == "yolo"
            and not any(token in selected for token in ("paddleocr", "crnn", "ocr"))
        )
        if not same_executor:
            continue
        if pose_target is not None and index != pose_target:
            continue
        semantic = next(
            (
                token
                for token in ("marks", "pointer", "meter", "pose")
                if token in selected and token in stage_text
            ),
            "",
        )
        # 单阶段能力的登记模型就是该阶段的最终模型，不再触发
        # descriptor 的 best-effort 回退。
        if pose_target is not None:
            if index == pose_target:
                overrides[str(index)] = selected_model
            continue
        if len(pipeline) == 1 and executor in {"yolo", "crnn", "ppocr"}:
            overrides[str(index)] = selected_model
        elif semantic or executor == "ppocr" or pose_target == index:
            overrides[str(index)] = selected_model
    return overrides


def chain_overrides(
    current: LocalInferenceEngine,
    ability_name: str,
    selected_model: str,
    candidates: list[Any],
) -> dict[str, str]:
    """按描述文件 pipeline 参数补齐多阶段推理所需的前置权重。"""
    pipeline = current._pipeline_items(current._abilities.get(ability_name) or {})
    if len(pipeline) < 2:
        return {}

    overrides: dict[str, str] = {}
    for position, (stage_index, stage) in enumerate(pipeline[:-1]):
        selected = latest_model_for_parameter(
            candidates, str(stage.get("param", "")).strip()
        )
        if selected:
            overrides[str(stage_index)] = selected.relative_path
    final_index = pipeline[-1][0]
    overrides[str(final_index)] = selected_model
    return overrides


def selected_ability(
    current: LocalInferenceEngine, ability_name: str, selected_model: str
) -> str:
    """为特殊权重构造其真实的多阶段依赖链。"""
    selected = Path(selected_model).name.lower()
    if OCR_MODEL_HINT in selected:
        current._abilities["__portal_ocr__"] = {
            "desc": "通用仪表 OCR",
            "pipeline": {
                1: {
                    "base": "base_yolo",
                    "patch": "meter_digit_p1",
                    "param": DM_COMMON_MODEL,
                },
                2: {
                    "base": "base_ppocr",
                    "patch": "meter_digit_p2",
                    "param": Path(selected_model).stem,
                    "ocrdict": {"ocrdict": "-0123456789.abc"},
                },
            },
        }
        return "__portal_ocr__"
    if any(
        token in selected
        for token in ("pose-meter-single", "pose-pointer-single", "pose-marks-single")
    ):
        source = current._abilities.get(ability_name) or {}
        if not any(
            str(stage.get("patch", "")).startswith("meter_p")
            for _, stage in current._pipeline_items(source)
        ):
            source = next(
                (
                    item
                    for item in current._abilities.values()
                    if isinstance(item, dict)
                    and any(
                        str(stage.get("patch", "")) == "meter_p1"
                        for _, stage in current._pipeline_items(item)
                    )
                ),
                {},
            )
        meter_ability = copy.deepcopy(source)
        meter_ability["desc"] = meter_ability.get("desc") or "连续表计读数"
        pipeline = {
            index: copy.deepcopy(stage)
            for index, stage in current._pipeline_items(meter_ability)
            if (
                str(stage.get("patch", "")) in {"meter_p1", "meter_p2"}
                or str(stage.get("patch", "")).startswith("meter_p3")
            )
        }
        if len(pipeline) != 3:
            pipeline = {
                1: {
                    "base": "base_yolo",
                    "patch": "meter_p1",
                    "param": POSE_METER_MODEL,
                    "kpt_names": ["center_pointer"],
                },
                2: {
                    "base": "base_yolo",
                    "patch": "meter_p2",
                    "param": POSE_POINTER_MODEL,
                    "kpt_names": ["mark_pointer_black"],
                },
                3: {
                    "base": "base_yolo",
                    "patch": "meter_p3",
                    "param": POSE_MARKS_MODEL,
                    "kpt_names": ["mark"],
                },
            }
        descriptor = {
            key: meter_ability.get(key)
            for key in (
                "scale",
                "precision",
                "reverse",
                "value_floor",
                "debug",
                "point_map",
            )
            if meter_ability.get(key) is not None
        }
        label = meter_ability.get("label") or {}
        if isinstance(label, dict) and isinstance(label.get("marks"), list):
            descriptor["marks"] = copy.deepcopy(label["marks"])
        if isinstance(meter_ability.get("marks"), list):
            descriptor["marks"] = copy.deepcopy(meter_ability["marks"])
        meter_ability["pipeline"] = pipeline
        meter_ability["meter_pipeline"] = True
        meter_ability["meter_descriptor"] = descriptor
        current._abilities["__portal_meter__"] = meter_ability
        return "__portal_meter__"
    return resolve_ability_name(current, ability_name, selected_model)


def infer(args: argparse.Namespace) -> dict[str, Any]:
    image = cv2.imread(str(Path(args.image).resolve()), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法读取输入图片")
    calibration = calibration_polygons(args.calibration)
    image_for_inference = image
    helpers = json.loads(args.helpers) if args.helpers else None
    current = engine(args.import_dir)
    mode = str(args.mode or "auto").strip().lower()
    if mode not in {"auto", "local", "import"}:
        raise ValueError("模型调用模式无效")
    if mode == "import":
        raise ValueError("导入模式请切换到本地模式选择已上传的模型")
    inference_mode = str(args.inference_mode or "local").strip().lower()
    if inference_mode not in {"local", "src"}:
        raise ValueError("推理链路无效")
    requested_ability = args.ability
    catalog_ability = args.ability
    candidates = current.discover_models()
    entries = configured_entries(current)
    entry = next(
        (item for item in entries if item.get("id") == catalog_ability),
        None,
    )
    if mode == "auto":
        selected = mapped_model(
            entry or {},
            candidates,
        )
        if selected:
            args.model = selected.relative_path
    elif not args.model:
        raise ValueError("本地模式请选择模型文件")
    if args.model:
        selected_name = Path(args.model).name.lower()
        uses_portal_pipeline = OCR_MODEL_HINT in selected_name or any(
            token in selected_name
            for token in ("pose-meter-single", "pose-pointer-single", "pose-marks-single")
        )
        if not uses_portal_pipeline:
            catalog_ability = resolve_ability_name(current, args.ability, args.model)
    ability_name = selected_ability(current, catalog_ability, args.model)
    overrides = stage_overrides(current, ability_name, args.model)
    overrides.update(chain_overrides(current, ability_name, args.model, candidates))
    result = current.infer(
        image=image_for_inference,
        ability_name=ability_name,
        stage_models=overrides or None,
        helpers=helpers,
        calibration=calibration,
        inference_mode=inference_mode,
    )
    if calibration:
        annotated = decode_data_url(result["annotated_image"])
        for polygon in calibration:
            cv2.polylines(
                annotated,
                [np.asarray(polygon, dtype=np.int32)],
                True,
                (0, 255, 255),
                2,
            )
        result["annotated_image"] = encode_data_url(annotated)
        result["calibration_polygons"] = calibration
    if ability_name != requested_ability and not ability_name.startswith("__portal_"):
        result.setdefault("logs", []).append(
            f"descriptor ability resolved: {requested_ability} -> {ability_name}"
        )
    result["resolved_ability"] = ability_name
    result["requested_inference_mode"] = inference_mode
    result.setdefault("inference_mode", inference_mode)
    if calibration:
        result.setdefault("logs", []).append(
            f"已应用 {len(calibration)} 个标定框，仅保留标定框内的推理结果。"
        )
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(result["annotated_image"].split(",", 1)[1]))
    result["output_path"] = str(output)
    if args.result:
        result_path = Path(args.result).resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--ability")
    parser.add_argument("--model", default="")
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--inference-mode", default="local")
    parser.add_argument("--import-dir", default="")
    parser.add_argument("--calibration", default="")
    parser.add_argument("--helpers", default="")
    parser.add_argument("--output")
    parser.add_argument("--result")
    args = parser.parse_args()
    if args.catalog:
        current = engine(args.import_dir)
        candidates = current.discover_models()
        catalog = current.ability_catalog()
        catalog["entries"] = configured_entries(current)
        catalog["models"] = [
            model_payload(item) for item in selectable_model_candidates(candidates)
        ]
        for entry in catalog["entries"]:
            selected = mapped_model(entry, candidates)
            if selected:
                entry["mapped_model"] = model_payload(selected)
        configured_ids = {entry["id"] for entry in catalog["entries"]}
        if catalog.get("recommended_ability") not in configured_ids:
            catalog["recommended_ability"] = (
                catalog["entries"][0]["id"] if catalog["entries"] else ""
            )
        catalog["model_count"] = len(catalog["models"])
        print(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))
        return
    if not args.image or not args.ability or not args.output:
        raise ValueError("推理参数不完整")
    result = infer(args)
    print(f"Trueno 推理完成：{len(result.get('detections') or [])} 个检测结果")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
