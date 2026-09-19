#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web portal adapter for the image filtering model.

The Rust portal owns isolation and permissions. This adapter only translates the
portal request into the model's existing command-line workflow.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


MODEL_ROOT = Path(__file__).resolve().parent
SCRIPT = MODEL_ROOT / "similar_group.py"
CONFIG = MODEL_ROOT / "config.yaml"


def log(message: str) -> None:
    print(message, flush=True)


def load_request(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("request.json 必须是对象")
    return value


def choose_input(request: dict) -> Path:
    uploads = request.get("uploads") or {}
    if not isinstance(uploads, dict):
        raise ValueError("uploads 必须是对象")

    candidates = []
    for value in uploads.values():
        if isinstance(value, str) and value.strip():
            candidates.append(Path(value))
    if not candidates:
        raise ValueError("请先导入图片文件或文件夹")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    raise ValueError("导入的图片路径不存在")


def build_runtime_config(request: dict, output_dir: Path) -> Path:
    with CONFIG.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    config = copy.deepcopy(config)

    parameters = request.get("parameters") or {}
    action = str(request.get("action") or "group_only")
    post_process = config.setdefault("post_process", {})
    if action == "group_only":
        post_process["backup_zip"] = False
        post_process["fuzzy_removal"] = False
        post_process["same_category_group_quantity_retention"] = 0
    elif action == "group_and_clean":
        post_process["backup_zip"] = bool(parameters.get("backup_zip", True))
        post_process["fuzzy_removal"] = str(
            parameters.get("blur_policy", "remove")
        ).lower() == "remove"
        post_process["same_category_group_quantity_retention"] = max(
            0, int(parameters.get("retention_count", 3) or 0)
        )
    elif action == "group_and_backup":
        post_process["backup_zip"] = bool(parameters.get("backup_zip", True))
        post_process["fuzzy_removal"] = False
        post_process["same_category_group_quantity_retention"] = 0
    else:
        raise ValueError(f"不支持的处理模式：{action}")

    runtime_config = output_dir / ".portal-runtime-config.yaml"
    with runtime_config.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
    return runtime_config


def copy_reports(input_root: Path, output_dir: Path) -> list[str]:
    copied = []
    for source in input_root.rglob("*"):
        if not source.is_file():
            continue
        name = source.name
        is_report = (
            name == "work.yaml"
            or (source.suffix.lower() == ".xlsx" and "样本分组展示" in name)
            or name.endswith("_backup.zip")
        )
        if not is_report:
            continue
        target = output_dir / source.relative_to(input_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target.relative_to(output_dir).as_posix())
    return sorted(copied)


def run(request: dict) -> dict:
    output_dir = Path(str(request.get("output_dir") or "")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_root = choose_input(request).resolve()
    runtime_config = build_runtime_config(request, output_dir)
    log(f"[输入] {input_root}")
    log(f"[模式] {request.get('action', 'group_only')}")
    log(f"[输出] {output_dir}")
    try:
        command = [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(input_root),
            "--config",
            str(runtime_config),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(MODEL_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                log(line)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"similar_group.py 退出码 {code}")
        files = copy_reports(input_root, output_dir)
        result = {
            "status": "completed",
            "summary": f"处理完成，收集 {len(files)} 个报表文件。",
            "files": files,
            "folders": [input_root.name],
        }
        return result
    finally:
        runtime_config.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-file", required=True)
    args = parser.parse_args()
    request_path = Path(args.request_file).resolve()
    try:
        request = load_request(request_path)
        result_value = str(request.get("result_path") or "").strip()
        if not result_value:
            raise ValueError("request.json 缺少 result_path")
        result_path = Path(result_value).resolve()
        result = run(request)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(f"[错误] {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
