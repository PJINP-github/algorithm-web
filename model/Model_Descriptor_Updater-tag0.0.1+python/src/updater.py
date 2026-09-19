"""核心逻辑：锚点模式 / 能力模式 两种替换分支，输出新 yaml 与打包文件夹。

输出目录结构：
    model_descriptor_pack_<ts>/
      model_descriptor.yaml            # 原始上传文件（原样保留）
      model_descriptor_updated.yaml    # 替换后的新文件（可配置名）
      original/
        model_descriptor.yaml          # 备份
      <导入的模型文件...>
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .model_lib import (
    ANCHOR_DEF_RE,
    detect_ambiguous_substrings,
    detect_has_anchors,
    list_folder_contents,
    load_config,
    normalize_path,
    pick_latest_model,
    safe_filename,
    scan_anchor_definitions,
    scan_inputs,
    strip_model_suffix,
)


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
    anchor_override: str,
    log,
) -> tuple[str, int]:
    anchor_defs = scan_anchor_definitions(yaml_text)

    ambiguous_pairs = detect_ambiguous_substrings(yaml_text, algorithm_order)
    ambiguous_anchors: set[str] = set()
    for sub, anchors in ambiguous_pairs:
        log(
            f"[warn] substring={sub!r} 命中多个锚点：{anchors}；"
            f"未提供 anchor_override 时将被跳过"
        )
        ambiguous_anchors.update(anchors)

    replaced = 0

    def _replace(match) -> str:
        nonlocal replaced
        indent = match.group("indent")
        key = match.group("key")
        anchor = match.group("anchor")
        value = match.group("value").strip()

        if anchor_override:
            if anchor != anchor_override:
                return match.group(0)
        else:
            if anchor in ambiguous_anchors:
                log(f"[skip] &{anchor} 存在歧义，未指定 anchor_override")
                return match.group(0)

        entry = _match_substring_for_value(value, algorithm_order)
        if entry is None:
            return match.group(0)

        model = pick_latest_model(models, str(entry.get("substring") or ""))
        if model is None:
            log(
                f"[skip] &{anchor} 未找到匹配的模型文件"
                f"（substring={entry.get('substring')}）"
            )
            return match.group(0)

        new_value = strip_model_suffix(model.name, suffixes)
        log(f"[replace] &{anchor}: {value} -> {new_value}  (来自 {model.name})")
        replaced += 1
        return f"{indent}{key}: &{anchor} {new_value}"

    new_text = ANCHOR_DEF_RE.sub(_replace, yaml_text)

    if anchor_override:
        log(f"[info] anchor_override={anchor_override}，其余锚点全部跳过")

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
                    model = pick_latest_model(
                        models, str(entry.get("substring") or "")
                    )
                    if model is not None:
                        new_value = strip_model_suffix(model.name, suffixes)
                        node[key] = new_value
                        log(f"[replace] {path}/param: {value} -> {new_value}")
                        replaced += 1
                    else:
                        log(
                            f"[skip] {path}/param 未找到匹配模型"
                            f"（substring={entry.get('substring')}）"
                        )
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
        abilities[ability_id],
        algorithm_order,
        models,
        suffixes,
        log,
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
    anchor_override: str,
    config: dict[str, Any],
    log,
) -> dict[str, Any]:
    suffixes = list(config.get("model_file_suffixes") or [".pt", ".om"])
    algorithm_order = list(config.get("Algorithm_Order") or [])
    prefix = str(config.get("output_folder_prefix") or "model_descriptor_pack")
    original_name = str(config.get("model_descriptor_name") or "model_descriptor.yaml")
    updated_name = str(
        config.get("output_yaml_name") or "model_descriptor_updated.yaml"
    )

    yaml_path, models = scan_inputs(input_folder, config)
    if yaml_path is None:
        contents = list_folder_contents(input_folder)
        if contents:
            log("[debug] 导入文件夹内容：")
            for c in contents:
                log(f"    {c}")
        else:
            log("[debug] 导入文件夹为空或无法读取")
        raise FileNotFoundError(
            f"未在 {input_folder} 找到 {original_name}"
        )

    if not models:
        log("[warn] 未扫描到模型文件，yaml 不会发生替换")

    yaml_text = yaml_path.read_text(encoding="utf-8")
    has_anchors = detect_has_anchors(yaml_text)
    log(
        "[info] 锚点检测："
        + ("存在 & 锚点，使用锚点替换" if has_anchors else "未使用锚点，走能力选择模式")
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    pack_dir = output_dir / f"{safe_filename(prefix)}_{ts}"
    original_dir = pack_dir / "original"
    pack_dir.mkdir(parents=True, exist_ok=True)
    original_dir.mkdir(parents=True, exist_ok=True)

    # 原样保留上传的 model_descriptor.yaml 到 pack_dir 根与 original/ 子目录
    original_root = pack_dir / original_name
    original_root.write_bytes(yaml_path.read_bytes())
    backup_path = original_dir / original_name
    backup_path.write_bytes(yaml_path.read_bytes())
    log(f"[info] 原文件保留到 {original_root}")
    log(f"[info] 原始 yaml 备份到 {backup_path}")

    # 替换后的新文件使用独立文件名
    out_yaml_path = pack_dir / updated_name

    if has_anchors:
        new_text, replaced = update_anchors(
            yaml_text,
            algorithm_order,
            models,
            suffixes,
            anchor_override,
            log,
        )
        out_yaml_path.write_text(new_text, encoding="utf-8")
    else:
        if not ability_id:
            log("[warn] 当前 yaml 不使用锚点，但未指定能力 ID；新 yaml 与原文件相同")
            out_yaml_path.write_text(yaml_text, encoding="utf-8")
            replaced = 0
        else:
            try:
                data, replaced = update_selected_ability(
                    yaml_path, ability_id, algorithm_order, models, suffixes, log
                )
                _dump_yaml_ruamel(data, out_yaml_path)
            except Exception as exc:
                log(f"[warn] 能力模式处理失败：{exc}；新 yaml 与原文件相同")
                out_yaml_path.write_text(yaml_text, encoding="utf-8")
                replaced = 0

    copied: list[str] = []
    for m in models:
        dst = pack_dir / m.name
        try:
            dst.write_bytes(m.read_bytes())
            copied.append(dst.name)
            log(f"[copy] {m.name}")
        except Exception as exc:
            log(f"[warn] 复制模型失败 {m.name}: {exc}")

    summary = (
        f"替换 {replaced} 处；输出 {pack_dir.name}/"
        f"（原文件 {original_name}，更新后 {updated_name}，模型 {len(copied)} 个）"
    )
    return {
        "status": "completed",
        "summary": summary,
        "folder_name": pack_dir.name,
        "original_yaml_name": original_name,
        "updated_yaml_name": updated_name,
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
    anchor_override = str(parameters.get("anchor_override") or "").strip()

    uploads = request.get("uploads") or {}
    input_path = uploads.get("input_folder") or request.get("input_path")
    if not input_path:
        raise ValueError("缺少 input_folder 上传")
    input_folder = Path(input_path)
    if not input_folder.exists() or not input_folder.is_dir():
        raise FileNotFoundError(f"导入文件夹不存在或不是目录：{input_folder}")

    output_dir = Path(request["output_dir"])

    result = process(
        input_folder, output_dir, ability_id, anchor_override, config, log
    )

    folder_name = result["folder_name"]
    original_name = result["original_yaml_name"]
    updated_name = result["updated_yaml_name"]

    # 关键：files 使用相对 output_dir 的完整子路径，
    # 让 Rust 在任务目录内正确定位并展示"处理文件展示区"。
    result["files"] = [
        f"{folder_name}/{original_name}",
        f"{folder_name}/{updated_name}",
    ]
    result["folders"] = [folder_name]
    result["logs"] = log_lines
    return result


def run_local() -> int:
    config = load_config()
    print("=== model_descriptor.yaml 参数替换工具（本地模式）===")

    raw_folder = input(
        "请输入包含 model_descriptor.yaml 与模型文件的文件夹路径："
    ).strip()
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
        print(
            f"未在文件夹中找到 {config.get('model_descriptor_name')}，退出。"
        )
        for c in list_folder_contents(input_folder):
            print(f"    {c}")
        return 1

    yaml_text = yaml_path.read_text(encoding="utf-8")
    has_anchors = detect_has_anchors(yaml_text)
    ability_id = ""
    anchor_override = ""

    if has_anchors:
        print("检测到锚点，将自动按 Algorithm_Order 替换。")
        ambiguous = detect_ambiguous_substrings(
            yaml_text, config.get("Algorithm_Order") or []
        )
        if ambiguous:
            print("检测到 substring 命中多个锚点：")
            for sub, anchors in ambiguous:
                print(f"  substring={sub} -> {anchors}")
            anchor_override = input(
                "请输入 anchor_override 以指定唯一锚点"
                "（留空则跳过歧义锚点）："
            ).strip()
    else:
        print("未检测到锚点；如需替换能力下的 param，请输入能力 ID。")
        ability_id = input("请输入 ability_id（留空则不改 yaml）：").strip()

    def log(msg: str) -> None:
        print(msg)

    try:
        result = process(
            input_folder,
            output_dir,
            ability_id,
            anchor_override,
            config,
            log,
        )
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"[FAIL] {exc}")
        return 1

    print(f"[OK] {result['summary']}")
    print(f"输出目录：{output_dir}")
    return 0