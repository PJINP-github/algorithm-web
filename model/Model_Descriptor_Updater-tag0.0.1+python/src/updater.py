"""核心逻辑：锚点模式 / 能力模式 两种替换分支，输出新 yaml 与打包文件夹。"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .model_lib import (
    detect_has_anchors,
    is_model_file,
    load_config,
    normalize_path,
    pick_latest_model,
    safe_filename,
    strip_model_suffix,
)

_MODEL_ROOT = Path(__file__).resolve().parent.parent

_ANCHOR_DEF_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[\w\-.]+)\s*:\s*&(?P<anchor>\w+)\s+(?P<value>\S.*?)\s*$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# 输入扫描
# ---------------------------------------------------------------------------

def scan_inputs(folder: Path, config: dict[str, Any]) -> tuple[Path | None, list[Path]]:
    """扫描导入文件夹：找出 model_descriptor.yaml 与所有模型文件。"""
    suffixes = list(config.get("model_file_suffixes") or [])
    target_name = str(config.get("model_descriptor_name") or "model_descriptor.yaml").lower()

    yaml_path: Path | None = None
    models: list[Path] = []
    for item in sorted(folder.iterdir()):
        if not item.is_file():
            continue
        if item.name.lower() == target_name:
            yaml_path = item
        elif is_model_file(item.name, suffixes):
            models.append(item)
    return yaml_path, models


# ---------------------------------------------------------------------------
# 锚点模式
# ---------------------------------------------------------------------------

def _match_substring_for_value(
    value: str, algorithm_order: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for entry in algorithm_order or []:
        sub = str(entry.get("substring") or "")
        if sub and sub in value:
            return entry
    return None


def update_anchors(
    yaml_text: str,
    algorithm_order: list[dict[str, Any]],
    models: list[Path],
    suffixes: list[str],
    log,
) -> tuple[str, int]:
    """在文本层替换锚点定义值。保留注释、顺序、缩进与锚点标记。"""
    replaced = 0

    def _replace(match: re.Match) -> str:
        nonlocal replaced
        indent = match.group("indent")
        key = match.group("key")
        anchor = match.group("anchor")
        value = match.group("value").strip()

        entry = _match_substring_for_value(value, algorithm_order)
        if entry is None:
            return match.group(0)

        model = pick_latest_model(models, str(entry.get("substring") or ""))
        if model is None:
            log(f"[skip] 锚点 &{anchor} 未找到匹配的模型文件（substring={entry.get('substring')}）")
            return match.group(0)

        new_value = strip_model_suffix(model.name, suffixes)
        log(f"[replace] &{anchor}: {value} -> {new_value}  (来自 {model.name})")
        replaced += 1
        return f"{indent}{key}: &{anchor} {new_value}"

    new_text = _ANCHOR_DEF_RE.sub(_replace, yaml_text)
    return new_text, replaced


# ---------------------------------------------------------------------------
# 能力模式（无锚点）
# ---------------------------------------------------------------------------

def _replace_params_recursive(
    node: Any,
    algorithm_order: list[dict[str, Any]],
    models: list[Path],
    suffixes: list[str],
    log,
    path: str = "",
) -> int:
    replaced = 0
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if key == "param" and isinstance(value, str):
                entry = _match_substring_for_value(value, algorithm_order)
                if entry is not None:
                    model = pick_latest_model(models, str(entry.get("substring") or ""))
                    if model is not None:
                        new_value = strip_model_suffix(model.name, suffixes)
                        node[key] = new_value
                        log(f"[replace] {path}/param: {value} -> {new_value}")
                        replaced += 1
                    else:
                        log(f"[skip] {path}/param 未找到匹配模型（substring={entry.get('substring')}）")
            elif isinstance(value, (dict, list)):
                replaced += _replace_params_recursive(
                    value, algorithm_order, models, suffixes, log, f"{path}/{key}"
                )
    elif isinstance(node, list):
        for i, item in enumerate(node):
            replaced += _replace_params_recursive(
                item, algorithm_order, models, suffixes, log, f"{path}[{i}]"
            )
    return replaced


def update_selected_ability(
    yaml_path: Path,
    ability_id: str,
    algorithm_order: list[dict[str, Any]],
    models: list[Path],
    suffixes: list[str],
    log,
) -> tuple[Any, int]:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)

    if not isinstance(data, dict):
        raise ValueError("model_descriptor.yaml 顶层不是映射，无法处理")

    abilities = data.get("abilities") or {}
    if ability_id not in abilities:
        log(f"[warn] 未在 abilities 中找到 {ability_id}，跳过")
        return data, 0

    replaced = _replace_params_recursive(
        abilities[ability_id], algorithm_order, models, suffixes, log,
        path=f"abilities/{ability_id}",
    )
    return data, replaced


def _dump_yaml_ruamel(data: Any, out_path: Path) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with out_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def process(
    input_folder: Path,
    output_dir: Path,
    ability_id: str,
    config: dict[str, Any],
    log,
) -> dict[str, Any]:
    suffixes = list(config.get("model_file_suffixes") or [".pt", ".om"])
    algorithm_order = list(config.get("Algorithm_Order") or [])
    prefix = str(config.get("output_folder_prefix") or "model_descriptor_pack")

    yaml_path, models = scan_inputs(input_folder, config)
    if yaml_path is None:
        raise FileNotFoundError(
            f"未在 {input_folder} 找到 {config.get('model_descriptor_name')}"
        )
    if not models:
        log("[warn] 未扫描到模型文件，yaml 不会发生替换")

    yaml_text = yaml_path.read_text(encoding="utf-8")
    has_anchors = detect_has_anchors(yaml_text)
    log(f"[info] 锚点检测：{'存在 & 锚点，使用锚点替换' if has_anchors else '未使用锚点，走能力选择模式'}")

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    pack_dir = output_dir / f"{safe_filename(prefix)}_{ts}"
    original_dir = pack_dir / "original"
    pack_dir.mkdir(parents=True, exist_ok=True)
    original_dir.mkdir(parents=True, exist_ok=True)

    # 备份原 yaml
    backup_path = original_dir / yaml_path.name
    shutil.copy2(yaml_path, backup_path)
    log(f"[info] 原始 yaml 备份到 {backup_path}")

    out_yaml_path = pack_dir / yaml_path.name

    if has_anchors:
        new_text, replaced = update_anchors(
            yaml_text, algorithm_order, models, suffixes, log
        )
        out_yaml_path.write_text(new_text, encoding="utf-8")
    else:
        if not ability_id:
            log("[warn] 当前 yaml 不使用锚点，但未指定能力 ID；yaml 保持原样")
            out_yaml_path.write_text(yaml_text, encoding="utf-8")
            replaced = 0
        else:
            try:
                data, replaced = update_selected_ability(
                    yaml_path, ability_id, algorithm_order, models, suffixes, log
                )
                _dump_yaml_ruamel(data, out_yaml_path)
            except Exception as exc:
                log(f"[warn] 能力模式处理失败：{exc}；yaml 保持原样")
                out_yaml_path.write_text(yaml_text, encoding="utf-8")
                replaced = 0

    # 复制所有模型文件到 pack_dir
    copied: list[str] = []
    for m in models:
        dst = pack_dir / m.name
        try:
            shutil.copy2(m, dst)
            copied.append(dst.name)
            log(f"[copy] {m.name}")
        except Exception as exc:
            log(f"[warn] 复制模型失败 {m.name}: {exc}")

    summary = (
        f"替换 {replaced} 处；输出 {pack_dir.name}/"
        f"（yaml={out_yaml_path.name}，模型 {len(copied)} 个）"
    )
    return {
        "status": "completed",
        "summary": summary,
        "folder_name": pack_dir.name,
        "yaml_name": out_yaml_path.name,
        "models_copied": copied,
        "replaced": replaced,
        "mode": "anchors" if has_anchors else "ability",
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def run_from_request(request: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)
        print(msg)

    parameters = request.get("parameters") or {}
    ability_id = str(parameters.get("ability_id") or "").strip()

    uploads = request.get("uploads") or {}
    input_path = uploads.get("input_folder") or request.get("input_path")
    if not input_path:
        raise ValueError("缺少 input_folder 上传")
    input_folder = Path(input_path)
    if not input_folder.exists() or not input_folder.is_dir():
        raise FileNotFoundError(f"导入文件夹不存在或不是目录：{input_folder}")

    output_dir = Path(request["output_dir"])

    result = process(input_folder, output_dir, ability_id, config, log)
    result["files"] = [result["yaml_name"]]
    result["folders"] = [result["folder_name"]]
    result["logs"] = log_lines
    return result


def run_local() -> int:
    config = load_config()
    print("=== model_descriptor.yaml 参数替换工具（本地模式）===")

    raw_folder = input("请输入包含 model_descriptor.yaml 与模型文件的文件夹路径：").strip()
    if not raw_folder:
        print("未提供文件夹，退出。")
        return 1
    input_folder = normalize_path(raw_folder)
    if input_folder is None or not input_folder.exists() or not input_folder.is_dir():
        print(f"文件夹不存在或不是目录：{input_folder}")
        return 1

    default_out = str(input_folder)
    raw_out = input(f"请输入输出目录（回车使用默认：{default_out}）：").strip()
    output_dir = normalize_path(raw_out) if raw_out else input_folder
    if output_dir is None:
        print("输出目录无效，退出。")
        return 1

    yaml_path, _models = scan_inputs(input_folder, config)
    if yaml_path is None:
        print(f"未在文件夹中找到 {config.get('model_descriptor_name')}，退出。")
        return 1

    yaml_text = yaml_path.read_text(encoding="utf-8")
    has_anchors = detect_has_anchors(yaml_text)
    ability_id = ""
    if has_anchors:
        print("检测到锚点，将自动按 Algorithm_Order 替换。")
    else:
        print("未检测到锚点，需要选择一个能力 ID。")

    def log(msg: str) -> None:
        print(msg)

    try:
        result = process(input_folder, output_dir, ability_id, config, log)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"[FAIL] {exc}")
        return 1

    print(f"[OK] {result['summary']}")
    print(f"输出目录：{output_dir}")
    return 0